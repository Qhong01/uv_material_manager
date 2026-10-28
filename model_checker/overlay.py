"""Viewport visualization for the active UV layer."""

import bpy
import bmesh
import gpu
import time
from gpu_extras.batch import batch_for_shader
from mathutils import Vector
from bpy.app.handlers import persistent

_DRAW_HANDLE = None
_DRAW_CACHE = {}


def _active_uv_layer(obj, bm):
    """Resolve the mesh's active UV map explicitly, including in Edit Mode."""
    active_layer = obj.data.uv_layers.active
    if active_layer is None:
        return None
    return bm.loops.layers.uv.get(active_layer.name)


def _edit_transform_active(window):
    if window is None:
        return False
    return any(
        getattr(operator, "bl_idname", "").startswith("TRANSFORM_OT_")
        for operator in window.modal_operators
    )


def _collect_boundary_edges(obj, lines):
    mesh = obj.data
    if mesh.uv_layers.active is None:
        return
    bm = bmesh.from_edit_mesh(mesh) if obj.mode == 'EDIT' else bmesh.new()
    if obj.mode != 'EDIT':
        bm.from_mesh(mesh)
    bm.normal_update()
    uv_layer = _active_uv_layer(obj, bm)
    if uv_layer is None:
        if obj.mode != 'EDIT':
            bm.free()
        return
    matrix = obj.matrix_world
    normal_matrix = matrix.to_3x3().inverted_safe().transposed()
    # Keep boundary lines just in front of the mesh surface.
    offset = max(obj.dimensions.length * 3e-4, 1e-5)
    for edge in bm.edges:
        loops = tuple(edge.link_loops)
        if not loops:
            continue
        is_boundary = len(loops) == 1
        if not is_boundary:
            def edge_uvs(loop):
                next_loop = loop.link_loop_next
                if loop.vert == edge.verts[0]:
                    return loop[uv_layer].uv, next_loop[uv_layer].uv
                return next_loop[uv_layer].uv, loop[uv_layer].uv

            first_a, first_b = edge_uvs(loops[0])
            for other in loops[1:]:
                other_a, other_b = edge_uvs(other)
                if (first_a - other_a).length_squared > 1e-12 or (first_b - other_b).length_squared > 1e-12:
                    is_boundary = True
                    break
        if is_boundary:
            normal = Vector((0.0, 0.0, 0.0))
            for loop in loops:
                normal += normal_matrix @ loop.face.normal
            if normal.length < 1e-6:
                normal = normal_matrix @ edge.verts[0].normal
            if normal.length >= 1e-6:
                normal.normalize()
            lines.extend((
                matrix @ edge.verts[0].co + normal * offset,
                matrix @ edge.verts[1].co + normal * offset,
            ))
    if obj.mode != 'EDIT':
        bm.free()


def _draw_overlay():
    context = bpy.context
    # The handler is registered on SpaceView3D/WINDOW; draw callbacks may expose
    # no active area through bpy.context, so do not discard a valid 3D draw here.
    if context.scene is None or context.region_data is None:
        return
    state = context.scene.model_checker_state
    scene_key = context.scene.as_pointer()
    if not state.overlay_active or not state.uv_show_boundaries:
        _DRAW_CACHE.pop(scene_key, None)
        return
    objects = tuple(
        obj for obj in context.scene.objects
        if obj.type == 'MESH' and obj.visible_get(view_layer=context.view_layer)
    )
    signature = _overlay_signature(context.mode, objects, state)
    cached = _DRAW_CACHE.get(scene_key)
    if cached is None or state.overlay_cache_dirty or cached[0] != signature:
        window = context.window
        modal_active = _edit_transform_active(window)
        if modal_active:
            if cached is None:
                return
        else:
            signature = _overlay_signature(context.mode, objects, state)
            cached = _build_overlay_batches(objects, state, signature)
            _DRAW_CACHE[scene_key] = cached
            state.overlay_cache_dirty = False
    _, line_shader, line_batch = cached
    if line_batch is None:
        return
    try:
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.depth_mask_set(False)
        if line_batch is not None:
            gpu.state.blend_set('ALPHA')
            line_shader.bind()
            line_shader.uniform_float("viewportSize", gpu.state.viewport_get()[2:])
            line_shader.uniform_float("lineWidth", state.uv_boundary_width)
            line_shader.uniform_float("color", (*state.uv_boundary_color, 1.0))
            line_batch.draw(line_shader)
    finally:
        gpu.state.depth_mask_set(True)
        gpu.state.depth_test_set('NONE')
        gpu.state.blend_set('NONE')


def _overlay_signature(mode, objects, state):
    object_signature = []
    for obj in objects:
        active_uv = obj.data.uv_layers.active
        matrix_signature = tuple(value for row in obj.matrix_world for value in row)
        object_signature.append((
            obj.as_pointer(),
            obj.data.as_pointer(),
            active_uv.name if active_uv else None,
            obj.data.uv_layers.active_index,
            matrix_signature,
        ))
    return (
        mode,
        tuple(object_signature),
        state.uv_show_boundaries,
        tuple(state.uv_boundary_color),
    )


def _build_overlay_batches(objects, state, signature):
    boundary_positions = []
    for obj in objects:
        if state.uv_show_boundaries:
            _collect_boundary_edges(obj, boundary_positions)
    line_shader = None
    line_batch = None
    if boundary_positions:
        line_shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
        line_batch = batch_for_shader(line_shader, 'LINES', {"pos": boundary_positions})
    return signature, line_shader, line_batch


@persistent
def _invalidate_overlay_cache(scene, depsgraph):
    if scene is None or not hasattr(scene, "model_checker_state"):
        return
    state = scene.model_checker_state
    if not state.overlay_active:
        return
    if state.refresh_in_progress:
        return
    changed_meshes = set()
    transformed_meshes = set()
    for update in depsgraph.updates:
        updated_id = update.id.original or update.id
        if isinstance(updated_id, bpy.types.Mesh):
            if update.is_updated_geometry:
                changed_meshes.add(updated_id)
        elif isinstance(updated_id, bpy.types.Object) and updated_id.type == 'MESH':
            if update.is_updated_geometry:
                changed_meshes.add(updated_id.data)
            if update.is_updated_transform:
                transformed_meshes.add(updated_id.data)
    if not changed_meshes and not transformed_meshes:
        return

    state.overlay_cache_dirty = True
    if not (state.uv_show_stretch or state.uv_show_checker or state.uv_show_orientation):
        return

    from . import diagnostic
    if state.uv_show_checker:
        diagnostic.mark_full_refresh(state)
        needs_refresh = True
    else:
        needs_refresh = False
        for mesh in changed_meshes | transformed_meshes:
            needs_refresh = diagnostic.mark_mesh_dirty(
                state,
                mesh,
                force=mesh in transformed_meshes,
            ) or needs_refresh
    if needs_refresh:
        if bpy.context.mode == 'EDIT_MESH':
            state.uv_display_defer_until = time.monotonic() + 0.6
        diagnostic.start_refresh_timer()


@persistent
def _clear_overlay_cache(_dummy):
    _DRAW_CACHE.clear()


def register_draw_handler():
    global _DRAW_HANDLE
    if _invalidate_overlay_cache not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_invalidate_overlay_cache)
    if _clear_overlay_cache not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_clear_overlay_cache)
    if _DRAW_HANDLE is None:
        _DRAW_HANDLE = bpy.types.SpaceView3D.draw_handler_add(_draw_overlay, (), 'WINDOW', 'POST_VIEW')


def unregister_draw_handler():
    global _DRAW_HANDLE
    if _invalidate_overlay_cache in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_invalidate_overlay_cache)
    if _clear_overlay_cache in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_clear_overlay_cache)
    _DRAW_CACHE.clear()
    if _DRAW_HANDLE is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_DRAW_HANDLE, 'WINDOW')
        _DRAW_HANDLE = None
