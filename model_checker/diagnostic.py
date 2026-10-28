"""Viewport UV diagnostics and a temporary image-texture checker preview."""

import hashlib
import math
import time
from array import array

import bpy
import bmesh
import numpy as np
from mathutils import Vector


_DISPLAY_ATTR_KEY = "_model_checker_display_attribute"
_DISPLAY_SIGNATURE_KEY = "_model_checker_display_signature"
_DISPLAY_MATRIX_KEY = "_model_checker_display_matrix"
_COLOR_WRITE_TIME_KEY = "_model_checker_color_write_time"
_STATS_STRETCH_KEY = "_model_checker_stats_stretch"
_STATS_FLIPPED_KEY = "_model_checker_stats_flipped"
_STATS_MATRIX_KEY = "_model_checker_stats_matrix"
_STATS_UV_KEY = "_model_checker_stats_uv"
_STATS_COUNT_KEY = "_model_checker_stats_count"
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


def _triangle_stretch_array(coords, uvs):
    """Vectorised equivalent of _triangle_stretch for every loop triangle.

    coords: (T, 3, 3) world-space corners, uvs: (T, 3, 2) corner UVs.
    Returns (stretch, flipped) arrays with one entry per triangle.
    """
    edge_1 = coords[:, 1] - coords[:, 0]
    edge_2 = coords[:, 2] - coords[:, 0]
    length_1 = np.linalg.norm(edge_1, axis=1)
    safe_1 = np.maximum(length_1, 1e-12)
    axis_x = edge_1 / safe_1[:, None]
    projected_x = np.einsum("ij,ij->i", edge_2, axis_x)
    orthogonal = edge_2 - axis_x * projected_x[:, None]
    length_y = np.linalg.norm(orthogonal, axis=1)
    safe_y = np.maximum(length_y, 1e-12)

    uv_1 = uvs[:, 1] - uvs[:, 0]
    uv_2 = uvs[:, 2] - uvs[:, 0]
    a = uv_1[:, 0] / safe_1
    b = uv_2[:, 0] / safe_y - uv_1[:, 0] * projected_x / (safe_1 * safe_y)
    c = uv_1[:, 1] / safe_1
    d = uv_2[:, 1] / safe_y - uv_1[:, 1] * projected_x / (safe_1 * safe_y)

    xx = a * a + c * c
    yy = b * b + d * d
    xy = a * b + c * d
    discriminant = np.sqrt(np.maximum((xx - yy) ** 2 + 4.0 * xy * xy, 0.0))
    largest = np.sqrt(np.maximum((xx + yy + discriminant) * 0.5, 1e-16))
    smallest = np.sqrt(np.maximum((xx + yy - discriminant) * 0.5, 1e-16))
    with np.errstate(divide="ignore", invalid="ignore"):
        stretch = np.minimum(np.abs(np.log2(largest / smallest)) / 4.0, 1.0)

    signed_area = uv_1[:, 0] * uv_2[:, 1] - uv_1[:, 1] * uv_2[:, 0]
    degenerate = (
        (length_1 < 1e-6)
        | (length_y < 1e-6)
        | (np.abs(signed_area) < 1e-12)
    )
    stretch = np.where(degenerate, 1.0, stretch)
    flipped = signed_area < -1e-12
    return stretch, flipped


def _face_base_colors_array(stretch, flipped, prefs):
    """Vectorised equivalent of _face_base_color for all faces at once."""
    count = len(stretch)
    background = 0.38
    threshold = min(max(float(prefs.uv_stretch_threshold), 0.0), 0.999)
    strength = max(float(prefs.uv_stretch_strength), 0.25)
    stretch_opacity = float(prefs.uv_stretch_opacity)
    orientation_opacity = float(prefs.uv_orientation_opacity)

    amount = np.where(
        stretch <= threshold + 1e-6,
        0.0,
        (stretch - threshold) / max(1.0 - threshold, 1e-6),
    )
    factor = np.clip(amount, 0.0, 1.0) ** (1.0 / strength)
    stretch_base = np.asarray(prefs.uv_stretch_base_color, dtype=np.float64)
    stretch_max = np.asarray(prefs.uv_stretch_max_color, dtype=np.float64)
    stretch_color = stretch_base + (stretch_max - stretch_base) * factor[:, None]
    stretch_mix = background + (stretch_color - background) * stretch_opacity

    if prefs.uv_show_orientation:
        back = np.asarray(prefs.uv_orientation_back_color, dtype=np.float64)
        front = np.asarray(prefs.uv_orientation_front_color, dtype=np.float64)
        back_mix = background + (back - background) * orientation_opacity
        front_mix = background + (front - background) * orientation_opacity
        if prefs.uv_show_stretch:
            return np.where(flipped[:, None], back_mix, stretch_mix)
        return np.where(flipped[:, None], back_mix, front_mix)
    if prefs.uv_show_stretch:
        return stretch_mix
    return np.full((count, 3), background, dtype=np.float64)


def _ensure_stats_attributes(mesh):
    stretch_attr = mesh.attributes.get(_STATS_STRETCH_KEY)
    if stretch_attr is None or stretch_attr.domain != 'FACE' or stretch_attr.data_type != 'FLOAT':
        if stretch_attr is not None:
            mesh.attributes.remove(stretch_attr)
        stretch_attr = mesh.attributes.new(_STATS_STRETCH_KEY, 'FLOAT', 'FACE')
    flipped_attr = mesh.attributes.get(_STATS_FLIPPED_KEY)
    if flipped_attr is None or flipped_attr.domain != 'FACE' or flipped_attr.data_type != 'INT':
        if flipped_attr is not None:
            mesh.attributes.remove(flipped_attr)
        flipped_attr = mesh.attributes.new(_STATS_FLIPPED_KEY, 'INT', 'FACE')
    return stretch_attr, flipped_attr


def _read_stats_cache(mesh, uv_name, matrix_values):
    """Return cached per-face stretch/flip stats when geometry is unchanged."""
    if mesh.get(_STATS_UV_KEY, "") != uv_name:
        return None
    if int(mesh.get(_STATS_COUNT_KEY, -1)) != len(mesh.polygons):
        return None
    stretch_attr = mesh.attributes.get(_STATS_STRETCH_KEY)
    flipped_attr = mesh.attributes.get(_STATS_FLIPPED_KEY)
    if stretch_attr is None or flipped_attr is None:
        return None
    if stretch_attr.domain != 'FACE' or stretch_attr.data_type != 'FLOAT':
        return None
    if flipped_attr.domain != 'FACE' or flipped_attr.data_type != 'INT':
        return None
    stored_matrix = tuple(mesh.get(_STATS_MATRIX_KEY, ()))
    if stored_matrix and not _conformal_delta(stored_matrix, matrix_values):
        return None

    count = len(mesh.polygons)
    stretch = np.empty(count, dtype=np.float32)
    stretch_attr.data.foreach_get("value", stretch)
    flipped = np.empty(count, dtype=np.int32)
    flipped_attr.data.foreach_get("value", flipped)
    if stored_matrix and stored_matrix != matrix_values:
        mesh[_STATS_MATRIX_KEY] = list(matrix_values)
    return stretch.astype(np.float64), flipped.astype(bool)


def _write_stats_cache(mesh, max_stretch, face_flipped, uv_name, matrix_values):
    stretch_attr, flipped_attr = _ensure_stats_attributes(mesh)
    stretch_attr.data.foreach_set("value", max_stretch.astype(np.float32))
    flipped_attr.data.foreach_set("value", face_flipped.astype(np.int32))
    mesh[_STATS_UV_KEY] = uv_name
    mesh[_STATS_MATRIX_KEY] = list(matrix_values)
    mesh[_STATS_COUNT_KEY] = len(mesh.polygons)


def _update_object_mesh_colors(obj, uv_layer, display_attr, prefs, stats_dirty=False):
    mesh = obj.data
    matrix = np.asarray(obj.matrix_world, dtype=np.float64)
    matrix_values = tuple(value for row in obj.matrix_world for value in row)
    polygon_count = len(mesh.polygons)
    if polygon_count == 0:
        return True

    cached = None if stats_dirty else _read_stats_cache(mesh, uv_layer.name, matrix_values)
    if cached is not None:
        max_stretch, face_flipped = cached
    else:
        mesh.calc_loop_triangles()
        triangle_count = len(mesh.loop_triangles)
        if triangle_count == 0:
            return True

        vertices = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", vertices)
        local_coords = vertices.reshape(-1, 3).astype(np.float64)
        world_coords = local_coords @ matrix[:3, :3].T + matrix[:3, 3]

        loop_vertices = np.empty(len(mesh.loops), dtype=np.int32)
        mesh.loops.foreach_get("vertex_index", loop_vertices)
        uv_values = np.empty(len(mesh.loops) * 2, dtype=np.float32)
        uv_layer.data.foreach_get("uv", uv_values)
        loop_uvs = uv_values.reshape(-1, 2).astype(np.float64)

        triangle_loops = np.empty(triangle_count * 3, dtype=np.int32)
        mesh.loop_triangles.foreach_get("loops", triangle_loops)
        triangle_loops = triangle_loops.reshape(-1, 3)
        triangle_polygons = np.empty(triangle_count, dtype=np.int32)
        mesh.loop_triangles.foreach_get("polygon_index", triangle_polygons)

        coords = world_coords[loop_vertices[triangle_loops]]
        uvs = loop_uvs[triangle_loops]
        stretch, flipped_triangles = _triangle_stretch_array(coords, uvs)

        # loop_triangles are grouped by polygon, so reduceat replaces the much
        # slower ufunc.at accumulation.
        triangle_counts = np.bincount(triangle_polygons, minlength=polygon_count)
        if polygon_count and triangle_counts.size == polygon_count and triangle_counts.all():
            starts = np.concatenate(([0], np.cumsum(triangle_counts)[:-1])).astype(np.int64)
            max_stretch = np.maximum.reduceat(stretch, starts)
            flipped_votes = np.add.reduceat(flipped_triangles.astype(np.int32), starts)
            face_flipped = flipped_votes * 2 >= triangle_counts
        else:
            max_stretch = np.zeros(polygon_count, dtype=np.float64)
            np.maximum.at(max_stretch, triangle_polygons, stretch)
            flipped_votes = np.zeros(polygon_count, dtype=np.int32)
            np.add.at(flipped_votes, triangle_polygons, flipped_triangles.astype(np.int32))
            face_flipped = (triangle_counts > 0) & (flipped_votes * 2 >= triangle_counts)
        _write_stats_cache(mesh, max_stretch, face_flipped, uv_layer.name, matrix_values)

    face_colors = _face_base_colors_array(max_stretch, face_flipped, prefs)

    loop_totals = np.empty(polygon_count, dtype=np.int32)
    mesh.polygons.foreach_get("loop_total", loop_totals)
    if int(loop_totals.sum()) == len(mesh.loops):
        loop_polygons = np.repeat(np.arange(polygon_count, dtype=np.int32), loop_totals)
        rgba = np.ones((len(mesh.loops), 4), dtype=np.float32)
        rgba[:, :3] = face_colors[loop_polygons].astype(np.float32)
        display_attr.data.foreach_set("color", rgba.ravel())
    else:
        # Non-contiguous loop layout: keep a correct, slower per-polygon path.
        for polygon in mesh.polygons:
            color = (*face_colors[polygon.index], 1.0)
            for loop_index in polygon.loop_indices:
                display_attr.data[loop_index].color = color
    # Attribute writes invalidate the depsgraph; remember when so the handler
    # does not mistake our own output for a user geometry edit.
    mesh[_COLOR_WRITE_TIME_KEY] = time.monotonic()
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
        texture = nodes.new('ShaderNodeTexImage')
        texture.image = _checker_image(prefs)
        texture.interpolation = 'Closest'
        texture.extension = 'REPEAT'
        texture.select = True
        nodes.active = texture
        if uv_name:
            uv_map = nodes.new('ShaderNodeUVMap')
            uv_map.uv_map = uv_name
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


_SHARED_CHECKER_GROUP = ".Model Checker UV - Checker"
_SHARED_CHECKER_MATERIAL = ".Model Checker UV - Checker"


def _shared_checker_group(prefs):
    """One node group + one material shared by every object preview."""
    group = bpy.data.node_groups.get(_SHARED_CHECKER_GROUP)
    if group is None or getattr(group, "bl_idname", "") != 'GeometryNodeTree':
        group = bpy.data.node_groups.new(_SHARED_CHECKER_GROUP, 'GeometryNodeTree')
        group.is_modifier = True
        group.interface.new_socket(name="Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
        group.interface.new_socket(name="Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
        group.nodes.clear()
        group_input = group.nodes.new('NodeGroupInput')
        group_output = group.nodes.new('NodeGroupOutput')
        set_material = group.nodes.new('GeometryNodeSetMaterial')
        material = bpy.data.materials.get(_SHARED_CHECKER_MATERIAL)
        if material is None:
            material = bpy.data.materials.new(_SHARED_CHECKER_MATERIAL)
        set_material.inputs['Material'].default_value = material
        group.links.new(group_input.outputs['Geometry'], set_material.inputs['Geometry'])
        group.links.new(set_material.outputs['Geometry'], group_output.inputs['Geometry'])
        group[_GROUP_MATERIAL_KEY] = material.name
        group[_GROUP_SIGNATURE_KEY] = ""

    material = bpy.data.materials.get(_SHARED_CHECKER_MATERIAL)
    if material is None:
        material = bpy.data.materials.new(_SHARED_CHECKER_MATERIAL)
        for node in group.nodes:
            if node.bl_idname == 'GeometryNodeSetMaterial':
                node.inputs['Material'].default_value = material
                break
        group[_GROUP_MATERIAL_KEY] = material.name

    signature = repr((
        "shared-checker-v1",
        int(prefs.uv_checker_density),
        float(prefs.uv_checker_opacity),
        tuple(prefs.uv_checker_color_a),
        tuple(prefs.uv_checker_color_b),
    ))
    if group.get(_GROUP_SIGNATURE_KEY, "") != signature:
        _build_checker_material(material, "", "", prefs)
        group[_GROUP_SIGNATURE_KEY] = signature
    return group


def _ensure_checker_modifier(obj, uv_name, display_name, prefs):
    group = _shared_checker_group(prefs)
    modifier = _legacy_preview_modifier(obj)
    if modifier is None:
        modifier = obj.modifiers.new("模型检查 UV 棋盘格", 'NODES')
    modifier.node_group = group
    modifier.show_render = False
    modifier.show_in_editmode = True
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
    for key in (
        _DISPLAY_ATTR_KEY,
        _DISPLAY_SIGNATURE_KEY,
        _DISPLAY_MATRIX_KEY,
        _COLOR_WRITE_TIME_KEY,
        _PREVIOUS_COLOR_KEY,
        _PREVIOUS_COLOR_HAD_KEY,
    ):
        if key in mesh:
            del mesh[key]
    for key in _LEGACY_MESH_KEYS:
        name = mesh.get(key)
        attribute = mesh.attributes.get(name) if name else None
        if attribute is not None:
            mesh.attributes.remove(attribute)
        if key in mesh:
            del mesh[key]
    for name in (_STATS_STRETCH_KEY, _STATS_FLIPPED_KEY):
        attribute = mesh.attributes.get(name)
        if attribute is not None:
            mesh.attributes.remove(attribute)
    for key in (_STATS_MATRIX_KEY, _STATS_UV_KEY, _STATS_COUNT_KEY):
        if key in mesh:
            del mesh[key]


def _conformal_delta(old_values, new_values, tolerance=1e-4):
    """True when new = (uniform scale + rotation + translation) * old.

    Such a transform preserves every world-space length ratio, so the stretch
    heatmap and the flipped-face classification stay valid without recomputing.
    """
    if len(old_values) != 16 or len(new_values) != 16:
        return False
    from mathutils import Matrix

    old = Matrix((old_values[0:4], old_values[4:8], old_values[8:12], old_values[12:16]))
    new = Matrix((new_values[0:4], new_values[4:8], new_values[8:12], new_values[12:16]))
    try:
        delta = new @ old.inverted()
    except (ValueError, ZeroDivisionError):
        return False
    linear = delta.to_3x3()
    columns = [linear.col[index] for index in range(3)]
    lengths = [column.length for column in columns]
    longest = max(lengths)
    if longest < 1e-9 or min(lengths) < longest * (1.0 - tolerance):
        return False
    if abs(longest - min(lengths)) > longest * tolerance:
        return False
    for first in range(3):
        for second in range(first + 1, 3):
            if abs(columns[first].dot(columns[second])) > longest * longest * tolerance:
                return False
    return linear.determinant() > 0.0


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
    ))
    matrix_values = tuple(value for row in obj.matrix_world for value in row)
    stored_signature = mesh.get(_DISPLAY_SIGNATURE_KEY, "")
    stored_matrix = tuple(mesh.get(_DISPLAY_MATRIX_KEY, ()))
    if not needs_color_diagnostics:
        updated = True
    elif force_update:
        updated = False
    elif stored_signature == signature and stored_matrix:
        # Same look; only skip when the object moved by a conformal transform.
        updated = _conformal_delta(stored_matrix, matrix_values)
    else:
        updated = False
    if not updated:
        if obj.mode == 'EDIT':
            updated = False
            for _attempt in range(3):
                updated = _update_edit_mesh_colors(obj, active_uv_name, display_name, prefs)
                if updated:
                    break
        else:
            current_uv = mesh.uv_layers.get(active_uv_name)
            updated = current_uv is not None
            if updated:
                _update_object_mesh_colors(obj, current_uv, display_attr, prefs, stats_dirty=force_update)
    if updated and needs_color_diagnostics:
        mesh[_DISPLAY_SIGNATURE_KEY] = signature
        mesh[_DISPLAY_MATRIX_KEY] = matrix_values
    if prefs.uv_show_checker:
        checker_modifier = _ensure_checker_modifier(obj, active_uv_name, display_name, prefs)
        checker_modifier.show_viewport = not needs_color_diagnostics
    else:
        _remove_legacy_preview(obj)
    return updated


def _start_checker_watchdog():
    if not bpy.app.timers.is_registered(_checker_watchdog):
        bpy.app.timers.register(_checker_watchdog, first_interval=0.5)


def _checker_watchdog():
    """Cheap poll: only rebuild previews when the visible object set changed.

    Replaces the old behaviour of recomputing every mesh 4x per second while
    the checker preview was enabled.
    """
    context = bpy.context
    scene = context.scene
    if scene is None or not hasattr(scene, "model_checker_state"):
        return None
    addon = context.preferences.addons.get(__package__.split('.')[0])
    if addon is None or not addon.preferences.uv_show_checker:
        return None
    state = scene.model_checker_state
    if state.refresh_in_progress:
        return 0.2
    group = bpy.data.node_groups.get(_SHARED_CHECKER_GROUP)
    if group is None:
        mark_full_refresh(state)
        start_refresh_timer()
        return 0.2
    for obj in visible_meshes(scene, context.view_layer):
        modifier = _legacy_preview_modifier(obj)
        if modifier is None or modifier.node_group is not group:
            mark_full_refresh(state)
            start_refresh_timer()
            return 0.2
    return 0.5


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
        if prefs.uv_show_checker:
            _start_checker_watchdog()
        if not active:
            restore_viewport_mode(scene)
        return None
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

    full_refresh = state.uv_display_full_refresh or prefs.uv_show_checker
    dirty_entries = tuple(item for item in state.uv_mesh_states if item.dirty)
    if not full_refresh and not dirty_entries:
        full_refresh = True

    state.refresh_in_progress = True
    try:
        if full_refresh:
            visible_objects = set(visible_meshes(scene, context.view_layer)) if active else set()
            visible_mesh_data = {obj.data for obj in visible_objects}
            state.uv_mesh_states.clear()
            for obj in tuple(scene.objects):
                if obj.type != 'MESH':
                    continue
                if obj in visible_objects:
                    _ensure_preview(obj, prefs, force_update=False)
                elif mesh_has_diagnostic(obj.data) or _legacy_preview_modifier(obj) is not None:
                    _remove_legacy_preview(obj)
                    if obj.data not in visible_mesh_data:
                        _remove_diagnostic_attributes(obj.data)
            state.uv_display_full_refresh = False
            state.uv_display_dirty = False
        else:
            dirty_meshes = {item.mesh for item in dirty_entries if item.mesh is not None}
            objects_by_mesh = {}
            visibility_by_mesh = {}
            for obj in scene.objects:
                if obj.type == 'MESH' and obj.data in dirty_meshes:
                    visible = obj.visible_get(view_layer=context.view_layer)
                    previous_visibility = visibility_by_mesh.get(obj.data, False)
                    if obj.data not in objects_by_mesh or visible or not previous_visibility:
                        objects_by_mesh[obj.data] = obj
                        visibility_by_mesh[obj.data] = visible

            for item in dirty_entries:
                mesh = item.mesh
                if mesh is None:
                    item.dirty = False
                    continue
                obj = objects_by_mesh.get(mesh)
                if obj is None:
                    continue
                _ensure_preview(obj, prefs, force_update=not item.transform_only)
                item.dirty = False
                item.transform_only = False
            state.uv_display_dirty = any(item.dirty for item in state.uv_mesh_states)
    finally:
        state.refresh_in_progress = False
    _remove_checker_images()
    if prefs.uv_show_checker:
        _start_checker_watchdog()
        return None
    if active:
        return None
    restore_viewport_mode(scene)
    return None


def mesh_source_signature(mesh):
    """Hash only the mesh inputs used by UV diagnostics, excluding display colors."""
    digest = hashlib.blake2b(digest_size=16)
    active_uv = mesh.uv_layers.active
    active_uv_name = active_uv.name if active_uv is not None else ""
    digest.update(active_uv_name.encode('utf-8'))
    digest.update(str((len(mesh.vertices), len(mesh.loops), len(mesh.polygons))).encode('ascii'))

    coordinates = array('f', [0.0]) * (len(mesh.vertices) * 3)
    if coordinates:
        mesh.vertices.foreach_get("co", coordinates)
        digest.update(coordinates.tobytes())

    loop_vertices = array('i', [0]) * len(mesh.loops)
    if loop_vertices:
        mesh.loops.foreach_get("vertex_index", loop_vertices)
        digest.update(loop_vertices.tobytes())

    loop_totals = array('i', [0]) * len(mesh.polygons)
    if loop_totals:
        mesh.polygons.foreach_get("loop_total", loop_totals)
        digest.update(loop_totals.tobytes())

    if active_uv is not None:
        uv_coordinates = array('f', [0.0]) * (len(active_uv.data) * 2)
        if uv_coordinates:
            active_uv.data.foreach_get("uv", uv_coordinates)
            digest.update(uv_coordinates.tobytes())
    return digest.hexdigest()


def _find_mesh_state(state, mesh):
    mesh_pointer = mesh.as_pointer()
    return next((
        item for item in state.uv_mesh_states
        if item.mesh is not None and item.mesh.as_pointer() == mesh_pointer
    ), None)


def _store_mesh_source_signature(state, mesh):
    item = _find_mesh_state(state, mesh)
    if item is None:
        item = state.uv_mesh_states.add()
        item.mesh = mesh
    item.source_signature = mesh_source_signature(mesh)
    item.dirty = False
    return item


def mark_full_refresh(state):
    state.uv_display_dirty = True
    state.uv_display_full_refresh = True


def mark_mesh_dirty(state, mesh, force=False):
    if state.uv_display_full_refresh:
        return False
    if not force:
        last_write = float(mesh.get(_COLOR_WRITE_TIME_KEY, 0.0))
        if last_write and time.monotonic() - last_write < 0.25:
            # Depsgraph echo of our own colour-attribute write.
            return False
    item = _find_mesh_state(state, mesh)
    if item is None:
        item = state.uv_mesh_states.add()
        item.mesh = mesh
    item.dirty = True
    # `force` means the depsgraph only reported a transform, so the colour
    # buffer can be reused when the transform preserves length ratios.
    item.transform_only = force
    state.uv_display_dirty = True
    return True


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
