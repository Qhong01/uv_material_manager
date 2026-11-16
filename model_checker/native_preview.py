"""Opt-in lit material diagnostics. Source material slots are never replaced."""

import time
from types import SimpleNamespace

import bmesh
import bpy
import numpy as np

from . import diagnostic

PREFIX = '.UVMM Material Preview'
STRETCH = PREFIX + ' Stretch'
FLIPPED = PREFIX + ' Flipped'


def modifier_for(obj):
    return next((m for m in obj.modifiers if m.type == 'NODES'
                 and m.node_group is not None
                 and m.node_group.get('uvmm_native_preview', False)), None)


def remove_preview(obj):
    modifier = modifier_for(obj)
    if modifier is not None:
        # Keep orphan IDs alive for Blender's undo memfiles.
        obj.modifiers.remove(modifier)


def remove_all():
    for obj in bpy.data.objects:
        if obj.type == 'MESH':
            remove_preview(obj)


def suspend_for_save():
    remove_all()
    # Orphan groups still hold user references to materials. Release those
    # references without freeing IDs that Blender's undo history may retain.
    for group in bpy.data.node_groups:
        if group.get('uvmm_native_preview', False) and group.users == 0:
            group['uvmm_binding'] = ''
            for node in group.nodes:
                if node.type == 'SET_MATERIAL':
                    node.inputs['Material'].default_value = None
    for mesh in bpy.data.meshes:
        for name in (STRETCH, FLIPPED):
            if mesh.is_editmode:
                bm = bmesh.from_edit_mesh(mesh)
                layers = bm.verts.layers.float if name == STRETCH else bm.loops.layers.float
                layer = layers.get(name)
                if layer is not None:
                    layers.remove(layer)
                    bm.normal_update()
                    bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
            else:
                attr = mesh.attributes.get(name)
                if attr is not None:
                    mesh.attributes.remove(attr)
    for scene in bpy.data.scenes:
        if hasattr(scene, 'model_checker_state'):
            scene.model_checker_state.uv_native_scratch = ''
            for item in scene.model_checker_state.uv_mesh_states:
                item.native_signature = ''


def _mesh_snapshot(mesh):
    """Use Blender's bulk BMesh export instead of Python triangle traversal."""
    if not mesh.is_editmode:
        return mesh
    state = bpy.context.scene.model_checker_state
    snapshot = bpy.data.meshes.get(state.uv_native_scratch)
    if snapshot is None or not snapshot.get('uvmm_native_scratch', False) or snapshot.users:
        snapshot = next((mesh for mesh in bpy.data.meshes
                         if mesh.get('uvmm_native_scratch', False) and not mesh.users), None)
        if snapshot is None:
            snapshot = bpy.data.meshes.new(PREFIX + ' Scratch')
            snapshot['uvmm_native_scratch'] = True
        state.uv_native_scratch = snapshot.name
    bm = bmesh.from_edit_mesh(mesh)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.verts.index_update()
    bm.faces.index_update()
    bm.to_mesh(snapshot)
    if bm.loops.layers.uv.active is not None:
        snapshot.uv_layers.active = snapshot.uv_layers.get(bm.loops.layers.uv.active.name)
    return snapshot


def source_signature(mesh):
    return diagnostic.mesh_source_signature(_mesh_snapshot(mesh))


def _point_stretch(mesh, stretch):
    vertices = np.empty(len(mesh.loops), dtype=np.int32)
    totals = np.empty(len(mesh.polygons), dtype=np.int32)
    mesh.loops.foreach_get('vertex_index', vertices)
    mesh.polygons.foreach_get('loop_total', totals)
    sums = np.bincount(vertices, weights=np.repeat(stretch, totals), minlength=len(mesh.vertices))
    counts = np.bincount(vertices, minlength=len(mesh.vertices))
    return np.divide(sums, counts, out=sums, where=counts > 0).astype(np.float32)


def _write_metrics(obj, prefs, dirty, source_mesh=None):
    mesh = obj.data
    if obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(mesh)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        bm.verts.index_update()
        bm.faces.index_update()
        snapshot = source_mesh if source_mesh is not None else _mesh_snapshot(mesh)
        proxy = SimpleNamespace(data=snapshot, matrix_world=obj.matrix_world)
        stretch, flipped = diagnostic._object_face_stats(
            proxy, snapshot.uv_layers.active, prefs, stats_dirty=True)
        # The object's cached face statistics predate this edit. Its native
        # attributes are current, but a later object-mode style change must
        # not reuse the stale statistics left on the source mesh.
        mesh[diagnostic._STATS_FLAGS_KEY] = 0
        if prefs.uv_show_stretch:
            layer = bm.verts.layers.float.get(STRETCH) or bm.verts.layers.float.new(STRETCH)
            for vert, value in zip(bm.verts, _point_stretch(snapshot, stretch)):
                vert[layer] = float(value)
        if prefs.uv_show_orientation:
            layer = bm.loops.layers.float.get(FLIPPED) or bm.loops.layers.float.new(FLIPPED)
            for face in bm.faces:
                value = float(flipped[face.index])
                for loop in face.loops:
                    loop[layer] = value
        mesh[diagnostic._COLOR_WRITE_TIME_KEY] = time.monotonic()
        mesh[diagnostic._COLOR_ECHO_TOKEN_KEY] = str(time.monotonic_ns())
        bm.normal_update()
        # Only diagnostic attributes changed; do not retriangulate the mesh.
        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
    else:
        stretch, flipped = diagnostic._object_face_stats(
            obj, mesh.uv_layers.active, prefs, stats_dirty=dirty)
        if prefs.uv_show_stretch:
            values = _point_stretch(mesh, stretch)
            attr = mesh.attributes.get(STRETCH) or mesh.attributes.new(STRETCH, 'FLOAT', 'POINT')
            attr.data.foreach_set('value', values.astype(np.float32))
        if prefs.uv_show_orientation:
            totals = np.empty(len(mesh.polygons), dtype=np.int32)
            mesh.polygons.foreach_get('loop_total', totals)
            attr = mesh.attributes.get(FLIPPED) or mesh.attributes.new(FLIPPED, 'FLOAT', 'CORNER')
            attr.data.foreach_set('value', np.repeat(flipped, totals).astype(np.float32))
    mesh[diagnostic._COLOR_WRITE_TIME_KEY] = time.monotonic()
    mesh[diagnostic._COLOR_ECHO_TOKEN_KEY] = str(time.monotonic_ns())
    mesh[diagnostic._DISPLAY_MATRIX_KEY] = tuple(v for row in obj.matrix_world for v in row)


def _ramp(nodes, links, attribute_name, name):
    attribute = nodes.new('ShaderNodeAttribute')
    attribute.attribute_name = attribute_name
    ramp = nodes.new('ShaderNodeValToRGB')
    ramp.name = name
    links.new(attribute.outputs['Fac'], ramp.inputs['Fac'])
    return attribute, ramp


def _build_material(source, uv_name, prefs):
    mat = source.copy() if source else bpy.data.materials.new(PREFIX)
    mat.name = PREFIX + (' ' + source.name if source else '')
    mat['uvmm_native_preview'] = True
    mat.use_fake_user = False
    mat.use_nodes = True
    if source is not None and not source.use_nodes:
        shader = mat.node_tree.nodes.get('Principled BSDF')
        shader.inputs['Base Color'].default_value = source.diffuse_color
        shader.inputs['Metallic'].default_value = source.metallic
        shader.inputs['Roughness'].default_value = source.roughness
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    outputs = [node for node in nodes if node.type == 'OUTPUT_MATERIAL']
    # Wrap every renderer output so Cycles-specific original shaders survive.
    for output in outputs:
        surface = output.inputs['Surface']
        original = surface.links[0].from_socket if surface.is_linked else None
        shader = nodes.new('ShaderNodeBsdfPrincipled')
        shader.inputs['Roughness'].default_value = .48
        mix = nodes.new('ShaderNodeMixShader')
        mix['uvmm_role'] = 'mix'
        mix.label = 'UVMM 原材质 / 检查材质'
        if original:
            links.new(original, mix.inputs[1])
        links.new(shader.outputs['BSDF'], mix.inputs[2])
        links.new(mix.outputs[0], surface)
        if prefs.uv_show_checker:
            mix['uvmm_role'] = 'checker_mix'
            uv = nodes.new('ShaderNodeUVMap')
            uv.uv_map = uv_name
            source_image = None
            if prefs.uv_checker_pattern == 'DIRECTION':
                source_image = diagnostic._file_checker_image(str(diagnostic._DIRECTION_CHECKER_PATH))
            elif prefs.uv_checker_pattern == 'CUSTOM':
                source_image = diagnostic._file_checker_image(prefs.uv_checker_custom_image)
            if source_image:
                scale = nodes.new('ShaderNodeVectorMath')
                scale['uvmm_role'] = 'checker_scale'
                scale.operation = 'SCALE'
                scale.inputs['Scale'].default_value = prefs.uv_checker_density / 8.0
                texture = nodes.new('ShaderNodeTexImage')
                texture.image = source_image
                texture.extension = 'REPEAT'
                links.new(uv.outputs['UV'], scale.inputs[0])
                links.new(scale.outputs['Vector'], texture.inputs['Vector'])
            else:
                texture = nodes.new('ShaderNodeTexChecker')
                texture['uvmm_role'] = 'checker'
                texture.inputs['Scale'].default_value = prefs.uv_checker_density
                texture.inputs['Color1'].default_value = (*prefs.uv_checker_color_a, 1)
                texture.inputs['Color2'].default_value = (*prefs.uv_checker_color_b, 1)
                links.new(uv.outputs['UV'], texture.inputs['Vector'])
            links.new(texture.outputs['Color'], shader.inputs['Base Color'])
            mix.inputs[0].default_value = prefs.uv_checker_opacity
            if not (prefs.uv_show_stretch or prefs.uv_show_orientation):
                continue
            # Checker is the lower inspection layer; UV colors are blended
            # above it. Zero color Alpha reveals the checker/original below.
            checker_surface = mix.outputs[0]
            shader = nodes.new('ShaderNodeBsdfPrincipled')
            shader.inputs['Roughness'].default_value = .48
            mix = nodes.new('ShaderNodeMixShader')
            mix['uvmm_role'] = 'mix'
            mix.label = 'UVMM 棋盘格 / UV 检查颜色'
            links.new(checker_surface, mix.inputs[1])
            links.new(shader.outputs['BSDF'], mix.inputs[2])
            links.new(mix.outputs[0], surface)
        color = alpha = None
        if prefs.uv_show_stretch:
            attribute, ramp = _ramp(nodes, links, STRETCH, 'UVMM Heatmap')
            ramp['uvmm_role'] = 'stretch'
            subtract = nodes.new('ShaderNodeMath')
            subtract['uvmm_role'] = 'threshold'
            subtract.operation = 'SUBTRACT'
            subtract.inputs[1].default_value = prefs.uv_stretch_threshold
            normalize = nodes.new('ShaderNodeMath')
            normalize['uvmm_role'] = 'normalize'
            normalize.operation = 'DIVIDE'
            normalize.use_clamp = True
            normalize.inputs[1].default_value = 1.0 - prefs.uv_stretch_threshold
            power = nodes.new('ShaderNodeMath')
            power['uvmm_role'] = 'strength'
            power.operation = 'POWER'
            power.inputs[1].default_value = 1.0 / prefs.uv_stretch_strength
            links.new(attribute.outputs['Fac'], subtract.inputs[0])
            links.new(subtract.outputs[0], normalize.inputs[0])
            links.new(normalize.outputs[0], power.inputs[0])
            links.new(power.outputs[0], ramp.inputs['Fac'])
            for position in (.25, .5, .75):
                ramp.color_ramp.elements.new(position)
            base, maximum = prefs.uv_material_base_color, prefs.uv_material_max_color
            palette = (base[:3], (0, .82, 1), (.12, 1, .08), (1, .92, 0), maximum[:3])
            for element, rgb in zip(ramp.color_ramp.elements, palette):
                t = element.position
                element.color = (*rgb, base[3] * (1-t) + maximum[3] * t)
            color, alpha = ramp.outputs['Color'], ramp.outputs['Alpha']
        if prefs.uv_show_orientation:
            attribute, ramp = _ramp(nodes, links, FLIPPED, 'UVMM Orientation')
            ramp['uvmm_role'] = 'orientation'
            ramp.color_ramp.interpolation = 'CONSTANT'
            ramp.color_ramp.elements[1].position = .5
            ramp.color_ramp.elements[0].color = prefs.uv_material_front_color
            ramp.color_ramp.elements[1].color = prefs.uv_material_back_color
            if color is not None:
                # Preserve combined stretch + flipped-face inspection.
                blend = nodes.new('ShaderNodeMixRGB')
                links.new(attribute.outputs['Fac'], blend.inputs[0])
                links.new(color, blend.inputs[1])
                links.new(ramp.outputs['Color'], blend.inputs[2])
                blend_alpha = nodes.new('ShaderNodeMixRGB')
                links.new(attribute.outputs['Fac'], blend_alpha.inputs[0])
                links.new(alpha, blend_alpha.inputs[1])
                links.new(ramp.outputs['Alpha'], blend_alpha.inputs[2])
                color, alpha = blend.outputs[0], blend_alpha.outputs[0]
            else:
                color, alpha = ramp.outputs['Color'], ramp.outputs['Alpha']
        links.new(color, shader.inputs['Base Color'])
        links.new(alpha, mix.inputs[0])
    return mat


def _update_uniforms(mat, prefs):
    for node in mat.node_tree.nodes:
        role = node.get('uvmm_role')
        if role == 'stretch':
            base, maximum = prefs.uv_material_base_color, prefs.uv_material_max_color
            palette = (base[:3], (0,.82,1), (.12,1,.08), (1,.92,0), maximum[:3])
            for element, rgb in zip(node.color_ramp.elements, palette):
                t = element.position
                element.color = (*rgb, base[3] * (1-t) + maximum[3] * t)
        elif role == 'orientation':
            node.color_ramp.elements[0].color = prefs.uv_material_front_color
            node.color_ramp.elements[1].color = prefs.uv_material_back_color
        elif role == 'threshold':
            node.inputs[1].default_value = prefs.uv_stretch_threshold
        elif role == 'normalize':
            node.inputs[1].default_value = 1 - prefs.uv_stretch_threshold
        elif role == 'strength':
            node.inputs[1].default_value = 1 / prefs.uv_stretch_strength
        elif role == 'checker_scale':
            node.inputs['Scale'].default_value = prefs.uv_checker_density / 8
        elif role == 'checker':
            node.inputs['Scale'].default_value = prefs.uv_checker_density
            node.inputs['Color1'].default_value = (*prefs.uv_checker_color_a, 1)
            node.inputs['Color2'].default_value = (*prefs.uv_checker_color_b, 1)
        elif role == 'checker_mix':
            node.inputs[0].default_value = prefs.uv_checker_opacity


def update_material_uniforms(prefs):
    """Update cached preview shader inputs without walking scene meshes."""
    updated = set()
    for scene in bpy.data.scenes:
        if not hasattr(scene, 'model_checker_state'):
            continue
        for item in scene.model_checker_state.uv_native_materials:
            material = bpy.data.materials.get(item.material)
            if (material is None or material.as_pointer() in updated
                    or not material.get('uvmm_native_preview', False)
                    or not material.use_nodes):
                continue
            _update_uniforms(material, prefs)
            updated.add(material.as_pointer())


def _material(source, uv_name, prefs, state):
    source_key = repr((source.session_uid, source.name_full,
                       source.library.filepath if source.library else '')) if source else ''
    key = repr((uv_name, prefs.uv_show_checker, prefs.uv_show_stretch,
                prefs.uv_show_orientation, prefs.uv_checker_pattern,
                prefs.uv_checker_custom_image))
    signature = repr((uv_name, prefs.uv_show_checker, prefs.uv_show_stretch,
        prefs.uv_show_orientation, tuple(prefs.uv_material_base_color),
        tuple(prefs.uv_material_max_color), tuple(prefs.uv_material_front_color),
        tuple(prefs.uv_material_back_color), prefs.uv_stretch_strength,
        prefs.uv_stretch_threshold, prefs.uv_checker_pattern, prefs.uv_checker_density,
        prefs.uv_checker_custom_image, prefs.uv_checker_opacity,
        tuple(prefs.uv_checker_color_a), tuple(prefs.uv_checker_color_b)))
    for item in state.uv_native_materials:
        material = bpy.data.materials.get(item.material)
        if (item.source == source_key and item.key == key and material is not None
                and material.get('uvmm_native_preview', False)):
            if item.signature != signature:
                _update_uniforms(material, prefs)
                item.signature = signature
            return material
    item = state.uv_native_materials.add()
    item.source = source_key
    item.key = key
    item.signature = signature
    material = _build_material(source, uv_name, prefs)
    item.material = material.name
    return material


def ensure_preview(obj, prefs, dirty=False):
    state = bpy.context.scene.model_checker_state
    mesh = obj.data
    entry = diagnostic._find_mesh_state(state, mesh)
    if entry is None:
        entry = state.uv_mesh_states.add()
        entry.mesh = mesh
    signature = repr((mesh.uv_layers.active.name, prefs.uv_show_stretch,
                      prefs.uv_show_orientation, tuple(v for row in obj.matrix_world for v in row)))
    changed_style_or_transform = entry.native_signature != signature
    needs_metrics = prefs.uv_show_stretch or prefs.uv_show_orientation
    if needs_metrics and (dirty or changed_style_or_transform or entry.native_force_refresh):
        source_mesh = _mesh_snapshot(mesh)
        source = diagnostic.mesh_source_signature(source_mesh)
        changed_inputs = source != entry.native_source_signature
        if (prefs.uv_show_stretch or prefs.uv_show_orientation) and (
                changed_inputs or changed_style_or_transform or entry.native_force_refresh):
            _write_metrics(obj, prefs, changed_inputs or entry.native_force_refresh, source_mesh)
        entry.native_source_signature = source
        entry.native_signature = signature
        entry.native_force_refresh = False
    elif not needs_metrics:
        # A checker-only preview samples the active UV map directly in its
        # material. It does not need mesh signatures or CPU diagnostic metrics.
        entry.native_source_signature = ''
        entry.native_signature = signature
        entry.native_force_refresh = False
    return _prepare_bindings(obj, prefs)


def prepare_display(context, prefs):
    """Create ID relationships inside the operator's undo transaction.

    Only heavy mesh metrics are deferred. Creating materials/node groups in
    a later timer leaves them outside the step F9 is about to restore.
    """
    active = prefs.uv_display_mode == 'MATERIAL' and any((
        prefs.uv_show_stretch,prefs.uv_show_orientation,prefs.uv_show_checker))
    for obj in tuple(context.scene.objects):
        if obj.type != 'MESH':
            continue
        if active and obj.visible_get(view_layer=context.view_layer) and obj.data.uv_layers.active:
            diagnostic._remove_legacy_preview(obj)
            _prepare_bindings(obj,prefs)
        else:
            remove_preview(obj)


def _prepare_bindings(obj, prefs):
    state = bpy.context.scene.model_checker_state
    mesh = obj.data
    materials = [_material(slot.material, mesh.uv_layers.active.name, prefs, state)
                 for slot in obj.material_slots]
    if not materials:
        materials = [_material(None, mesh.uv_layers.active.name, prefs, state)]
    modifier = modifier_for(obj)
    binding = repr(tuple(mat.as_pointer() for mat in materials))
    if modifier is not None and modifier.node_group.get('uvmm_binding') == binding:
        return True
    remove_preview(obj)
    group = next((g for g in bpy.data.node_groups
                  if g.get('uvmm_native_preview', False)
                  and g.get('uvmm_binding') == binding), None)
    if group is not None:
        modifier = obj.modifiers.new('UV 检查 · 材质预览（临时）', 'NODES')
        modifier.node_group = group
        modifier.show_render = False
        modifier.show_in_editmode = True
        return True
    group = bpy.data.node_groups.new(PREFIX, 'GeometryNodeTree')
    group['uvmm_native_preview'] = True
    group['uvmm_binding'] = binding
    group.interface.new_socket(name='Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
    group.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
    nodes, links = group.nodes, group.links
    input_node = nodes.new('NodeGroupInput')
    output_node = nodes.new('NodeGroupOutput')
    if len(materials) == 1:
        # No slot selection is needed; avoid capturing/allocating a face-index
        # attribute every time a dense single-material edit mesh is evaluated.
        assign = nodes.new('GeometryNodeSetMaterial')
        assign.inputs['Material'].default_value = materials[0]
        links.new(input_node.outputs['Geometry'], assign.inputs['Geometry'])
        geometry = assign.outputs['Geometry']
    else:
        capture = nodes.new('GeometryNodeCaptureAttribute')
        capture.domain = 'FACE'
        capture.capture_items.new('INT', 'Slot')
        index = nodes.new('GeometryNodeInputMaterialIndex')
        links.new(index.outputs[0], capture.inputs['Slot'])
        links.new(input_node.outputs['Geometry'], capture.inputs['Geometry'])
        geometry = capture.outputs['Geometry']
        for slot_index, mat in enumerate(materials):
            compare = nodes.new('FunctionNodeCompare')
            compare.data_type = 'INT'
            compare.operation = 'EQUAL'
            links.new(capture.outputs['Slot'], compare.inputs['A'])
            compare.inputs['B'].default_value = slot_index
            assign = nodes.new('GeometryNodeSetMaterial')
            assign.inputs['Material'].default_value = mat
            links.new(geometry, assign.inputs['Geometry'])
            links.new(compare.outputs['Result'], assign.inputs['Selection'])
            geometry = assign.outputs['Geometry']
    links.new(geometry, output_node.inputs['Geometry'])
    modifier = obj.modifiers.new('UV 检查 · 材质预览（临时）', 'NODES')
    modifier.node_group = group
    modifier.show_render = False
    modifier.show_in_editmode = True
    return True
