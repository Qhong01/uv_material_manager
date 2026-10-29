"""Automatic, change-driven UV bounds checking for the UV Editor.

The checker is a single on/off session per scene.  While it is enabled:

* the edit-mode meshes are scanned once to establish the baseline;
* a depsgraph handler marks only the mesh whose geometry/UV data changed;
* a debounced timer refreshes that dirty mesh outside of panel/GPU drawing;
* a cheap watchdog notices mode, edit-object and active-UV-layer changes;
* the overlay is cached as GPU batches and only the initiating UV Editor is
  annotated;
* closing the session restores that editor's original UV grid settings.

No mesh is iterated in a panel ``draw()`` callback or in the GPU draw callback.
"""

import bpy
import bmesh
import json
import math
import time

import gpu
from gpu_extras.batch import batch_for_shader
from bpy.types import Operator, PropertyGroup
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.app.handlers import persistent

EPSILON = 1e-6
REFRESH_INITIAL = 0.02
REFRESH_SETTLE = 0.08
REFRESH_RETRY = 0.10
WATCHDOG_INTERVAL = 0.35
SCAN_BUDGET = 0.008
SCAN_TICK = 0.001
# Written by model_checker.diagnostic when it refreshes the UV-stretch /
# UV-orientation loop colours.  The value is an exact monotonic integer string;
# one depsgraph callback carrying it is skipped, later callbacks are real edits.
COLOR_ECHO_TOKEN_KEY = "_model_checker_color_echo_token"
COLOR_ECHO_MAX_AGE_NS = 80_000_000


# ---------------------------------------------------------------------------
# UV maths
# ---------------------------------------------------------------------------

def is_uv_out_of_bounds(u: float, v: float, eps: float = EPSILON) -> bool:
    """Return True when (u, v) is strictly outside the first tile."""
    return not ((-eps <= u <= 1.0 + eps) and (-eps <= v <= 1.0 + eps))


def uv_coord_to_tile_index(val: float, eps: float = EPSILON) -> int:
    """Map a 1D UV coordinate to an integer tile index with tolerance."""
    nearest = round(val)
    if abs(val - nearest) < eps:
        val = float(nearest)
    if val > 0.0:
        if abs(val - nearest) < eps and nearest > 0:
            return nearest - 1
        return int(math.floor(val))
    if val < 0.0:
        if abs(val - nearest) < eps and nearest < 0:
            return nearest
        return int(math.floor(val))
    return 0


def uv_to_tile(u: float, v: float, eps: float = EPSILON) -> tuple[int, int]:
    """Map 2D UV coordinates to a tile coordinate."""
    return (
        uv_coord_to_tile_index(u, eps),
        uv_coord_to_tile_index(v, eps),
    )


# ---------------------------------------------------------------------------
# Context and mesh discovery
# ---------------------------------------------------------------------------

def get_unique_edit_mesh_objects(context):
    """Return one object per mesh currently in Edit Mode (shared meshes once)."""
    if context is None or getattr(context, "mode", None) != 'EDIT_MESH':
        return []

    candidates = getattr(context, "objects_in_mode_unique_data", None)
    if candidates is None:
        candidates = getattr(context, "objects_in_mode", [])
        if not candidates:
            edit_object = getattr(context, "edit_object", None)
            candidates = [edit_object] if edit_object is not None else []

    unique = []
    seen = set()
    for obj in candidates:
        try:
            mesh = obj.data
            is_mesh = obj.type == 'MESH'
        except (ReferenceError, AttributeError):
            continue
        if not is_mesh or mesh is None:
            continue
        pointer = mesh.as_pointer()
        if pointer in seen:
            continue
        seen.add(pointer)
        unique.append(obj)
    return unique


def _unique_edit_meshes(context):
    return [obj.data for obj in get_unique_edit_mesh_objects(context)]


def _active_uv_info(mesh):
    """Return (name, index) of the mesh's active UV layer."""
    try:
        active = mesh.uv_layers.active
    except (ReferenceError, AttributeError):
        return "", 0
    if active is None:
        return "", 0
    return active.name, mesh.uv_layers.active_index


def _mesh_signature(meshes):
    return ";".join(str(pointer) for pointer in sorted(mesh.as_pointer() for mesh in meshes))


def get_initiating_uv_space(context):
    """Resolve the UV Editor space from which the toggle was invoked."""
    space = getattr(context, "space_data", None)
    if space is not None and space.type == 'IMAGE_EDITOR' and hasattr(space, "uv_editor"):
        return space

    area = getattr(context, "area", None)
    if area is not None and area.type == 'IMAGE_EDITOR':
        active_space = getattr(area.spaces, "active", None)
        if (
            active_space is not None
            and active_space.type == 'IMAGE_EDITOR'
            and hasattr(active_space, "uv_editor")
        ):
            return active_space

    window_manager = getattr(context, "window_manager", None)
    if window_manager is not None:
        for window in window_manager.windows:
            screen = getattr(window, "screen", None)
            if screen is None:
                continue
            for window_area in screen.areas:
                if window_area.type != 'IMAGE_EDITOR':
                    continue
                candidate = getattr(window_area.spaces, "active", None)
                if (
                    candidate is not None
                    and candidate.type == 'IMAGE_EDITOR'
                    and hasattr(candidate, "uv_editor")
                ):
                    return candidate
    return None


def find_space_by_pointer(context, pointer_string):
    """Find a UV Editor space by its saved pointer string."""
    if not pointer_string:
        return None
    try:
        target_pointer = int(pointer_string)
    except (ValueError, TypeError):
        return None

    window_manager = getattr(context, "window_manager", None)
    if window_manager is None:
        return None
    for window in window_manager.windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != 'IMAGE_EDITOR':
                continue
            for space in area.spaces:
                if space.type == 'IMAGE_EDITOR' and space.as_pointer() == target_pointer:
                    return space
    return None


def tag_redraw_image_editors(context):
    """Request redraws for all Image Editor areas (panel and GPU overlay)."""
    window_manager = getattr(context, "window_manager", None)
    if window_manager is None:
        return
    for window in window_manager.windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            if area.type == 'IMAGE_EDITOR':
                area.tag_redraw()


# ---------------------------------------------------------------------------
# Scanning (read-only)
# ---------------------------------------------------------------------------

def _scan_mesh_data(mesh, uv_name=None):
    """Read visible edit-mesh faces and return their out-of-bounds summary."""
    result = {
        'has_uv': False,
        'out_of_bounds_count': 0,
        'tiles': set(),
        'negative_tiles_count': 0,
        'loops_scanned': 0,
        'faces_scanned': 0,
        'max_pos_tx': 0,
        'max_pos_ty': 0,
    }
    try:
        bm = bmesh.from_edit_mesh(mesh)
    except (RuntimeError, ValueError, ReferenceError):
        return result

    bm.faces.ensure_lookup_table()
    if uv_name:
        uv_layer = bm.loops.layers.uv.get(uv_name)
    else:
        uv_layer = bm.loops.layers.uv.active
    if uv_layer is None:
        return result

    result['has_uv'] = True
    oob_count = 0
    loops_scanned = 0
    faces_scanned = 0
    tiles = result['tiles']
    eps = EPSILON

    for face in bm.faces:
        if face.hide:
            continue
        faces_scanned += 1
        for loop in face.loops:
            uv = loop[uv_layer].uv
            u = uv.x
            v = uv.y
            loops_scanned += 1
            if -eps <= u <= 1.0 + eps and -eps <= v <= 1.0 + eps:
                continue
            oob_count += 1
            tile = uv_to_tile(u, v, eps)
            if tile != (0, 0):
                tiles.add(tile)

    result['out_of_bounds_count'] = oob_count
    result['loops_scanned'] = loops_scanned
    result['faces_scanned'] = faces_scanned
    if tiles:
        result['max_pos_tx'] = max((tile[0] for tile in tiles if tile[0] >= 0), default=0)
        result['max_pos_ty'] = max((tile[1] for tile in tiles if tile[1] >= 0), default=0)
        result['negative_tiles_count'] = sum(
            1 for tile in tiles if tile[0] < 0 or tile[1] < 0
        )
    return result


def _scan_bmesh_slice(bm, face_iter, total_faces, start_cursor, deadline, uv_name=None):
    """Continue a held edit-BMesh face iterator until the time budget expires."""
    result = {
        'next_cursor': start_cursor,
        'done': False,
        'total_faces': total_faces,
        'uv_present': False,
        'uv_missing': False,
        'out_of_bounds_count': 0,
        'tiles': set(),
        'loops_scanned': 0,
        'faces_scanned': 0,
    }
    if total_faces <= 0:
        result['done'] = True
        return result

    if uv_name:
        uv_layer = bm.loops.layers.uv.get(uv_name)
    else:
        uv_layer = bm.loops.layers.uv.active
    if uv_layer is None:
        result['next_cursor'] = total_faces
        result['done'] = True
        result['uv_missing'] = bool(uv_name)
        return result

    result['uv_present'] = True
    oob_count = 0
    loops_scanned = 0
    faces_scanned = 0
    processed = 0
    tiles = result['tiles']
    eps = EPSILON
    perf = time.perf_counter

    while True:
        face = next(face_iter, None)
        if face is None:
            result['done'] = True
            break
        processed += 1
        if not face.hide:
            faces_scanned += 1
            for loop in face.loops:
                uv = loop[uv_layer].uv
                u = uv.x
                v = uv.y
                loops_scanned += 1
                if -eps <= u <= 1.0 + eps and -eps <= v <= 1.0 + eps:
                    continue
                oob_count += 1
                tile = uv_to_tile(u, v, eps)
                if tile != (0, 0):
                    tiles.add(tile)
        if perf() >= deadline:
            break

    result['next_cursor'] = start_cursor + processed
    result['out_of_bounds_count'] = oob_count
    result['loops_scanned'] = loops_scanned
    result['faces_scanned'] = faces_scanned
    return result


def scan_uv_bounds(context):
    """Compatibility helper: scan the current edit meshes without caching."""
    empty = {
        'has_active_uv': False,
        'out_of_bounds_count': 0,
        'oob_tiles': set(),
        'max_pos_tx': 0,
        'max_pos_ty': 0,
        'has_negative': False,
    }
    meshes = _unique_edit_meshes(context)
    if not meshes:
        return empty

    has_uv = False
    oob_count = 0
    tiles = set()
    for mesh in meshes:
        uv_name, _index = _active_uv_info(mesh)
        data = _scan_mesh_data(mesh, uv_name)
        has_uv = has_uv or data['has_uv']
        oob_count += data['out_of_bounds_count']
        tiles.update(data['tiles'])

    if not has_uv:
        return empty
    return {
        'has_active_uv': True,
        'out_of_bounds_count': oob_count,
        'oob_tiles': tiles,
        'max_pos_tx': max((tile[0] for tile in tiles if tile[0] >= 0), default=0),
        'max_pos_ty': max((tile[1] for tile in tiles if tile[1] >= 0), default=0),
        'has_negative': any(tile[0] < 0 or tile[1] < 0 for tile in tiles),
    }


# ---------------------------------------------------------------------------
# Native UV grid handling
# ---------------------------------------------------------------------------

def save_original_grid_settings(space, props):
    """Save the target editor's grid settings once per enabled session."""
    if props.has_saved_state or space is None or not hasattr(space, "uv_editor"):
        return False
    try:
        props.target_space_ptr = str(space.as_pointer())
        props.orig_grid_source = space.uv_editor.grid_shape_source
        props.orig_tile_grid_cols = int(space.uv_editor.tile_grid_shape[0])
        props.orig_tile_grid_rows = int(space.uv_editor.tile_grid_shape[1])
        props.orig_show_grid_bg = bool(space.overlay.show_grid_background)
        props.orig_show_overlays = bool(space.overlay.show_overlays)
        props.has_saved_state = True
        props.grid_applied = False
    except (ReferenceError, AttributeError, ValueError, TypeError, RuntimeError):
        return False
    return True


def apply_grid_expansion(space, cols, rows):
    """Expand the native background grid on one UV Editor only."""
    if space is None or not hasattr(space, "uv_editor"):
        return False
    try:
        space.uv_editor.grid_shape_source = 'FIXED'
        space.uv_editor.tile_grid_shape = (max(1, int(cols)), max(1, int(rows)))
        space.overlay.show_grid_background = True
        space.overlay.show_overlays = True
    except (ReferenceError, AttributeError, ValueError, TypeError, RuntimeError):
        return False
    return True


def restore_grid_settings(context, props):
    """Restore the target editor's original grid settings."""
    if not props.has_saved_state and not props.grid_applied:
        return False
    space = find_space_by_pointer(context, props.target_space_ptr)
    restored = False
    if space is not None and hasattr(space, "uv_editor"):
        try:
            space.uv_editor.grid_shape_source = props.orig_grid_source
            space.uv_editor.tile_grid_shape = (
                int(props.orig_tile_grid_cols),
                int(props.orig_tile_grid_rows),
            )
            space.overlay.show_grid_background = props.orig_show_grid_bg
            space.overlay.show_overlays = props.orig_show_overlays
            restored = True
        except (ReferenceError, AttributeError, ValueError, TypeError, RuntimeError):
            restored = False
    props.has_saved_state = False
    props.grid_applied = False
    props.applied_grid_cols = 0
    props.applied_grid_rows = 0
    return restored


def _grid_for_result(context, props):
    """Apply or restore the native grid to match the current result."""
    changed = False
    if props.out_of_bounds_count <= 0:
        if props.has_saved_state or props.grid_applied:
            changed = restore_grid_settings(context, props)
        return changed

    space = find_space_by_pointer(context, props.target_space_ptr)
    if space is None:
        return False

    cols = max(1, int(props.max_pos_tx) + 1)
    rows = max(1, int(props.max_pos_ty) + 1)
    if not props.has_saved_state:
        changed = save_original_grid_settings(space, props) or changed
    if not props.grid_applied or (props.applied_grid_cols, props.applied_grid_rows) != (cols, rows):
        if apply_grid_expansion(space, cols, rows):
            props.grid_applied = True
            props.applied_grid_cols = cols
            props.applied_grid_rows = rows
            changed = True
    return changed


# ---------------------------------------------------------------------------
# GPU overlay cache and draw callback
# ---------------------------------------------------------------------------

_GPU_SCENE_CACHE = {}
_BMESH_SESSIONS = {}
_SHADER_FILL = None
_SHADER_LINE = None
_DRAW_HANDLER = None


def clear_gpu_cache(scene=None):
    """Drop GPU batches for a scene, or for all scenes."""
    if scene is None:
        _GPU_SCENE_CACHE.clear()
        return
    try:
        _GPU_SCENE_CACHE.pop(scene.as_pointer(), None)
    except (ReferenceError, AttributeError):
        pass


def clear_bmesh_sessions():
    """Release borrowed BMesh wrappers; never free Blender-owned edit BMesh."""
    _BMESH_SESSIONS.clear()


def _build_gpu_batches(scene, tiles, revision, target_pointer):
    """Build and cache GPU batches for a result revision (outside draw where possible)."""
    if scene is None:
        return None
    scene_key = scene.as_pointer()
    if not tiles:
        _GPU_SCENE_CACHE.pop(scene_key, None)
        return None

    global _SHADER_FILL, _SHADER_LINE
    try:
        if _SHADER_FILL is None:
            _SHADER_FILL = gpu.shader.from_builtin('UNIFORM_COLOR')
        if _SHADER_LINE is None:
            _SHADER_LINE = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    except (RuntimeError, SystemError, AttributeError, ValueError, TypeError):
        _GPU_SCENE_CACHE.pop(scene_key, None)
        return None

    fill_vertices = []
    outline_vertices = []
    grid_vertices = []

    for tx, ty in tiles:
        fill_vertices.extend((
            (float(tx), float(ty)),
            (float(tx + 1), float(ty)),
            (float(tx + 1), float(ty + 1)),
            (float(tx), float(ty)),
            (float(tx + 1), float(ty + 1)),
            (float(tx), float(ty + 1)),
        ))
        outline_vertices.extend((
            (float(tx), float(ty)), (float(tx + 1), float(ty)),
            (float(tx + 1), float(ty)), (float(tx + 1), float(ty + 1)),
            (float(tx + 1), float(ty + 1)), (float(tx), float(ty + 1)),
            (float(tx), float(ty + 1)), (float(tx), float(ty)),
        ))
        if tx < 0 or ty < 0:
            for step in range(1, 10):
                fraction = step / 10.0
                grid_vertices.extend((
                    (float(tx) + fraction, float(ty)),
                    (float(tx) + fraction, float(ty + 1)),
                    (float(tx), float(ty) + fraction),
                    (float(tx + 1), float(ty) + fraction),
                ))

    try:
        batch_fill = batch_for_shader(_SHADER_FILL, 'TRIS', {"pos": fill_vertices})
        batch_outline = batch_for_shader(_SHADER_LINE, 'LINES', {"pos": outline_vertices})
        batch_grid = (
            batch_for_shader(_SHADER_LINE, 'LINES', {"pos": grid_vertices})
            if grid_vertices else None
        )
        cached = {
            "revision": revision,
            "target_pointer": target_pointer,
            "batch_fill": batch_fill,
            "batch_outline": batch_outline,
            "batch_grid": batch_grid,
            "failed": False,
        }
        _GPU_SCENE_CACHE[scene_key] = cached
        return cached
    except (RuntimeError, SystemError, AttributeError, ValueError, TypeError):
        _GPU_SCENE_CACHE[scene_key] = {
            "revision": revision,
            "target_pointer": target_pointer,
            "batch_fill": None,
            "batch_outline": None,
            "batch_grid": None,
            "failed": True,
        }
        return None


def rebuild_gpu_batches(scene, oob_tiles, revision=0, target_pointer=""):
    """Public-ish helper retained for tests and other plugin code."""
    return _build_gpu_batches(scene, tuple(oob_tiles), revision, target_pointer)


def _draw_uv_bounds_overlay():
    """GPU draw: use cached batches only; never iterate a mesh here."""
    context = bpy.context
    scene = getattr(context, "scene", None)
    if scene is None:
        return
    props = getattr(scene, "um_uv_bounds_checker", None)
    if props is None or not props.is_active:
        return
    if not props.results_current:
        return
    if props.out_of_bounds_count <= 0 or props.tiles_json in ("", "[]"):
        return

    area = getattr(context, "area", None)
    if area is not None and area.type != 'IMAGE_EDITOR':
        return
    current_space = getattr(context, "space_data", None)
    if current_space is None and area is not None:
        current_space = getattr(area.spaces, "active", None)
    if current_space is None or not props.target_space_ptr:
        return
    try:
        if str(current_space.as_pointer()) != props.target_space_ptr:
            return
    except (ReferenceError, AttributeError):
        return

    scene_key = scene.as_pointer()
    cached = _GPU_SCENE_CACHE.get(scene_key)
    if (
        cached is None
        or cached.get("revision") != props.render_revision
        or cached.get("target_pointer") != props.target_space_ptr
    ):
        try:
            tiles = [tuple(item) for item in json.loads(props.tiles_json)]
        except (json.JSONDecodeError, ValueError, TypeError):
            return
        cached = _build_gpu_batches(
            scene,
            tiles,
            props.render_revision,
            props.target_space_ptr,
        )
    if cached is None or cached.get("failed"):
        return

    batch_fill = cached.get("batch_fill")
    batch_outline = cached.get("batch_outline")
    batch_grid = cached.get("batch_grid")
    if _SHADER_FILL is None or _SHADER_LINE is None:
        return

    try:
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('NONE')
        gpu.state.depth_mask_set(False)

        if batch_fill is not None:
            _SHADER_FILL.bind()
            _SHADER_FILL.uniform_float("color", (1.0, 0.45, 0.05, 0.12))
            batch_fill.draw(_SHADER_FILL)

        if batch_grid is not None:
            _SHADER_LINE.bind()
            _SHADER_LINE.uniform_float("viewportSize", gpu.state.viewport_get()[2:])
            _SHADER_LINE.uniform_float("lineWidth", 1.0)
            _SHADER_LINE.uniform_float("color", (1.0, 0.55, 0.25, 0.35))
            batch_grid.draw(_SHADER_LINE)

        if batch_outline is not None:
            _SHADER_LINE.bind()
            _SHADER_LINE.uniform_float("viewportSize", gpu.state.viewport_get()[2:])
            _SHADER_LINE.uniform_float("lineWidth", 2.0)
            _SHADER_LINE.uniform_float("color", (1.0, 0.5, 0.1, 0.85))
            batch_outline.draw(_SHADER_LINE)
    except (RuntimeError, ValueError, AttributeError, TypeError):
        pass
    finally:
        try:
            gpu.state.depth_mask_set(True)
            gpu.state.blend_set('NONE')
        except (RuntimeError, ValueError, AttributeError, TypeError):
            pass


# ---------------------------------------------------------------------------
# Scene cache and refresh machinery
# ---------------------------------------------------------------------------

def _scene_props(scene):
    return getattr(scene, "um_uv_bounds_checker", None)


def _find_mesh_state(props, mesh):
    try:
        pointer = mesh.as_pointer()
    except (ReferenceError, AttributeError):
        return None
    for state in props.mesh_cache:
        try:
            state_mesh = state.mesh
        except ReferenceError:
            continue
        if state_mesh is not None and state_mesh.as_pointer() == pointer:
            return state
    return None


def _clear_results(scene, props, restore_grid=False, clear_cache=False):
    """Remove annotations/results without changing UV or selection data."""
    if restore_grid:
        restore_grid_settings(bpy.context, props)
    previous_tiles = props.tiles_json
    props.out_of_bounds_count = 0
    props.affected_tiles_count = 0
    props.negative_tiles_count = 0
    props.tiles_json = "[]"
    props.max_pos_tx = 0
    props.max_pos_ty = 0
    props.has_no_uv_layers = False
    props.has_edit_meshes = False
    props.edit_mesh_count = 0
    props.mesh_without_uv_count = 0
    if previous_tiles != "[]":
        props.render_revision += 1
    clear_gpu_cache(scene)
    if clear_cache:
        props.mesh_cache.clear()
    _clear_job_fields(props)
    props.scan_generation += 1
    props.results_current = False
    props.batch_active = False
    props.batch_meshes = 0
    props.batch_loops = 0
    props.batch_scan_ms = 0.0
    props.refresh_pending = False
    props.refresh_due = 0.0
    return previous_tiles != "[]"


def _schedule_refresh(props, delay=REFRESH_SETTLE):
    """Coalesce events into one scan after a short quiet period."""
    now = time.monotonic()
    due = now + max(0.0, float(delay))
    if props.refresh_pending:
        props.refresh_due = max(props.refresh_due, due)
    else:
        props.refresh_due = due
    props.refresh_pending = True
    if not bpy.app.timers.is_registered(_refresh_timer):
        bpy.app.timers.register(_refresh_timer, first_interval=REFRESH_INITIAL)


def _find_state_by_pointer(props, pointer):
    pointer_text = str(pointer)
    for state in props.mesh_cache:
        try:
            mesh = state.mesh
        except ReferenceError:
            continue
        if mesh is not None and str(mesh.as_pointer()) == pointer_text:
            return state
    return None


def _clear_job_fields(props, drop_session=True):
    props.job_mesh_ptr = ""
    props.job_generation = 0
    props.job_cursor = 0
    props.job_total_faces = 0
    props.job_oob = 0
    props.job_loops = 0
    props.job_faces = 0
    props.job_has_uv = False
    props.job_uv_name = ""
    props.job_uv_index = 0
    props.job_tile_chunks = ""
    props.scan_in_progress = False
    if drop_session:
        clear_bmesh_sessions()


def _cancel_active_job(props, requeue=True, drop_session=True):
    pointer = props.job_mesh_ptr
    if requeue and pointer:
        state = _find_state_by_pointer(props, pointer)
        if state is not None:
            state.pending = True
            state.dirty = True
    _clear_job_fields(props, drop_session=drop_session)


def _get_bmesh_session(scene, props, mesh):
    """Lease one edit BMesh across a bounded batch, discarded on invalidation."""
    key = scene.as_pointer()
    pointer = str(mesh.as_pointer())
    session = _BMESH_SESSIONS.get(key)
    if session is not None and session.get("mesh_ptr") == pointer:
        try:
            total_faces = len(session["bm"].faces)
            session["face_iter"] = iter(session["bm"].faces)
            session["generation"] = props.scan_generation
            session["total_faces"] = total_faces
            return session
        except (RuntimeError, ReferenceError, ValueError):
            _BMESH_SESSIONS.pop(key, None)
    elif session is not None:
        _BMESH_SESSIONS.pop(key, None)
    try:
        bm = bmesh.from_edit_mesh(mesh)
        total_faces = len(bm.faces)
    except (RuntimeError, ValueError, ReferenceError):
        return None
    session = {
        "bm": bm,
        "face_iter": iter(bm.faces),
        "mesh_ptr": pointer,
        "generation": props.scan_generation,
        "total_faces": total_faces,
    }
    _BMESH_SESSIONS[key] = session
    return session


def _add_state(props, mesh, pending=False):
    state = props.mesh_cache.add()
    state.mesh = mesh
    state.uv_name = ""
    state.uv_index = 0
    state.has_uv = False
    state.out_of_bounds_count = 0
    state.negative_tiles_count = 0
    state.tiles_json = "[]"
    state.dirty = bool(pending)
    state.pending = bool(pending)
    state.valid = False
    return state


def _begin_full_batch(context, scene, props, meshes):
    """Prepare a time-sliced full scan without scanning any face here."""
    signature = _mesh_signature(meshes)
    keep_visible = (
        props.results_current
        and props.edit_signature == signature
        and len(props.mesh_cache) == len(meshes)
    )
    _clear_job_fields(props)
    props.mesh_cache.clear()
    for mesh in meshes:
        _add_state(props, mesh, pending=True)
    props.edit_signature = signature
    props.needs_full = False
    if not keep_visible:
        # The edit set (or the session) changed: the old annotation belongs to
        # different meshes, so it must not stay on screen.
        props.results_current = False
        clear_gpu_cache(scene)
    props.refresh_pending = True
    props.batch_active = True
    props.batch_meshes = 0
    props.batch_loops = 0
    props.batch_scan_ms = 0.0
    props.scan_in_progress = False


def _start_job(scene, props, state):
    try:
        mesh = state.mesh
    except ReferenceError:
        return False
    if mesh is None:
        return False
    uv_name, uv_index = _active_uv_info(mesh)
    session = _get_bmesh_session(scene, props, mesh)
    if session is None:
        return False
    props.job_mesh_ptr = str(mesh.as_pointer())
    props.job_generation = props.scan_generation
    props.job_cursor = 0
    props.job_total_faces = session["total_faces"]
    props.job_oob = 0
    props.job_loops = 0
    props.job_faces = 0
    props.job_has_uv = False
    props.job_uv_name = uv_name
    props.job_uv_index = uv_index
    props.job_tile_chunks = ""
    props.scan_in_progress = True
    return True


def _append_job_tiles(props, tiles):
    if not tiles:
        return
    chunk = json.dumps(sorted(tiles))
    if props.job_tile_chunks:
        props.job_tile_chunks = props.job_tile_chunks + "\n" + chunk
    else:
        props.job_tile_chunks = chunk


def _finish_job(context, scene, props, meshes):
    state = _find_state_by_pointer(props, props.job_mesh_ptr)
    if state is None:
        _cancel_active_job(props, requeue=False)
        props.needs_full = True
        return False

    tiles = set()
    for line in props.job_tile_chunks.splitlines():
        try:
            tiles.update(tuple(tile) for tile in json.loads(line))
        except (json.JSONDecodeError, ValueError, TypeError):
            continue

    state.uv_name = props.job_uv_name
    state.uv_index = props.job_uv_index
    state.has_uv = bool(props.job_has_uv)
    state.out_of_bounds_count = int(props.job_oob)
    state.negative_tiles_count = sum(
        1 for tile in tiles if tile[0] < 0 or tile[1] < 0
    )
    state.tiles_json = json.dumps(sorted(tiles)) if tiles else "[]"
    state.dirty = False
    state.pending = False
    state.valid = True
    props.scan_count += 1
    props.batch_meshes += 1
    props.batch_loops += int(props.job_loops)
    # Keep the borrowed BMesh lease for the next edit on the same mesh; it is
    # dropped on mode/scene/file changes or when a different mesh is scanned.
    _clear_job_fields(props, drop_session=False)
    return True


def _finish_batch(context, scene, props, meshes):
    if any(state.pending for state in props.mesh_cache):
        return False
    props.scan_batches += 1
    props.last_scan_meshes = props.batch_meshes
    props.last_scan_loops = props.batch_loops
    props.last_scan_ms = props.batch_scan_ms
    props.batch_active = False
    props.needs_full = False
    props.scan_in_progress = False
    _publish_results(scene, props, context, meshes)
    return True


def _finish_empty_batch(context, scene, props):
    _clear_results(scene, props, restore_grid=True, clear_cache=True)
    props.edit_signature = ""
    props.needs_full = False
    props.refresh_pending = False
    props.refresh_due = 0.0
    props.scan_in_progress = False
    props.results_current = True
    props.batch_active = False
    tag_redraw_image_editors(context)


def _publish_results(scene, props, context, edit_meshes):
    """Publish one complete, generation-consistent aggregate only."""
    if any(state.pending for state in props.mesh_cache):
        return False
    was_current = bool(props.results_current)
    was_pending = bool(props.refresh_pending or props.batch_active or props.scan_in_progress)
    old_counts = (
        props.out_of_bounds_count,
        props.affected_tiles_count,
        props.negative_tiles_count,
        props.tiles_json,
    )
    union_tiles = set()
    total_oob = 0
    edit_mesh_count = len(edit_meshes)
    mesh_without_uv = 0
    has_any_uv = False

    for state in props.mesh_cache:
        if state.has_uv:
            has_any_uv = True
        else:
            mesh_without_uv += 1
        total_oob += int(state.out_of_bounds_count)
        if state.tiles_json and state.tiles_json != "[]":
            try:
                union_tiles.update(tuple(tile) for tile in json.loads(state.tiles_json))
            except (json.JSONDecodeError, ValueError, TypeError):
                continue

    max_pos_tx = max((tile[0] for tile in union_tiles if tile[0] >= 0), default=0)
    max_pos_ty = max((tile[1] for tile in union_tiles if tile[1] >= 0), default=0)
    negative_count = sum(
        1 for tile in union_tiles if tile[0] < 0 or tile[1] < 0
    )
    tiles_json = json.dumps(sorted(union_tiles)) if union_tiles else "[]"

    props.out_of_bounds_count = total_oob
    props.affected_tiles_count = len(union_tiles)
    props.negative_tiles_count = negative_count
    props.tiles_json = tiles_json
    props.max_pos_tx = max_pos_tx
    props.max_pos_ty = max_pos_ty
    props.has_no_uv_layers = bool(edit_meshes) and not has_any_uv
    props.has_edit_meshes = bool(edit_meshes)
    props.edit_mesh_count = edit_mesh_count
    props.mesh_without_uv_count = mesh_without_uv
    props.results_current = True
    props.refresh_pending = False
    props.refresh_due = 0.0
    props.scan_in_progress = False
    if tiles_json != old_counts[3]:
        props.render_revision += 1
        clear_gpu_cache(scene)

    grid_changed = _grid_for_result(context, props)
    new_counts = (
        props.out_of_bounds_count,
        props.affected_tiles_count,
        props.negative_tiles_count,
        props.tiles_json,
    )
    changed = new_counts != old_counts or grid_changed or not was_current or was_pending
    if changed:
        tag_redraw_image_editors(context)
    return changed


def _scan_next_slice(context, scene, props, meshes, deadline):
    """Scan at most one bounded face slice.  Never publishes partial results."""
    mesh_pointers = {str(mesh.as_pointer()) for mesh in meshes}

    if props.job_mesh_ptr:
        if props.job_generation != props.scan_generation:
            session = _BMESH_SESSIONS.get(scene.as_pointer())
            same_mesh = bool(
                session is not None
                and session.get("mesh_ptr") == props.job_mesh_ptr
            )
            _cancel_active_job(props, requeue=True, drop_session=not same_mesh)
            if same_mesh:
                try:
                    session["generation"] = props.scan_generation
                    session["face_iter"] = iter(session["bm"].faces)
                except (RuntimeError, ReferenceError, ValueError):
                    clear_bmesh_sessions()
            return False
        if props.job_mesh_ptr not in mesh_pointers:
            _cancel_active_job(props, requeue=False)
            props.needs_full = True
            return False

    state = None
    if not props.job_mesh_ptr:
        for candidate in props.mesh_cache:
            if candidate.pending:
                state = candidate
                break
        if state is None:
            if props.batch_meshes:
                _finish_batch(context, scene, props, meshes)
            else:
                _finish_empty_batch(context, scene, props)
            return True
        if not _start_job(scene, props, state):
            state.pending = False
            state.valid = False
            props.needs_full = True
            return False

    state = _find_state_by_pointer(props, props.job_mesh_ptr)
    if state is None:
        _cancel_active_job(props, requeue=False)
        props.needs_full = True
        return False
    try:
        mesh = state.mesh
    except ReferenceError:
        mesh = None
    if mesh is None:
        _cancel_active_job(props, requeue=False)
        props.needs_full = True
        return False

    session = _BMESH_SESSIONS.get(scene.as_pointer())
    if (
        session is None
        or session.get("mesh_ptr") != props.job_mesh_ptr
        or session.get("generation") != props.job_generation
    ):
        _cancel_active_job(props, requeue=True)
        props.scan_generation += 1
        return False

    scan_start = time.perf_counter()
    try:
        data = _scan_bmesh_slice(
            session["bm"],
            session["face_iter"],
            session["total_faces"],
            props.job_cursor,
            deadline,
            props.job_uv_name,
        )
    except (RuntimeError, ReferenceError, ValueError):
        _cancel_active_job(props, requeue=True)
        props.scan_generation += 1
        return False
    props.batch_scan_ms += (time.perf_counter() - scan_start) * 1000.0

    if data['uv_missing']:
        # The active UV layer disappeared between callbacks.  The partial state
        # is discarded and the mesh is queued again after the layer settles.
        props.scan_generation += 1
        _cancel_active_job(props, requeue=True)
        props.needs_full = True
        _schedule_refresh(props)
        return False

    if props.job_total_faces == 0:
        props.job_total_faces = data['total_faces']
    elif data['total_faces'] != props.job_total_faces:
        _cancel_active_job(props, requeue=True)
        props.scan_generation += 1
        return False

    props.job_oob += int(data['out_of_bounds_count'])
    props.job_loops += int(data['loops_scanned'])
    props.job_faces += int(data['faces_scanned'])
    props.job_has_uv = props.job_has_uv or bool(data['uv_present'])
    _append_job_tiles(props, data['tiles'])
    props.job_cursor = int(data['next_cursor'])

    if data['done']:
        if props.job_generation != props.scan_generation:
            _cancel_active_job(props, requeue=True)
            return False
        if not _finish_job(context, scene, props, meshes):
            return False
        if not any(candidate.pending for candidate in props.mesh_cache):
            _finish_batch(context, scene, props, meshes)
    return True


def _edit_transform_active(context):
    window = getattr(context, "window", None)
    if window is None:
        return False
    for operator in window.modal_operators:
        if getattr(operator, "bl_idname", "").startswith("TRANSFORM_OT_"):
            return True
    return False


def _refresh_timer():
    context = bpy.context
    scene = getattr(context, "scene", None)
    if scene is None:
        return None
    props = _scene_props(scene)
    if props is None or not props.is_active:
        return None

    pending = (
        props.needs_full
        or props.refresh_pending
        or bool(props.job_mesh_ptr)
        or any(state.pending for state in props.mesh_cache)
    )
    if not pending:
        props.refresh_due = 0.0
        return None
    if _edit_transform_active(context):
        # Coalesce an entire interactive UV drag into one refresh after the
        # modal transform finishes.  This is the important anti-stutter guard.
        if props.job_mesh_ptr:
            _cancel_active_job(props, requeue=True, drop_session=False)
        return REFRESH_RETRY

    callback_start = time.perf_counter()
    meshes = _unique_edit_meshes(context)
    if not meshes:
        if props.job_mesh_ptr:
            _cancel_active_job(props, requeue=False)
        _finish_empty_batch(context, scene, props)
        return None

    has_pending = any(state.pending for state in props.mesh_cache)
    if not (props.needs_full or props.job_mesh_ptr or has_pending):
        props.refresh_pending = False
        props.refresh_due = 0.0
        return None

    signature = _mesh_signature(meshes)
    if (
        props.needs_full
        or signature != props.edit_signature
        or len(props.mesh_cache) != len(meshes)
    ):
        _cancel_active_job(props, requeue=False)
        _begin_full_batch(context, scene, props, meshes)
        has_pending = any(state.pending for state in props.mesh_cache)

    if not props.batch_active and has_pending:
        props.batch_active = True
        props.batch_meshes = 0
        props.batch_loops = 0
        props.batch_scan_ms = 0.0
        # Keep results_current so the overlay keeps its last complete batch;
        # the publish step swaps in the new aggregate when it is ready.

    now = time.monotonic()
    if not props.job_mesh_ptr and props.refresh_due > now:
        # Do not scan yet: another UV event may extend the quiet period.
        return min(REFRESH_RETRY, max(REFRESH_INITIAL, props.refresh_due - now))

    props.scan_slices += 1
    _scan_next_slice(
        context,
        scene,
        props,
        meshes,
        callback_start + SCAN_BUDGET,
    )
    elapsed_ms = (time.perf_counter() - callback_start) * 1000.0
    props.last_callback_ms = elapsed_ms
    props.max_callback_ms = max(props.max_callback_ms, elapsed_ms)

    if props.needs_full or props.refresh_pending or props.job_mesh_ptr or any(
        state.pending for state in props.mesh_cache
    ):
        return SCAN_TICK
    props.refresh_due = 0.0
    return None


def _any_scene_active():
    for scene in bpy.data.scenes:
        props = _scene_props(scene)
        if props is not None and props.is_active:
            return True
    return False


def _watchdog():
    context = bpy.context
    scene = getattr(context, "scene", None)
    if scene is None:
        return None

    active_scenes = []
    for candidate in bpy.data.scenes:
        props = _scene_props(candidate)
        if props is not None and props.is_active:
            active_scenes.append((candidate, props))
    if not active_scenes:
        return None

    for candidate, props in active_scenes:
        if candidate is scene:
            continue
        if props.grid_applied or props.has_saved_state:
            restore_grid_settings(context, props)
        if props.has_edit_meshes or props.out_of_bounds_count or len(props.mesh_cache):
            _clear_results(candidate, props, restore_grid=False, clear_cache=True)
            props.edit_signature = ""
            props.needs_full = False
            props.refresh_pending = False
        props.pending_reentry = True

    props = _scene_props(scene)
    if props is None or not props.is_active:
        return WATCHDOG_INTERVAL

    if props.pending_reentry:
        props.pending_reentry = False
        _clear_results(scene, props, restore_grid=False, clear_cache=True)
        props.edit_signature = ""
        props.needs_full = True
        props.refresh_pending = True
        _schedule_refresh(props, REFRESH_INITIAL)

    if props.needs_full:
        _schedule_refresh(props, REFRESH_INITIAL)
        return WATCHDOG_INTERVAL

    if props.job_mesh_ptr or any(state.pending for state in props.mesh_cache):
        # A time-sliced batch is already in flight; the timer owns progression.
        return WATCHDOG_INTERVAL

    meshes = _unique_edit_meshes(context)
    signature = _mesh_signature(meshes)
    if signature != props.edit_signature:
        _clear_results(scene, props, restore_grid=True, clear_cache=True)
        props.edit_signature = ""
        props.needs_full = True
        props.refresh_pending = True
        _schedule_refresh(props, REFRESH_INITIAL)
        return WATCHDOG_INTERVAL

    uv_changed = False
    for mesh in meshes:
        state = _find_mesh_state(props, mesh)
        if state is None or not state.valid:
            props.needs_full = True
            props.refresh_pending = True
            _schedule_refresh(props, REFRESH_INITIAL)
            return WATCHDOG_INTERVAL
        uv_name, uv_index = _active_uv_info(mesh)
        if state.uv_name != uv_name or state.uv_index != uv_index:
            state.dirty = True
            state.pending = True
            uv_changed = True
    if uv_changed:
        props.scan_generation += 1
        props.results_current = False
        clear_gpu_cache(scene)
        _schedule_refresh(props)
        tag_redraw_image_editors(context)

    if props.results_current and not any(state.pending for state in props.mesh_cache):
        _grid_for_result(context, props)

    return WATCHDOG_INTERVAL


def _stop_timers():
    for timer in (_refresh_timer, _watchdog, _history_timer):
        if bpy.app.timers.is_registered(timer):
            bpy.app.timers.unregister(timer)


def _ensure_watchdog():
    if not bpy.app.timers.is_registered(_watchdog):
        bpy.app.timers.register(_watchdog, first_interval=WATCHDOG_INTERVAL)


# ---------------------------------------------------------------------------
# Public state transitions
# ---------------------------------------------------------------------------

def _invalidate_scene(scene, props, restore_grid):
    _clear_results(scene, props, restore_grid=restore_grid, clear_cache=True)
    props.edit_signature = ""
    props.needs_full = True
    props.refresh_pending = True
    _schedule_refresh(props, REFRESH_INITIAL)
    _ensure_watchdog()


def enable_uv_bounds_check(context):
    """Turn the automatic checker on for the current scene."""
    scene = getattr(context, "scene", None)
    if scene is None:
        return False
    props = _scene_props(scene)
    if props is None:
        return False

    target_space = get_initiating_uv_space(context)
    if target_space is None:
        return False

    if props.is_active:
        return True

    _clear_results(scene, props, restore_grid=False, clear_cache=True)
    props.is_active = True
    props.target_space_ptr = str(target_space.as_pointer())
    props.has_saved_state = False
    props.grid_applied = False
    props.applied_grid_cols = 0
    props.applied_grid_rows = 0
    props.pending_reentry = False
    props.edit_signature = ""
    props.needs_full = True
    props.refresh_pending = True
    props.history_pending = False
    props.scan_count = 0
    props.scan_batches = 0
    props.scan_slices = 0
    props.last_scan_ms = 0.0
    props.last_scan_meshes = 0
    props.last_scan_loops = 0
    props.last_callback_ms = 0.0
    props.max_callback_ms = 0.0
    _schedule_refresh(props, REFRESH_INITIAL)
    _ensure_watchdog()
    tag_redraw_image_editors(context)
    return True


def disable_uv_bounds_check(context, restore=True):
    """Turn off the checker and remove all of its transient resources."""
    scene = getattr(context, "scene", None)
    if scene is None:
        return
    props = _scene_props(scene)
    if props is None:
        return

    if restore:
        restore_grid_settings(context, props)
    props.is_active = False
    props.target_space_ptr = ""
    props.has_saved_state = False
    props.grid_applied = False
    props.applied_grid_cols = 0
    props.applied_grid_rows = 0
    props.pending_reentry = False
    props.edit_signature = ""
    props.needs_full = False
    props.scan_in_progress = False
    props.history_pending = False
    _clear_results(scene, props, restore_grid=False, clear_cache=True)
    clear_gpu_cache(scene)
    if not _any_scene_active():
        _stop_timers()
    tag_redraw_image_editors(context)


def _disable_all_scenes(restore=True):
    context = bpy.context
    for scene in bpy.data.scenes:
        props = _scene_props(scene)
        if props is None:
            continue
        if restore and (props.has_saved_state or props.grid_applied):
            restore_grid_settings(context, props)
        props.is_active = False
        props.target_space_ptr = ""
        props.has_saved_state = False
        props.grid_applied = False
        props.pending_reentry = False
        props.needs_full = False
        props.refresh_pending = False
        props.refresh_due = 0.0
        props.scan_in_progress = False
        props.results_current = False
        props.batch_active = False
        props.history_pending = False
        _clear_job_fields(props)
        props.mesh_cache.clear()
        props.out_of_bounds_count = 0
        props.affected_tiles_count = 0
        props.negative_tiles_count = 0
        props.tiles_json = "[]"
        props.has_edit_meshes = False
        props.has_no_uv_layers = False
        props.render_revision += 1
    _stop_timers()
    clear_gpu_cache()


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

@persistent
def _on_depsgraph_update(scene, depsgraph):
    context = bpy.context
    if scene is None or context is None or context.scene is not scene:
        return
    props = _scene_props(scene)
    if props is None or not props.is_active:
        return
    if getattr(context, "mode", None) != 'EDIT_MESH':
        return

    meshes = _unique_edit_meshes(context)
    if not meshes:
        return
    edit_pointers = {mesh.as_pointer() for mesh in meshes}
    changed = False
    echo_updates = set()

    for update in depsgraph.updates:
        try:
            identifier = update.id.original or update.id
        except (ReferenceError, AttributeError):
            continue
        if isinstance(identifier, bpy.types.Mesh):
            mesh = identifier
        elif isinstance(identifier, bpy.types.Object) and identifier.type == 'MESH':
            try:
                mesh = identifier.data
            except (ReferenceError, AttributeError):
                continue
        else:
            continue
        if not update.is_updated_geometry or mesh is None:
            continue
        pointer = mesh.as_pointer()
        if pointer not in edit_pointers:
            continue

        state = _find_mesh_state(props, mesh)
        # The model checker rewrites a loop-colour attribute and tags the mesh
        # as "geometry updated".  Consume exactly one echo token per callback;
        # a later callback with the same token, or any newer update, is a real
        # edit and must not be swallowed.
        try:
            echo_token = str(mesh.get(COLOR_ECHO_TOKEN_KEY, ""))
        except (TypeError, ValueError):
            echo_token = ""
        if state is not None and echo_token:
            if echo_token != state.color_echo_token:
                try:
                    token_ns = int(echo_token)
                except (TypeError, ValueError):
                    token_ns = 0
                if token_ns and (time.monotonic_ns() - token_ns) < COLOR_ECHO_MAX_AGE_NS:
                    state.color_echo_token = echo_token
                    echo_updates.add(pointer)
                    continue
            if pointer in echo_updates:
                continue

        if state is None:
            props.needs_full = True
            changed = True
        else:
            state.dirty = True
            state.pending = True
            # Extend the quiet period on every update, even when the mesh is
            # already dirty, so a continuous drag produces one scan at the end.
            changed = True

    if changed:
        # Any edit invalidates the in-flight slice, but keep the last
        # *complete* aggregate visible while the same edit meshes are
        # revalidated.  Hiding it here made unrelated geometry updates blink
        # the overlay.  Partial counts are still never published.
        props.scan_generation += 1
        _schedule_refresh(props)
        tag_redraw_image_editors(context)


@persistent
def _on_load_pre(*_args):
    """Restore temporary grids before Blender replaces the file."""
    context = bpy.context
    for scene in bpy.data.scenes:
        props = _scene_props(scene)
        if props is None:
            continue
        if props.has_saved_state or props.grid_applied:
            restore_grid_settings(context, props)


@persistent
def _on_load_post(*_args):
    """The session is intentionally not persisted across files."""
    _stop_timers()
    _disable_all_scenes(restore=False)
    tag_redraw_image_editors(bpy.context)


@persistent
def _on_save_pre(*_args):
    """Do not write an expanded temporary grid into the .blend file."""
    context = bpy.context
    changed = False
    for scene in bpy.data.scenes:
        props = _scene_props(scene)
        if props is None:
            continue
        if props.has_saved_state or props.grid_applied:
            changed = restore_grid_settings(context, props) or changed
    if changed:
        tag_redraw_image_editors(context)


@persistent
def _on_save_post(*_args):
    """Re-apply the current editor's grid after the file was written."""
    context = bpy.context
    scene = getattr(context, "scene", None)
    if scene is None:
        return
    props = _scene_props(scene)
    if props is None or not props.is_active:
        return
    if props.out_of_bounds_count > 0:
        _grid_for_result(context, props)
        tag_redraw_image_editors(context)


@persistent
def _on_history_post(scene=None, *_args):
    """Defer reconciliation until undo/redo has finished touching scene data."""
    context = bpy.context
    target_scene = scene or getattr(context, "scene", None)
    if target_scene is None:
        return
    props = _scene_props(target_scene)
    if props is None:
        return
    props.history_pending = True
    if not bpy.app.timers.is_registered(_history_timer):
        bpy.app.timers.register(_history_timer, first_interval=0.0)


def _history_timer():
    context = bpy.context
    current_scene = getattr(context, "scene", None)
    for scene in bpy.data.scenes:
        props = _scene_props(scene)
        if props is None or not props.history_pending:
            continue
        props.history_pending = False
        if props.is_active:
            if scene is current_scene:
                _invalidate_scene(scene, props, restore_grid=False)
            else:
                props.needs_full = True
                props.refresh_pending = True
        else:
            if props.has_saved_state or props.grid_applied:
                restore_grid_settings(context, props)
            props.target_space_ptr = ""
            _clear_results(scene, props, restore_grid=False, clear_cache=True)
            tag_redraw_image_editors(context)
    return None


# ---------------------------------------------------------------------------
# Operator and UI
# ---------------------------------------------------------------------------

class UVMM_OT_ToggleUVBounds(Operator):
    """开启/关闭自动 UV 越界检查（开启后自动跟踪变化）"""

    bl_idname = "uvmm.toggle_uv_bounds"
    bl_label = "UV 越界检查"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return getattr(context, "scene", None) is not None

    def execute(self, context):
        props = _scene_props(context.scene)
        if props is None:
            self.report({'ERROR'}, "未找到 UV 越界检查属性")
            return {'CANCELLED'}

        if props.is_active:
            disable_uv_bounds_check(context, restore=True)
            self.report({'INFO'}, "已关闭 UV 越界检查并恢复原栅格")
            return {'FINISHED'}

        if not enable_uv_bounds_check(context):
            self.report({'ERROR'}, "请在 UV 编辑器中开启 UV 越界检查")
            return {'CANCELLED'}
        self.report({'INFO'}, "已开启 UV 越界检查，将自动跟踪变化")
        return {'FINISHED'}


def draw_uv_bounds_check_panel(panel, context):
    """Draw the one-switch UV bounds panel without touching mesh data."""
    layout = panel.layout
    props = getattr(context.scene, "um_uv_bounds_checker", None)
    if props is None:
        layout.label(text="属性未就绪", icon='ERROR')
        return

    active = bool(props.is_active)
    row = layout.row(align=True)
    row.scale_y = 1.25
    row.operator(
        "uvmm.toggle_uv_bounds",
        text="UV 越界检查",
        icon='CHECKBOX_HLT' if active else 'CHECKBOX_DEHLT',
        depress=active,
    )

    if not active:
        layout.label(text="已关闭", icon='CHECKBOX_DEHLT')
        return

    current_space = get_initiating_uv_space(context)
    current_pointer = str(current_space.as_pointer()) if current_space is not None else ""
    is_target = bool(props.target_space_ptr) and current_pointer == props.target_space_ptr
    target_exists = find_space_by_pointer(context, props.target_space_ptr) is not None

    box = layout.box()
    column = box.column(align=True)
    if not props.results_current or props.refresh_pending or props.scan_in_progress:
        column.label(text="正在自动检查…", icon='TIME')
    elif not props.has_edit_meshes:
        column.label(text="已开启 · 进入网格编辑模式后自动检查", icon='INFO')
    elif props.has_no_uv_layers:
        column.label(text="当前编辑网格没有活动 UV 层", icon='ERROR')
    elif props.out_of_bounds_count == 0:
        column.label(text="UV 未越界（全部在 [0, 1] 内）", icon='CHECKMARK')
    else:
        column.label(
            text=f"越界 UV 点数: {props.out_of_bounds_count}",
            icon='ERROR',
        )
        column.label(
            text=f"涉及瓦片数: {props.affected_tiles_count}",
            icon='GRID',
        )
        if props.negative_tiles_count > 0:
            column.label(
                text=f"含负向瓦片: {props.negative_tiles_count} 个",
                icon='INFO',
            )

    if props.has_edit_meshes:
        info = layout.column(align=True)
        info.label(
            text=f"编辑网格: {props.edit_mesh_count}  无 UV: {props.mesh_without_uv_count}",
            icon='MESH_DATA',
        )

    if not target_exists:
        note = layout.column(align=True)
        note.label(text="目标 UV 编辑器已关闭，请关闭后重新开启", icon='ERROR')
    elif not is_target:
        note = layout.column(align=True)
        note.label(text="标注显示在启动检查的 UV 编辑器", icon='INFO')


# ---------------------------------------------------------------------------
# RNA state
# ---------------------------------------------------------------------------

class UM_PG_UVBoundsMeshState(PropertyGroup):
    """Cached result for one unique edit-mode mesh."""

    mesh: PointerProperty(type=bpy.types.Mesh, options={'SKIP_SAVE'})
    uv_name: StringProperty(default="", options={'SKIP_SAVE'})
    uv_index: IntProperty(default=0, options={'SKIP_SAVE'})
    has_uv: BoolProperty(default=False, options={'SKIP_SAVE'})
    out_of_bounds_count: IntProperty(default=0, options={'SKIP_SAVE'})
    negative_tiles_count: IntProperty(default=0, options={'SKIP_SAVE'})
    tiles_json: StringProperty(default="[]", options={'SKIP_SAVE'})
    dirty: BoolProperty(default=False, options={'SKIP_SAVE'})
    pending: BoolProperty(default=False, options={'SKIP_SAVE'})
    valid: BoolProperty(default=False, options={'SKIP_SAVE'})
    color_echo_token: StringProperty(default="", options={'SKIP_SAVE'})

class UM_PG_UVBoundsChecker(PropertyGroup):
    """Persistent session flags and transient analysis results attach to a Scene."""

    is_active: BoolProperty(name="自动检查", default=False, options={'SKIP_SAVE'})
    needs_full: BoolProperty(default=False, options={'SKIP_SAVE'})
    refresh_pending: BoolProperty(default=False, options={'SKIP_SAVE'})
    refresh_due: FloatProperty(default=0.0, options={'SKIP_SAVE'})
    history_pending: BoolProperty(default=False, options={'SKIP_SAVE'})
    scan_in_progress: BoolProperty(default=False, options={'SKIP_SAVE'})
    results_current: BoolProperty(default=False, options={'SKIP_SAVE'})
    batch_active: BoolProperty(default=False, options={'SKIP_SAVE'})
    scan_generation: IntProperty(default=0, options={'SKIP_SAVE'})
    pending_reentry: BoolProperty(default=False, options={'SKIP_SAVE'})
    edit_signature: StringProperty(default="", options={'SKIP_SAVE'})

    job_mesh_ptr: StringProperty(default="", options={'SKIP_SAVE'})
    job_generation: IntProperty(default=0, options={'SKIP_SAVE'})
    job_cursor: IntProperty(default=0, options={'SKIP_SAVE'})
    job_total_faces: IntProperty(default=0, options={'SKIP_SAVE'})
    job_oob: IntProperty(default=0, options={'SKIP_SAVE'})
    job_loops: IntProperty(default=0, options={'SKIP_SAVE'})
    job_faces: IntProperty(default=0, options={'SKIP_SAVE'})
    job_has_uv: BoolProperty(default=False, options={'SKIP_SAVE'})
    job_uv_name: StringProperty(default="", options={'SKIP_SAVE'})
    job_uv_index: IntProperty(default=0, options={'SKIP_SAVE'})
    job_tile_chunks: StringProperty(default="", options={'SKIP_SAVE'})
    batch_meshes: IntProperty(default=0, options={'SKIP_SAVE'})
    batch_loops: IntProperty(default=0, options={'SKIP_SAVE'})
    batch_scan_ms: FloatProperty(default=0.0, options={'SKIP_SAVE'})

    has_edit_meshes: BoolProperty(default=False, options={'SKIP_SAVE'})
    edit_mesh_count: IntProperty(default=0, options={'SKIP_SAVE'})
    mesh_without_uv_count: IntProperty(default=0, options={'SKIP_SAVE'})
    has_no_uv_layers: BoolProperty(default=False, options={'SKIP_SAVE'})
    out_of_bounds_count: IntProperty(default=0, options={'SKIP_SAVE'})
    affected_tiles_count: IntProperty(default=0, options={'SKIP_SAVE'})
    negative_tiles_count: IntProperty(default=0, options={'SKIP_SAVE'})
    tiles_json: StringProperty(default="[]", options={'SKIP_SAVE'})
    max_pos_tx: IntProperty(default=0, options={'SKIP_SAVE'})
    max_pos_ty: IntProperty(default=0, options={'SKIP_SAVE'})
    render_revision: IntProperty(default=0, options={'SKIP_SAVE'})

    scan_count: IntProperty(default=0, options={'SKIP_SAVE'})
    scan_batches: IntProperty(default=0, options={'SKIP_SAVE'})
    scan_slices: IntProperty(default=0, options={'SKIP_SAVE'})
    last_scan_ms: FloatProperty(default=0.0, options={'SKIP_SAVE'})
    last_scan_meshes: IntProperty(default=0, options={'SKIP_SAVE'})
    last_scan_loops: IntProperty(default=0, options={'SKIP_SAVE'})
    last_callback_ms: FloatProperty(default=0.0, options={'SKIP_SAVE'})
    max_callback_ms: FloatProperty(default=0.0, options={'SKIP_SAVE'})

    target_space_ptr: StringProperty(default="", options={'SKIP_SAVE'})
    orig_grid_source: StringProperty(default="DYNAMIC", options={'SKIP_SAVE'})
    orig_tile_grid_cols: IntProperty(default=1, options={'SKIP_SAVE'})
    orig_tile_grid_rows: IntProperty(default=1, options={'SKIP_SAVE'})
    orig_show_grid_bg: BoolProperty(default=True, options={'SKIP_SAVE'})
    orig_show_overlays: BoolProperty(default=True, options={'SKIP_SAVE'})
    has_saved_state: BoolProperty(default=False, options={'SKIP_SAVE'})
    grid_applied: BoolProperty(default=False, options={'SKIP_SAVE'})
    applied_grid_cols: IntProperty(default=0, options={'SKIP_SAVE'})
    applied_grid_rows: IntProperty(default=0, options={'SKIP_SAVE'})

    mesh_cache: CollectionProperty(type=UM_PG_UVBoundsMeshState, options={'SKIP_SAVE'})


classes = (
    UM_PG_UVBoundsMeshState,
    UM_PG_UVBoundsChecker,
    UVMM_OT_ToggleUVBounds,
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_HANDLER_LISTS = (
    (bpy.app.handlers.depsgraph_update_post, _on_depsgraph_update),
    (bpy.app.handlers.load_pre, _on_load_pre),
    (bpy.app.handlers.load_post, _on_load_post),
    (bpy.app.handlers.save_pre, _on_save_pre),
    (bpy.app.handlers.save_post, _on_save_post),
    (bpy.app.handlers.undo_post, _on_history_post),
    (bpy.app.handlers.redo_post, _on_history_post),
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.um_uv_bounds_checker = PointerProperty(type=UM_PG_UVBoundsChecker)

    global _DRAW_HANDLER
    if _DRAW_HANDLER is None:
        _DRAW_HANDLER = bpy.types.SpaceImageEditor.draw_handler_add(
            _draw_uv_bounds_overlay, (), 'WINDOW', 'POST_VIEW'
        )

    for handler_list, handler in _HANDLER_LISTS:
        if handler not in handler_list:
            handler_list.append(handler)


def unregister():
    _stop_timers()
    _disable_all_scenes(restore=True)

    global _DRAW_HANDLER
    if _DRAW_HANDLER is not None:
        try:
            bpy.types.SpaceImageEditor.draw_handler_remove(_DRAW_HANDLER, 'WINDOW')
        except (ValueError, TypeError, ReferenceError):
            pass
        _DRAW_HANDLER = None

    for handler_list, handler in _HANDLER_LISTS:
        if handler in handler_list:
            handler_list.remove(handler)

    clear_gpu_cache()

    if hasattr(bpy.types.Scene, "um_uv_bounds_checker"):
        del bpy.types.Scene.um_uv_bounds_checker

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

