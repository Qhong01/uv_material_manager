"""Viewport UV diagnostics and a temporary image-texture checker preview."""

import math
import time
from array import array

import bpy
import bmesh
from mathutils import Vector


_DISPLAY_ATTR_KEY = "_model_checker_display_attribute"
_DISPLAY_SIGNATURE_KEY = "_model_checker_display_signature"
_PREVIOUS_COLOR_KEY = "_model_checker_previous_color_attribute"
_PREVIOUS_COLOR_HAD_KEY = "_model_checker_previous_color_had_value"
_DISPLAY_ATTR_BASE = "__ModelChecker_UVDisplay"
_LEGACY_GROUP_PREFIX = ".Model Checker UV -"
_GROUP_MATERIAL_KEY = "_model_checker_material"
_GROUP_SIGNATURE_KEY = "_model_checker_checker_signature"
_CHECKER_IMAGE_TAG = "_model_checker_temporary_checker_image"
_CHECKER_IMAGE_SIGNATURE = "_model_checker_checker_image_signature"
_LEGACY_MESH_KEYS = (
    "_model_checker_stretch_attribute",
    "_model_checker_flipped_attribute",
)


def _feature_enabled(prefs):
    return any((prefs.uv_show_stretch, prefs.uv_show_checker, prefs.uv_show_orientation))


def visible_meshes(scene, view_layer):
    """Return mesh objects currently visible in the active view layer."""
    return tuple(
        obj for obj in scene.objects
        if obj.type == 'MESH' and obj.visible_get(view_layer=view_layer)
    )


def _legacy_preview_modifier(obj):
    return next((
        mod for mod in obj.modifiers
        if mod.type == 'NODES'
        and mod.node_group is not None
        and mod.node_group.name.startswith(_LEGACY_GROUP_PREFIX)
    ), None)


def _new_attribute_name(mesh, base_name):
    name = base_name
    suffix = 1
    while mesh.color_attributes.get(name) is not None:
        name = f"{base_name}.{suffix:03d}"
        suffix += 1
    return name


def _ensure_display_attribute(mesh, edit_mode=False):
    if _PREVIOUS_COLOR_HAD_KEY not in mesh:
        previous = mesh.color_attributes.active_color
        mesh[_PREVIOUS_COLOR_HAD_KEY] = previous is not None
        mesh[_PREVIOUS_COLOR_KEY] = previous.name if previous is not None else ""
    name = mesh.get(_DISPLAY_ATTR_KEY, "")
    attribute = mesh.color_attributes.get(name) if name else None
    if attribute is None or attribute.domain != 'CORNER' or attribute.data_type != 'FLOAT_COLOR':
        if attribute is not None:
            mesh.color_attributes.remove(attribute)
        name = _new_attribute_name(mesh, _DISPLAY_ATTR_BASE)
        if edit_mode:
            bm = bmesh.from_edit_mesh(mesh)
            bm.loops.layers.float_color.new(name)
            bmesh.update_edit_mesh(mesh, loop_triangles=True, destructive=False)
            attribute = mesh.color_attributes.get(name)
        else:
            attribute = mesh.color_attributes.new(name=name, type='FLOAT_COLOR', domain='CORNER')
        mesh[_DISPLAY_ATTR_KEY] = name
    mesh.color_attributes.active_color = attribute
    return name, attribute


def _triangle_stretch(coords, uvs):
    edge_1 = coords[1] - coords[0]
    edge_2 = coords[2] - coords[0]
    length_1 = edge_1.length
    if length_1 < 1e-6:
        return 1.0
    axis_x = edge_1 / length_1
    projected_x = edge_2.dot(axis_x)
    orthogonal = edge_2 - axis_x * projected_x
    length_y = orthogonal.length
    if length_y < 1e-6:
        return 1.0
    uv_1 = uvs[1] - uvs[0]
    uv_2 = uvs[2] - uvs[0]
    a = uv_1.x / length_1
    b = uv_2.x / length_y - uv_1.x * projected_x / (length_1 * length_y)
    c = uv_1.y / length_1
    d = uv_2.y / length_y - uv_1.y * projected_x / (length_1 * length_y)
    xx = a * a + c * c
    yy = b * b + d * d
    xy = a * b + c * d
    discriminant = math.sqrt(max((xx - yy) ** 2 + 4.0 * xy * xy, 0.0))
    largest = math.sqrt(max((xx + yy + discriminant) * 0.5, 1e-16))
    smallest = math.sqrt(max((xx + yy - discriminant) * 0.5, 1e-16))
    signed_area = uv_1.x * uv_2.y - uv_1.y * uv_2.x
    if abs(signed_area) < 1e-12:
        return 1.0
    return min(abs(math.log2(largest / smallest)) / 4.0, 1.0)


def _stretch_color(amount, prefs):
    threshold = min(max(float(prefs.uv_stretch_threshold), 0.0), 0.999)
    if amount <= threshold + 1e-6:
        amount = 0.0
    else:
        amount = (amount - threshold) / max(1.0 - threshold, 1e-6)
    strength = max(float(prefs.uv_stretch_strength), 0.25)
    factor = min(max(amount, 0.0), 1.0) ** (1.0 / strength)
    return _mix_colors(prefs.uv_stretch_base_color, prefs.uv_stretch_max_color, factor)


def _mix_colors(first, second, factor):
    return tuple(a + (b - a) * factor for a, b in zip(first, second))


def _face_base_color(stretch, flipped, prefs):
    # Keep the face's diagnostic category unambiguous when both checks are on:
    # flipped faces use the orientation color, while all other faces use the
    # stretch heatmap.
    if prefs.uv_show_orientation and flipped:
        base = _mix_colors(
            (0.38, 0.38, 0.38),
            prefs.uv_orientation_back_color,
            float(prefs.uv_orientation_opacity),
        )
    elif prefs.uv_show_stretch:
        base = _mix_colors(
            (0.38, 0.38, 0.38),
            _stretch_color(stretch, prefs),
            float(prefs.uv_stretch_opacity),
        )
    elif prefs.uv_show_orientation:
        base = _mix_colors(
            (0.38, 0.38, 0.38),
            prefs.uv_orientation_front_color,
            float(prefs.uv_orientation_opacity),
        )
    else:
        base = (0.38, 0.38, 0.38)
    return base


def _loop_display_color(base):
    return (*base, 1.0)


def _update_edit_mesh_colors(obj, layer_name, display_name, prefs):
    mesh = obj.data
    bm = bmesh.from_edit_mesh(mesh)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.verts.index_update()
    bm.faces.index_update()
    uv_layer = bm.loops.layers.uv.get(layer_name)
    color_layer = bm.loops.layers.float_color.get(display_name)
    if uv_layer is None or color_layer is None:
        return False

    max_stretch = [0.0] * len(bm.faces)
    flipped_votes = [0] * len(bm.faces)
    triangle_counts = [0] * len(bm.faces)
    for triangle in bm.calc_loop_triangles():
        loops = tuple(triangle)
        face_index = loops[0].face.index
        coords = tuple(obj.matrix_world @ loop.vert.co for loop in loops)
        uvs = tuple(Vector(loop[uv_layer].uv) for loop in loops)
        max_stretch[face_index] = max(max_stretch[face_index], _triangle_stretch(coords, uvs))
        uv_a = uvs[1] - uvs[0]
        uv_b = uvs[2] - uvs[0]
        if uv_a.x * uv_b.y - uv_a.y * uv_b.x < -1e-12:
            flipped_votes[face_index] += 1
        triangle_counts[face_index] += 1

    for face in bm.faces:
        face_index = face.index
        flipped = triangle_counts[face_index] > 0 and flipped_votes[face_index] * 2 >= triangle_counts[face_index]
        base = _face_base_color(max_stretch[face_index], flipped, prefs)
        for loop in face.loops:
            loop[color_layer] = _loop_display_color(base)
    bmesh.update_edit_mesh(mesh, loop_triangles=True, destructive=False)
    return True


def _uv_finding_faces(obj, bm, prefs):
    """Return UV-problem faces without changing mesh or selection state."""
    check_stretch = bool(prefs.uv_show_stretch)
    check_flipped = bool(prefs.uv_show_orientation)
    if not (check_stretch or check_flipped):
        return ()

    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    active_uv = obj.data.uv_layers.active
    uv_layer = bm.loops.layers.uv.get(active_uv.name) if active_uv is not None else None
    if uv_layer is None:
        return ()

    max_stretch = [0.0] * len(bm.faces)
    flipped_votes = [0] * len(bm.faces)
    triangle_counts = [0] * len(bm.faces)
    for triangle in bm.calc_loop_triangles():
        loops = tuple(triangle)
        face_index = loops[0].face.index
        world_coords = tuple(obj.matrix_world @ loop.vert.co for loop in loops)
        uvs = tuple(Vector(loop[uv_layer].uv) for loop in loops)
        max_stretch[face_index] = max(
            max_stretch[face_index],
            _triangle_stretch(world_coords, uvs),
        )
        uv_a = uvs[1] - uvs[0]
        uv_b = uvs[2] - uvs[0]
        if uv_a.x * uv_b.y - uv_a.y * uv_b.x < -1e-12:
            flipped_votes[face_index] += 1
        triangle_counts[face_index] += 1

    findings = []
    for face in bm.faces:
        index = face.index
        stretched = check_stretch and max_stretch[index] > float(prefs.uv_stretch_threshold) + 1e-6
        flipped = (
            check_flipped
            and triangle_counts[index] > 0
            and flipped_votes[index] * 2 >= triangle_counts[index]
        )
        if stretched or flipped:
            findings.append(face)
    return tuple(findings)


def count_uv_findings(objects, prefs):
    """Count UV problem faces without changing mode or selection."""
    unique_meshes = {}
    for obj in objects:
        if obj.type == 'MESH':
            unique_meshes.setdefault(obj.data, obj)

    found_faces = 0
    for mesh, obj in unique_meshes.items():
        if obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(mesh)
            found_faces += len(_uv_finding_faces(obj, bm, prefs))
        else:
            bm = bmesh.new()
            try:
                bm.from_mesh(mesh)
                found_faces += len(_uv_finding_faces(obj, bm, prefs))
            finally:
                bm.free()
    return found_faces


def select_uv_findings_in_edit_mode(objects, prefs):
    """Select UV issue faces across the supplied edit-mode meshes."""
    unique_meshes = {}
    for obj in objects:
        if obj.type == 'MESH':
            unique_meshes.setdefault(obj.data, obj)

    # A zero-result check must not clear a user's existing component selection.
    if count_uv_findings(tuple(unique_meshes.values()), prefs) == 0:
        return 0

    found_faces = 0
    for mesh, obj in unique_meshes.items():
        bm = bmesh.from_edit_mesh(mesh)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        for face in bm.faces:
            face.select_set(False)
        for edge in bm.edges:
            edge.select_set(False)
        for vertex in bm.verts:
            vertex.select_set(False)

        findings = _uv_finding_faces(obj, bm, prefs)
        for face in findings:
            face.select_set(True)
        found_faces += len(findings)
        bm.normal_update()
        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
    return found_faces


def _update_object_mesh_colors(obj, uv_layer, display_attr, prefs):
    mesh = obj.data
    mesh.calc_loop_triangles()
    max_stretch = [0.0] * len(mesh.polygons)
    flipped_votes = [0] * len(mesh.polygons)
    triangle_counts = [0] * len(mesh.polygons)
    for triangle in mesh.loop_triangles:
        loop_indices = tuple(triangle.loops)
        coords = tuple(
            obj.matrix_world @ mesh.vertices[mesh.loops[index].vertex_index].co
            for index in loop_indices
        )
        uvs = tuple(Vector(uv_layer.data[index].uv) for index in loop_indices)
        polygon_index = triangle.polygon_index
        max_stretch[polygon_index] = max(max_stretch[polygon_index], _triangle_stretch(coords, uvs))
        uv_a = uvs[1] - uvs[0]
        uv_b = uvs[2] - uvs[0]
        if uv_a.x * uv_b.y - uv_a.y * uv_b.x < -1e-12:
            flipped_votes[polygon_index] += 1
        triangle_counts[polygon_index] += 1

    for polygon in mesh.polygons:
        flipped = triangle_counts[polygon.index] > 0 and flipped_votes[polygon.index] * 2 >= triangle_counts[polygon.index]
        base = _face_base_color(max_stretch[polygon.index], flipped, prefs)
        for loop_index in polygon.loop_indices:
            display_attr.data[loop_index].color = _loop_display_color(base)
    mesh.update()
    return True


def _remove_legacy_preview(obj):
    modifier = _legacy_preview_modifier(obj)
    if modifier is None:
        return
    group = modifier.node_group
    material_name = group.get("_model_checker_material", "") if group is not None else ""
    material = bpy.data.materials.get(material_name)
    obj.modifiers.remove(modifier)
    if group is not None and group.users == 0:
        bpy.data.node_groups.remove(group)
    if material is not None and material.users == 0:
        bpy.data.materials.remove(material)


def _checker_image(prefs):
    density = min(max(int(prefs.uv_checker_density), 1), 256)
    signature = repr((
        "lowres-v2",
        density,
        float(prefs.uv_checker_opacity),
        tuple(prefs.uv_checker_color_a),
        tuple(prefs.uv_checker_color_b),
    ))
    image = next((
        candidate for candidate in bpy.data.images
        if candidate.get(_CHECKER_IMAGE_TAG, False)
    ), None)
    size = density * 2
    if image is None:
        image = bpy.data.images.new(
            ".Model Checker UV 棋盘格",
            width=size,
            height=size,
            alpha=False,
            float_buffer=False,
        )
        image[_CHECKER_IMAGE_TAG] = True
        image.colorspace_settings.name = 'Non-Color'
    elif tuple(image.size) != (size, size):
        image.scale(size, size)
    if image.get(_CHECKER_IMAGE_SIGNATURE, "") == signature:
        return image

    opacity = min(max(float(prefs.uv_checker_opacity), 0.0), 1.0)
    background = (0.38, 0.38, 0.38)
    colors = []
    for color in (prefs.uv_checker_color_a, prefs.uv_checker_color_b):
        colors.append(array('f', (
            *(background[channel] + (float(color[channel]) - background[channel]) * opacity
              for channel in range(3)),
            1.0,
        )))

    even_row = array('f')
    odd_row = array('f')
    for cell_x in range(density):
        even_row.extend(colors[cell_x % 2] * 2)
        odd_row.extend(colors[(cell_x + 1) % 2] * 2)

    pixels = array('f')
    for cell_y in range(density):
        row = even_row if cell_y % 2 == 0 else odd_row
        pixels.extend(row)
        pixels.extend(row)
    image.pixels.foreach_set(pixels)
    image.update()
    image[_CHECKER_IMAGE_SIGNATURE] = signature
    return image


def _remove_checker_images():
    for image in tuple(bpy.data.images):
        if image.get(_CHECKER_IMAGE_TAG, False) and image.users == 0:
            bpy.data.images.remove(image)


def _build_checker_material(material, uv_name, display_name, prefs):
    material.use_nodes = True
    material.node_tree.nodes.clear()
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    principled = nodes.new('ShaderNodeBsdfPrincipled')
    principled.inputs['Roughness'].default_value = 0.72
    output = nodes.new('ShaderNodeOutputMaterial')

    surface_color = None
    if prefs.uv_show_checker:
        uv_map = nodes.new('ShaderNodeUVMap')
        uv_map.uv_map = uv_name
        texture = nodes.new('ShaderNodeTexImage')
        texture.image = _checker_image(prefs)
        texture.interpolation = 'Closest'
        texture.extension = 'REPEAT'
        texture.select = True
        nodes.active = texture
        links.new(uv_map.outputs['UV'], texture.inputs['Vector'])
        surface_color = texture.outputs['Color']

    if display_name and (prefs.uv_show_stretch or prefs.uv_show_orientation):
        diagnostic_color = nodes.new('ShaderNodeVertexColor')
        diagnostic_color.layer_name = display_name
        if surface_color is None:
            surface_color = diagnostic_color.outputs['Color']
        else:
            mix = nodes.new('ShaderNodeMixRGB')
            mix.blend_type = 'MIX'
            mix.inputs['Fac'].default_value = 0.68
            links.new(surface_color, mix.inputs['Color1'])
            links.new(diagnostic_color.outputs['Color'], mix.inputs['Color2'])
            surface_color = mix.outputs['Color']

    if surface_color is not None:
        links.new(surface_color, principled.inputs['Base Color'])
    links.new(principled.outputs['BSDF'], output.inputs['Surface'])


def _ensure_checker_modifier(obj, uv_name, display_name, prefs):
    modifier = _legacy_preview_modifier(obj)
    if modifier is None:
        material = bpy.data.materials.new(f".Model Checker UV - {obj.name}")
        group = bpy.data.node_groups.new(f".Model Checker UV - {obj.name}", 'GeometryNodeTree')
        group.is_modifier = True
        group.interface.new_socket(name="Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
        group.interface.new_socket(name="Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
        group.nodes.clear()
        group_input = group.nodes.new('NodeGroupInput')
        group_output = group.nodes.new('NodeGroupOutput')
        set_material = group.nodes.new('GeometryNodeSetMaterial')
        set_material.inputs['Material'].default_value = material
        group.links.new(group_input.outputs['Geometry'], set_material.inputs['Geometry'])
        group.links.new(set_material.outputs['Geometry'], group_output.inputs['Geometry'])
        modifier = obj.modifiers.new("模型检查 UV 棋盘格", 'NODES')
        modifier.node_group = group
        modifier.show_render = False
        modifier.show_in_editmode = True
        group[_GROUP_MATERIAL_KEY] = material.name
    modifier.show_render = False
    modifier.show_in_editmode = True
    group = modifier.node_group
    material_name = group.get(_GROUP_MATERIAL_KEY, "")
    material = bpy.data.materials.get(material_name)
    if material is None:
        material = bpy.data.materials.new(f".Model Checker UV - {obj.name}")
        group[_GROUP_MATERIAL_KEY] = material.name
        for node in group.nodes:
            if node.bl_idname == 'GeometryNodeSetMaterial':
                node.inputs['Material'].default_value = material
                break
    signature = repr((
        "material-preview-composite-v2-lowres-checker",
        uv_name,
        display_name,
        bool(prefs.uv_show_checker),
        bool(prefs.uv_show_stretch),
        bool(prefs.uv_show_orientation),
        int(prefs.uv_checker_density),
        float(prefs.uv_checker_opacity),
        tuple(prefs.uv_checker_color_a),
        tuple(prefs.uv_checker_color_b),
        tuple(prefs.uv_stretch_base_color),
        tuple(prefs.uv_stretch_max_color),
        float(prefs.uv_stretch_strength),
        float(prefs.uv_stretch_threshold),
        float(prefs.uv_stretch_opacity),
        tuple(prefs.uv_orientation_front_color),
        tuple(prefs.uv_orientation_back_color),
        float(prefs.uv_orientation_opacity),
    ))
    if group.get(_GROUP_SIGNATURE_KEY, "") != signature:
        _build_checker_material(material, uv_name, display_name, prefs)
        group[_GROUP_SIGNATURE_KEY] = signature
    return modifier


def _restore_active_color_attribute(mesh, display_name):
    current = mesh.color_attributes.active_color
    if current is not None and current.name != display_name:
        return
    had_previous = bool(mesh.get(_PREVIOUS_COLOR_HAD_KEY, False))
    previous_name = mesh.get(_PREVIOUS_COLOR_KEY, "")
    previous = mesh.color_attributes.get(previous_name) if had_previous else None
    if previous is not None:
        mesh.color_attributes.active_color = previous


def _remove_diagnostic_attributes(mesh):
    display_name = mesh.get(_DISPLAY_ATTR_KEY, "")
    if display_name:
        _restore_active_color_attribute(mesh, display_name)
        attribute = mesh.color_attributes.get(display_name)
        if attribute is not None:
            mesh.color_attributes.remove(attribute)
    for key in (_DISPLAY_ATTR_KEY, _DISPLAY_SIGNATURE_KEY, _PREVIOUS_COLOR_KEY, _PREVIOUS_COLOR_HAD_KEY):
        if key in mesh:
            del mesh[key]
    for key in _LEGACY_MESH_KEYS:
        name = mesh.get(key)
        attribute = mesh.attributes.get(name) if name else None
        if attribute is not None:
            mesh.attributes.remove(attribute)
        if key in mesh:
            del mesh[key]


def _ensure_preview(obj, prefs, force_update=False):
    mesh = obj.data
    active_uv = mesh.uv_layers.active
    if active_uv is None:
        _remove_legacy_preview(obj)
        _remove_diagnostic_attributes(mesh)
        return False
    active_uv_name = active_uv.name
    needs_color_diagnostics = bool(prefs.uv_show_stretch or prefs.uv_show_orientation)
    if needs_color_diagnostics:
        display_name, display_attr = _ensure_display_attribute(mesh, edit_mode=obj.mode == 'EDIT')
    else:
        display_name = ""
        display_attr = None
        _remove_diagnostic_attributes(mesh)
    signature = repr((
        active_uv_name,
        bool(prefs.uv_show_stretch),
        bool(prefs.uv_show_checker),
        bool(prefs.uv_show_orientation),
        tuple(prefs.uv_stretch_base_color),
        tuple(prefs.uv_stretch_max_color),
        float(prefs.uv_stretch_strength),
        float(prefs.uv_stretch_threshold),
        float(prefs.uv_stretch_opacity),
        float(prefs.uv_checker_opacity),
        int(prefs.uv_checker_density),
        tuple(prefs.uv_checker_color_a),
        tuple(prefs.uv_checker_color_b),
        float(prefs.uv_orientation_opacity),
        tuple(prefs.uv_orientation_front_color),
        tuple(prefs.uv_orientation_back_color),
        tuple(value for row in obj.matrix_world for value in row),
    ))
    if not needs_color_diagnostics:
        updated = True
    elif not force_update and mesh.get(_DISPLAY_SIGNATURE_KEY, "") == signature:
        updated = True
    elif obj.mode == 'EDIT':
        updated = False
        for _attempt in range(3):
            updated = _update_edit_mesh_colors(obj, active_uv_name, display_name, prefs)
            if updated:
                break
    else:
        current_uv = mesh.uv_layers.get(active_uv_name)
        updated = current_uv is not None
        if updated:
            _update_object_mesh_colors(obj, current_uv, display_attr, prefs)
    if updated:
        if needs_color_diagnostics:
            mesh[_DISPLAY_SIGNATURE_KEY] = signature
    if prefs.uv_show_checker:
        checker_modifier = _ensure_checker_modifier(obj, active_uv_name, display_name, prefs)
        checker_modifier.show_viewport = not needs_color_diagnostics
    else:
        _remove_legacy_preview(obj)
    return updated


def refresh_timer():
    context = bpy.context
    scene = context.scene
    if scene is None or not hasattr(scene, "model_checker_state"):
        return None
    state = scene.model_checker_state
    addon = context.preferences.addons.get(__package__.split('.')[0])
    if addon is None:
        cleanup_all()
        return None
    prefs = addon.preferences
    active = _feature_enabled(prefs)
    if not state.uv_display_dirty:
        if not active:
            restore_viewport_mode(scene)
        return None
    visible_objects = set(visible_meshes(scene, context.view_layer)) if active else set()
    visible_mesh_data = {obj.data for obj in visible_objects}
    window = context.window
    edit_transform_active = (
        context.mode == 'EDIT_MESH'
        and _edit_transform_active(window)
    )
    defer_preview_refresh = (
        state.uv_display_dirty
        and (
            time.monotonic() < state.uv_display_defer_until
            or edit_transform_active
        )
    )
    if defer_preview_refresh:
        return 0.25
    objects = tuple(scene.objects)
    start = min(state.uv_refresh_cursor, len(objects))
    stop = min(start + 8, len(objects))
    refresh_pending = stop < len(objects)
    state.refresh_in_progress = True
    try:
        for obj in objects[start:stop]:
            if obj.type != 'MESH':
                continue
            if obj in visible_objects:
                _ensure_preview(
                    obj,
                    prefs,
                    force_update=state.uv_display_dirty,
                )
            elif mesh_has_diagnostic(obj.data) or _legacy_preview_modifier(obj) is not None:
                _remove_legacy_preview(obj)
                if obj.data not in visible_mesh_data:
                    _remove_diagnostic_attributes(obj.data)

        if refresh_pending:
            state.uv_refresh_cursor = stop
        else:
            state.uv_refresh_cursor = 0
            state.uv_display_dirty = False
    finally:
        state.refresh_in_progress = False
    if not refresh_pending:
        _remove_checker_images()
    if refresh_pending:
        return 0.05
    if active:
        return None
    restore_viewport_mode(scene)
    return None


def mesh_has_diagnostic(mesh):
    return bool(mesh.get(_DISPLAY_ATTR_KEY)) or any(mesh.get(key) for key in _LEGACY_MESH_KEYS)


def start_refresh_timer():
    if not bpy.app.timers.is_registered(refresh_timer):
        bpy.app.timers.register(refresh_timer, first_interval=0.1)


def stop_refresh_timer():
    if bpy.app.timers.is_registered(refresh_timer):
        bpy.app.timers.unregister(refresh_timer)


def _edit_transform_active(window):
    if window is None:
        return False
    return any(
        getattr(operator, "bl_idname", "").startswith("TRANSFORM_OT_")
        for operator in window.modal_operators
    )


def set_viewport_mode(context, enabled, display_color_type='VERTEX'):
    if context.area is None or context.area.type != 'VIEW_3D' or context.scene is None:
        return
    state = context.scene.model_checker_state
    shading = context.area.spaces.active.shading
    if enabled:
        desired_type = 'SOLID'
        if (shading.type != desired_type or shading.color_type != display_color_type) and not state.preview_shading_saved:
            state.preview_area_pointer = str(context.area.as_pointer())
            state.preview_shading_type = shading.type
            state.preview_shading_color_type = shading.color_type
            state.preview_shading_saved = True
        shading.type = desired_type
        shading.color_type = display_color_type
        return
    restore_viewport_mode(context.scene, context.area)


def sync_viewport_mode(scene, enabled, display_color_type='VERTEX'):
    """Update the viewport that entered diagnostics outside an operator context."""
    state = scene.model_checker_state if hasattr(scene, 'model_checker_state') else None
    if state is None or not state.preview_shading_saved:
        return
    if not enabled:
        restore_viewport_mode(scene)
        return
    target_pointer = state.preview_area_pointer
    manager = bpy.context.window_manager
    if manager is None:
        return
    for window in manager.windows:
        if window.screen is None:
            continue
        for area in window.screen.areas:
            if area.type != 'VIEW_3D' or str(area.as_pointer()) != target_pointer:
                continue
            shading = area.spaces.active.shading
            shading.type = 'SOLID'
            shading.color_type = display_color_type
            area.tag_redraw()
            return


def remove_checker_previews():
    """Remove temporary checker modifiers without changing material slots."""
    for obj in tuple(bpy.data.objects):
        if obj.type == 'MESH':
            _remove_legacy_preview(obj)
    _remove_checker_images()


def restore_viewport_mode(scene, preferred_area=None):
    state = scene.model_checker_state if hasattr(scene, 'model_checker_state') else None
    if state is None or not state.preview_shading_saved:
        return
    target_pointer = state.preview_area_pointer
    areas = []
    if preferred_area is not None:
        areas.append(preferred_area)
    manager = bpy.context.window_manager
    if manager is not None:
        for window in manager.windows:
            if window.screen is not None:
                areas.extend(area for area in window.screen.areas if area.type == 'VIEW_3D')
    for area in areas:
        if str(area.as_pointer()) == target_pointer and area.type == 'VIEW_3D':
            shading = area.spaces.active.shading
            shading.type = state.preview_shading_type
            shading.color_type = state.preview_shading_color_type
            break
    state.preview_shading_saved = False
    state.preview_area_pointer = ""


def cleanup_all():
    stop_refresh_timer()
    for obj in tuple(bpy.data.objects):
        if obj.type != 'MESH':
            continue
        _remove_legacy_preview(obj)
        if mesh_has_diagnostic(obj.data):
            _remove_diagnostic_attributes(obj.data)
    for scene in bpy.data.scenes:
        restore_viewport_mode(scene)
    _remove_checker_images()
