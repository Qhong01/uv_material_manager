"""View-aligned edge rotation with per-vertex guide rails."""

import json
import math

import bmesh
import bpy
from bpy.props import BoolProperty, FloatProperty, PointerProperty, StringProperty
from bpy.types import Operator, PropertyGroup
from mathutils import Vector


_EPS = 1e-6


class EdgeRailRotateSettings(PropertyGroup):
    runtime_data: StringProperty(options={'SKIP_SAVE'})
    running: BoolProperty(options={'SKIP_SAVE'})
    mouse_ready: BoolProperty(options={'SKIP_SAVE'})
    anchor_x: FloatProperty(options={'SKIP_SAVE'})
    anchor_y: FloatProperty(options={'SKIP_SAVE'})
    last_angle: FloatProperty(options={'SKIP_SAVE'})
    numeric_text: StringProperty(options={'SKIP_SAVE'})
    numeric_valid: BoolProperty(options={'SKIP_SAVE'})


def _selected_chain(bm, vertex_mode=False):
    if vertex_mode:
        selected_verts = [vert for vert in bm.verts if vert.select]
        if len(selected_verts) != 2:
            raise ValueError("点模式下请只选择一条边的两个端点")
        edges = [edge for edge in selected_verts[0].link_edges
                 if selected_verts[1] in edge.verts]
        if len(edges) != 1:
            raise ValueError("点模式下请选择一条边的两个端点")
    else:
        edges = [edge for edge in bm.edges if edge.select]
    if not edges:
        raise ValueError("请先选择一条连续的边线")
    selected = set(edges)
    verts = {vert for edge in edges for vert in edge.verts}
    degrees = {vert: sum(edge in selected for edge in vert.link_edges) for vert in verts}
    ends = [vert for vert, degree in degrees.items() if degree == 1]
    if any(degree > 2 or degree == 0 for degree in degrees.values()):
        raise ValueError("请选择一条不分叉的连续边线")

    if len(ends) == 2:
        ordered = [ends[0]]
        while ordered[-1] is not ends[1]:
            current = ordered[-1]
            previous = ordered[-2] if len(ordered) > 1 else None
            following = [edge.other_vert(current) for edge in current.link_edges
                         if edge in selected and edge.other_vert(current) is not previous]
            if len(following) != 1 or following[0] in ordered:
                raise ValueError("所选边不构成一条连续边线")
            ordered.append(following[0])
        if len(ordered) != len(verts):
            raise ValueError("请只选择一条边线，不要同时选择多个独立部分")
        return ordered, selected, False

    if not ends and all(degree == 2 for degree in degrees.values()):
        start = min(verts, key=lambda vert: vert.index)
        ordered = [start]
        previous_edge = None
        current = start
        while True:
            candidates = sorted((edge for edge in current.link_edges
                                 if edge in selected and edge is not previous_edge),
                                key=lambda edge: edge.index)
            if len(candidates) != 1 and previous_edge is None:
                candidates = sorted((edge for edge in current.link_edges if edge in selected),
                                    key=lambda edge: edge.index)
            if not candidates:
                raise ValueError("所选边不构成一条连续闭环")
            edge = candidates[0]
            following = edge.other_vert(current)
            if following is start:
                break
            if following in ordered:
                raise ValueError("所选边不构成一条连续闭环")
            ordered.append(following)
            previous_edge, current = edge, following
        if len(ordered) != len(verts) or len(selected) != len(ordered):
            raise ValueError("请只选择一条闭合边线，不要同时选择多个独立部分")
        return ordered, selected, True

    raise ValueError("请选择一条连续边线或一个闭合边环")


def _screen_point(matrix, world, width, height):
    clip = matrix @ Vector((world.x, world.y, world.z, 1.0))
    if clip.w <= _EPS:
        raise ValueError("所选边线位于视图背面，无法计算旋转")
    return Vector(((clip.x / clip.w + 1.0) * width * 0.5,
                   (clip.y / clip.w + 1.0) * height * 0.5))


def _dashed_screen_segments(points, closed, dash_length=7.0, gap_length=5.0):
    """Split a projected polyline into short on-screen dash segments."""
    pairs = list(zip(points, points[1:]))
    if closed and len(points) > 1:
        pairs.append((points[-1], points[0]))
    result = []
    period = dash_length + gap_length
    travelled = 0.0
    for start, end in pairs:
        if start is None or end is None:
            travelled = 0.0
            continue
        delta = end - start
        length = delta.length
        if length < _EPS:
            continue
        offset = 0.0
        while offset < length:
            phase = (travelled + offset) % period
            drawing = phase < dash_length
            step = min(length - offset,
                       dash_length - phase if drawing else period - phase)
            if step < _EPS:
                offset += _EPS
                continue
            if drawing:
                result.extend((start + delta * (offset / length),
                               start + delta * ((offset + step) / length)))
            offset += step
        travelled += length
    return result


def _draw_original_position(snapshot_json, area_pointer):
    context = bpy.context
    area = context.area
    region = context.region
    region_3d = context.region_data
    if (area is None or area.type != 'VIEW_3D' or region is None
            or region.type != 'WINDOW' or region_3d is None
            or area.as_pointer() != area_pointer):
        return

    from bpy_extras.view3d_utils import location_3d_to_region_2d
    import gpu
    from gpu_extras.batch import batch_for_shader
    from mathutils import Matrix

    data = json.loads(snapshot_json)
    world_matrix = Matrix(data['world_matrix'])
    projected = []
    for coord in data['coords']:
        point = location_3d_to_region_2d(
            region, region_3d, world_matrix @ Vector(coord))
        projected.append(Vector(point) if point is not None else None)
    dashes = _dashed_screen_segments(projected, data.get('closed', False))
    if not dashes:
        return

    positions = [(point.x, point.y, 0.0) for point in dashes]
    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    batch = batch_for_shader(shader, 'LINES', {'pos': positions})
    previous_blend = gpu.state.blend_get()
    previous_depth_test = gpu.state.depth_test_get()
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    try:
        shader.bind()
        shader.uniform_float('viewportSize', gpu.state.viewport_get()[2:])
        shader.uniform_float('lineWidth', 1.5)
        shader.uniform_float('color', (0.72, 0.76, 0.80, 0.72))
        batch.draw(shader)
    finally:
        gpu.state.depth_test_set(previous_depth_test)
        gpu.state.blend_set(previous_blend)


def _rail_path(start, first_edge, excluded, max_edges, junction_mode='CLOSEST'):
    """Return local-space rail vertices, following the least-turn branch."""
    current = start
    edge = first_edge
    visited = set(excluded)
    points = [tuple(start.co)]
    for _ in range(max_edges):
        visited.add(edge)
        following = edge.other_vert(current)
        points.append(tuple(following.co))
        incoming = following.co - current.co
        if incoming.length < _EPS:
            break
        candidates = []
        for candidate in following.link_edges:
            if candidate in visited:
                continue
            outgoing = candidate.other_vert(following).co - following.co
            if outgoing.length < _EPS:
                continue
            alignment = incoming.normalized().dot(outgoing.normalized())
            if alignment > _EPS:
                candidates.append((alignment, candidate))
        if not candidates:
            break
        if junction_mode == 'STRAIGHT' and len(candidates) > 1:
            break
        edge = max(candidates, key=lambda item: item[0])[1]
        current = following
    return points


def _project_clip(matrix, world, width, height):
    clip = matrix @ Vector((world.x, world.y, world.z, 1.0))
    if clip.w <= _EPS:
        return None
    point = Vector(((clip.x / clip.w + 1.0) * width * 0.5,
                    (clip.y / clip.w + 1.0) * height * 0.5))
    return point, clip.w


def _closest_rail_point(target, paths, world_matrix, matrix, width, height, boundary):
    """Find the rail position nearest a projected target, including its UV slide distance."""
    best = None
    for path in paths:
        world_path = [world_matrix @ Vector(point) for point in path]
        travelled = 0.0
        for start, end in zip(world_path, world_path[1:]):
            projected_start = _project_clip(matrix, start, width, height)
            projected_end = _project_clip(matrix, end, width, height)
            segment_length = (end - start).length
            if projected_start is None or projected_end is None or segment_length < _EPS:
                travelled += segment_length
                continue
            point_a, weight_a = projected_start
            point_b, weight_b = projected_end
            screen_segment = point_b - point_a
            screen_length_sq = screen_segment.length_squared
            if screen_length_sq < _EPS:
                screen_factor = 0.0
            else:
                screen_factor = max(0.0, min(1.0,
                    (target - point_a).dot(screen_segment) / screen_length_sq))
            denominator = ((1.0 - screen_factor) * weight_b
                           + screen_factor * weight_a)
            if abs(denominator) < _EPS:
                travelled += segment_length
                continue
            world_factor = screen_factor * weight_a / denominator
            world_factor = max(0.0, min(1.0, world_factor))
            world_point = start.lerp(end, world_factor)
            projected = _project_clip(matrix, world_point, width, height)
            if projected is not None:
                error = (projected[0] - target).length_squared
                distance = travelled + segment_length * world_factor
                candidate = (error, world_point, distance, world_path[1] - world_path[0])
                if best is None or candidate[0] < best[0]:
                    best = candidate
            travelled += segment_length

        if boundary == 'EXTEND' and len(world_path) >= 2:
            start, end = world_path[-2], world_path[-1]
            projected_start = _project_clip(matrix, start, width, height)
            projected_end = _project_clip(matrix, end, width, height)
            segment_length = (end - start).length
            if (projected_start is not None and projected_end is not None
                    and segment_length >= _EPS):
                point_a, weight_a = projected_start
                point_b, weight_b = projected_end
                screen_segment = point_b - point_a
                screen_length_sq = screen_segment.length_squared
                if screen_length_sq >= _EPS:
                    screen_factor = max(1.0,
                        (target - point_a).dot(screen_segment) / screen_length_sq)
                    denominator = ((1.0 - screen_factor) * weight_b
                                   + screen_factor * weight_a)
                    if abs(denominator) >= _EPS:
                        world_factor = screen_factor * weight_a / denominator
                        if world_factor >= 1.0:
                            world_point = start.lerp(end, world_factor)
                            projected = _project_clip(matrix, world_point, width, height)
                            if projected is not None:
                                error = (projected[0] - target).length_squared
                                distance = travelled + segment_length * (world_factor - 1.0)
                                candidate = (error, world_point, distance,
                                             world_path[1] - world_path[0])
                                if best is None or candidate[0] < best[0]:
                                    best = candidate
    return best


def _capture_uv(bm, ordered, selected_edges, world_matrix, rail_refs):
    """Store UV values and a local slide derivative for every affected face corner."""
    result = []
    for layer in bm.loops.layers.uv.values():
        for vertex in ordered:
            rail_reference = Vector(rail_refs[str(vertex.index)])
            rail_neighbors = {
                edge.other_vert(vertex)
                for edge in vertex.link_edges if edge not in selected_edges
            }
            corner_data = []
            for face in vertex.link_faces:
                for loop_index, loop in enumerate(face.loops):
                    if loop.vert is not vertex:
                        continue
                    original_uv = Vector(loop[layer].uv)
                    slope = Vector((0.0, 0.0))
                    best_length = 0.0
                    for neighbor_loop in (loop.link_loop_next, loop.link_loop_prev):
                        neighbor = neighbor_loop.vert
                        if neighbor not in rail_neighbors:
                            continue
                        delta = world_matrix @ neighbor.co - world_matrix @ vertex.co
                        signed_length = delta.dot(rail_reference)
                        if abs(signed_length) > best_length + _EPS:
                            best_length = abs(signed_length)
                            slope = (Vector(neighbor_loop[layer].uv) - original_uv) / signed_length
                    corner_data.append((face.index, loop_index, original_uv, slope))

            # A continuous UV island must remain continuous on the selected line.
            for face_index, loop_index, uv, slope in corner_data:
                same_uv = [item[3] for item in corner_data if (item[2] - uv).length < _EPS]
                average = sum(same_uv, Vector((0.0, 0.0))) / len(same_uv)
                result.append((vertex.index, face_index, loop_index, layer.name,
                               tuple(uv), tuple(average)))
    return result


def _pivot_world(context, ordered, world_matrix):
    points = [world_matrix @ vertex.co for vertex in ordered]
    mode = context.scene.tool_settings.transform_pivot_point
    if mode == 'CURSOR':
        return context.scene.cursor.location.copy()
    if mode == 'ACTIVE_ELEMENT':
        active = context.active_object.data
        bm = bmesh.from_edit_mesh(active)
        element = bm.select_history.active
        if isinstance(element, bmesh.types.BMVert) and element in ordered:
            return world_matrix @ element.co
        if isinstance(element, bmesh.types.BMEdge) and all(v in ordered for v in element.verts):
            return world_matrix @ ((element.verts[0].co + element.verts[1].co) * 0.5)
    if mode == 'BOUNDING_BOX_CENTER':
        return Vector(tuple((min(p[i] for p in points) + max(p[i] for p in points)) * 0.5
                            for i in range(3)))
    return sum(points, Vector((0.0, 0.0, 0.0))) / len(points)


class MESH_OT_EdgeRailRotate(Operator):
    bl_idname = "mesh.edge_rail_rotate"
    bl_label = "滑动旋转"
    bl_description = "让所选边线沿相邻网格边滑动旋转，并尽量保持原有形状"
    bl_options = {'REGISTER', 'UNDO'}

    angle: FloatProperty(name="旋转角度", subtype='ANGLE', default=0.0)
    follow_closest_edge: BoolProperty(
        name="岔路：沿最接近的边", description="关闭时沿当前导轨的方向延长", default=False)
    stop_at_boundary: BoolProperty(
        name="到边界时停止", description="关闭时沿导轨末段方向继续延长", default=False)
    snapshot_json: StringProperty(options={'HIDDEN'})

    def draw(self, context):
        column = self.layout.column()
        column.prop(self, 'angle')
        column.prop(self, 'follow_closest_edge')
        column.prop(self, 'stop_at_boundary')

    def _remove_original_draw(self):
        handle = getattr(self, '_original_draw_handle', None)
        if handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
            self._original_draw_handle = None

    def _clear_modal_state(self, context, config):
        self._remove_original_draw()
        config.running = False
        config.runtime_data = ""
        context.area.header_text_set(None)
        context.area.tag_redraw()

    def execute(self, context):
        if not self.snapshot_json:
            self.report({'WARNING'}, "请从 3D 视图按钮启动滑动旋转")
            return {'CANCELLED'}
        try:
            data = json.loads(self.snapshot_json)
            if (context.active_object is None or context.active_object.name != data['object']
                    or context.active_object.data.name != data['mesh']):
                raise ValueError("编辑对象已改变，无法调整上一步操作")
            data['junction'] = 'CLOSEST' if self.follow_closest_edge else 'STRAIGHT'
            data['boundary'] = 'STOP' if self.stop_at_boundary else 'EXTEND'
            positions = self._positions(data, self.angle)
            if positions is None:
                raise ValueError("当前角度无法在所选边界设置下保持整条边线")
            self._restore(context, data)
            self._apply(context, data, positions)
            self._remove_original_draw()
            return {'FINISHED'}
        except (ValueError, RuntimeError, ReferenceError, KeyError, IndexError,
                ZeroDivisionError) as exc:
            self._remove_original_draw()
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}

    @classmethod
    def poll(cls, context):
        select_mode = context.tool_settings.mesh_select_mode if context.tool_settings else ()
        return (context.mode == 'EDIT_MESH' and context.active_object is not None
                and context.active_object.type == 'MESH'
                and context.area is not None and context.area.type == 'VIEW_3D'
                and len(select_mode) >= 3 and not select_mode[2]
                and (select_mode[0] or select_mode[1]))

    def invoke(self, context, event):
        obj = context.active_object
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        bm.verts.index_update()
        bm.faces.index_update()
        try:
            vertex_mode = context.tool_settings.mesh_select_mode[0]
            ordered, selected_edges, closed = _selected_chain(bm, vertex_mode=vertex_mode)
            region = next((item for item in context.area.regions if item.type == 'WINDOW'), None)
            if region is None:
                raise ValueError("找不到 3D 视图区域")
            view = context.area.spaces.active.region_3d
            matrix = view.perspective_matrix.copy()
            world_matrix = obj.matrix_world.copy()
            if abs(world_matrix.determinant()) < _EPS:
                raise ValueError("物体变换不可逆，请先检查物体缩放")
            screen = [_screen_point(matrix, world_matrix @ v.co, region.width, region.height)
                      for v in ordered]
            screen_segments = list(zip(screen, screen[1:]))
            if closed:
                screen_segments.append((screen[-1], screen[0]))
            screen_length = sum((b - a).length for a, b in screen_segments)
            if screen_length < 8.0:
                raise ValueError("所选边线在视图中太短，请放大后重试")

            config = context.scene.edge_rail_rotate_settings
            rail_data = {}
            rail_refs = {}
            default_junction = 'CLOSEST' if self.follow_closest_edge else 'STRAIGHT'
            for vertex in ordered:
                paths_by_mode = {'STRAIGHT': [], 'CLOSEST': []}
                for mode in paths_by_mode:
                    for edge in vertex.link_edges:
                        if edge in selected_edges:
                            continue
                        path = _rail_path(vertex, edge, selected_edges, len(bm.edges), mode)
                        if len(path) >= 2:
                            paths_by_mode[mode].append(path)
                if not paths_by_mode[default_junction]:
                    raise ValueError(f"顶点 {vertex.index} 没有可滑动的相邻边")
                rail_data[str(vertex.index)] = paths_by_mode
                reference = (world_matrix @ Vector(paths_by_mode[default_junction][0][1])
                             - world_matrix @ vertex.co)
                rail_refs[str(vertex.index)] = tuple(reference.normalized())

            data = {
                'object': obj.name,
                'mesh': obj.data.name,
                'verts': [vertex.index for vertex in ordered],
                'closed': closed,
                'coords': [tuple(vertex.co) for vertex in ordered],
                'uv': _capture_uv(bm, ordered, selected_edges, world_matrix, rail_refs),
                'rails': rail_data,
                'rail_refs': rail_refs,
                'matrix': [tuple(row) for row in matrix],
                'world_matrix': [tuple(row) for row in world_matrix],
                'width': region.width,
                'height': region.height,
                'screen_points': [tuple(point) for point in screen],
                'pivot': tuple(_screen_point(matrix, _pivot_world(context, ordered, world_matrix),
                                             region.width, region.height)),
                'junction': 'CLOSEST' if self.follow_closest_edge else 'STRAIGHT',
                'boundary': 'STOP' if self.stop_at_boundary else 'EXTEND',
            }
            self.snapshot_json = json.dumps(data)
            config.runtime_data = self.snapshot_json
            config.running = True
            config.last_angle = 0.0
            config.numeric_text = ""
            config.numeric_valid = True
            config.mouse_ready = False
            if (region.x <= event.mouse_x < region.x + region.width
                    and region.y <= event.mouse_y < region.y + region.height):
                config.anchor_x = event.mouse_x - region.x
                config.anchor_y = event.mouse_y - region.y
                config.mouse_ready = True
            self._original_draw_handle = bpy.types.SpaceView3D.draw_handler_add(
                _draw_original_position,
                (self.snapshot_json, context.area.as_pointer()),
                'WINDOW', 'POST_PIXEL')
            context.window_manager.modal_handler_add(self)
            context.area.header_text_set("滑动旋转：将鼠标移入视图后移动，或输入角度；左键/Enter 确认，Esc 取消")
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        except (ValueError, RuntimeError, ReferenceError) as exc:
            self._remove_original_draw()
            config = context.scene.edge_rail_rotate_settings
            config.running = False
            config.runtime_data = ""
            context.area.header_text_set(None)
            context.area.tag_redraw()
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}

    @staticmethod
    def _restore(context, data):
        obj = bpy.data.objects.get(data['object'])
        if obj is None or obj.type != 'MESH' or obj.data.name != data['mesh']:
            return
        if obj.mode != 'EDIT':
            mesh = obj.data
            for index, coord in zip(data['verts'], data['coords']):
                mesh.vertices[index].co = coord
            for _vert, face, loop, layer_name, uv, _slope in data['uv']:
                layer = mesh.uv_layers.get(layer_name)
                if layer is not None:
                    loop_index = mesh.polygons[face].loop_start + loop
                    layer.data[loop_index].uv = uv
            mesh.update()
            return
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        for index, coord in zip(data['verts'], data['coords']):
            bm.verts[index].co = coord
        layers = {layer.name: layer for layer in bm.loops.layers.uv.values()}
        for _vert, face, loop, layer_name, uv, _slope in data['uv']:
            layer = layers.get(layer_name)
            if layer is not None:
                bm.faces[face].loops[loop][layer].uv = uv
        bm.normal_update()
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)

    @staticmethod
    def _positions(data, angle):
        from mathutils import Matrix

        matrix = Matrix(data['matrix'])
        world_matrix = Matrix(data['world_matrix'])
        inverse_world = world_matrix.inverted_safe()
        original_screen = [Vector(point) for point in data['screen_points']]
        pivot = Vector(data['pivot'])
        cosine, sine = math.cos(angle), math.sin(angle)
        targets = []
        for point in original_screen:
            delta = point - pivot
            targets.append(pivot + Vector((delta.x * cosine - delta.y * sine,
                                           delta.x * sine + delta.y * cosine)))

        indices = data['verts']
        world_positions = []
        rail_distances = []
        for position, index in enumerate(indices):
            rail_paths = data['rails'][str(index)]
            if isinstance(rail_paths, dict):
                rail_paths = rail_paths.get(data['junction'], [])
            candidate = _closest_rail_point(
                targets[position], rail_paths, world_matrix, matrix,
                data['width'], data['height'], data['boundary'])
            if candidate is None:
                return None
            _error, world, travelled, first_direction = candidate
            rail_reference = Vector(data['rail_refs'][str(index)])
            sign = 1.0 if first_direction.dot(rail_reference) >= 0 else -1.0
            world_positions.append(world)
            rail_distances.append(sign * travelled)

        # Relax neighboring points against the rotated original segment vectors.
        # This keeps curved chains from being forced onto a single straight target line.
        closed = data.get('closed', False)
        for _iteration in range(6):
            projected = []
            for world in world_positions:
                point = _project_clip(matrix, world, data['width'], data['height'])
                if point is None:
                    return None
                projected.append(point[0])
            for position, index in enumerate(indices):
                weighted_target = targets[position] * 2.0
                total_weight = 2.0
                previous = position - 1 if position > 0 else (len(indices) - 1 if closed else None)
                following = position + 1 if position + 1 < len(indices) else (0 if closed else None)
                if previous is not None:
                    desired_delta = targets[position] - targets[previous]
                    weighted_target += (projected[previous] + desired_delta)
                    total_weight += 1.0
                if following is not None:
                    desired_delta = targets[following] - targets[position]
                    weighted_target += (projected[following] - desired_delta)
                    total_weight += 1.0
                desired = weighted_target / total_weight
                rail_paths = data['rails'][str(index)]
                if isinstance(rail_paths, dict):
                    rail_paths = rail_paths.get(data['junction'], [])
                candidate = _closest_rail_point(
                    desired, rail_paths, world_matrix, matrix,
                    data['width'], data['height'], data['boundary'])
                if candidate is None:
                    return None
                _error, world_positions[position], travelled, first_direction = candidate
                rail_reference = Vector(data['rail_refs'][str(index)])
                sign = 1.0 if first_direction.dot(rail_reference) >= 0 else -1.0
                rail_distances[position] = sign * travelled

        return [(inverse_world @ world, distance)
                for world, distance in zip(world_positions, rail_distances)]

    @staticmethod
    def _apply(context, data, positions):
        obj = context.active_object
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        for index, (coord, _distance) in zip(data['verts'], positions):
            bm.verts[index].co = coord
        layers = {layer.name: layer for layer in bm.loops.layers.uv.values()}
        distances = {index: distance for index, (_coord, distance) in zip(data['verts'], positions)}
        for index, face, loop, layer_name, uv, slope in data['uv']:
            layer = layers.get(layer_name)
            if layer is not None:
                bm.faces[face].loops[loop][layer].uv = (
                    Vector(uv) + Vector(slope) * distances[index]
                )
        bm.normal_update()
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        return True

    def _preview(self, context, config, data, angle):
        positions = self._positions(data, angle)
        if positions is None:
            context.area.header_text_set("滑动旋转：此角度无法沿全部导轨滑动；试另一方向或角度")
            return False
        self._restore(context, data)
        self._apply(context, data, positions)
        config.last_angle = angle
        context.area.header_text_set(
            f"滑动旋转 {math.degrees(angle):.1f}°  |  左键/Enter 确认  |  右键/Esc 取消")
        context.area.tag_redraw()
        return True

    def modal(self, context, event):
        config = context.scene.edge_rail_rotate_settings
        if not config.running or not config.runtime_data:
            self._clear_modal_state(context, config)
            return {'CANCELLED'}
        data = json.loads(config.runtime_data)
        if (context.mode != 'EDIT_MESH' or context.active_object is None
                or context.active_object.name != data['object']
                or context.active_object.data.name != data['mesh']):
            self._restore(context, data)
            self._clear_modal_state(context, config)
            self.report({'WARNING'}, "编辑对象已改变，滑动旋转已停止")
            return {'CANCELLED'}

        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            self._restore(context, data)
            self._clear_modal_state(context, config)
            return {'CANCELLED'}
        if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            if config.numeric_text and not config.numeric_valid:
                self.report({'WARNING'}, "该角度无法同时保持边线形状和滑动约束")
                return {'RUNNING_MODAL'}
            if abs(config.last_angle) < _EPS:
                self._restore(context, data)
                self._clear_modal_state(context, config)
                return {'CANCELLED'}
            self.angle = config.last_angle
            self._clear_modal_state(context, config)
            self.report({'INFO'}, f"已沿边滑动旋转 {math.degrees(config.last_angle):.1f}°")
            return {'FINISHED'}
        digits = {
            'ZERO': '0', 'ONE': '1', 'TWO': '2', 'THREE': '3', 'FOUR': '4',
            'FIVE': '5', 'SIX': '6', 'SEVEN': '7', 'EIGHT': '8', 'NINE': '9',
        }
        digits.update({f'NUMPAD_{number}': str(number) for number in range(10)})
        if event.value == 'PRESS' and event.type in digits | {
                'PERIOD': '.', 'NUMPAD_PERIOD': '.', 'MINUS': '-', 'NUMPAD_MINUS': '-'}:
            character = (digits | {'PERIOD': '.', 'NUMPAD_PERIOD': '.',
                                   'MINUS': '-', 'NUMPAD_MINUS': '-'})[event.type]
            if (character == '.' and '.' in config.numeric_text
                    or character == '-' and config.numeric_text):
                return {'RUNNING_MODAL'}
            config.numeric_text += character
        elif event.value == 'PRESS' and event.type == 'BACK_SPACE':
            config.numeric_text = config.numeric_text[:-1]
            if not config.numeric_text:
                self._restore(context, data)
                config.last_angle = 0.0
                config.numeric_valid = True
                return {'RUNNING_MODAL'}
        elif event.type != 'MOUSEMOVE':
            return {'RUNNING_MODAL'}

        if config.numeric_text:
            try:
                angle = math.radians(float(config.numeric_text))
                config.numeric_valid = self._preview(context, config, data, angle)
            except (ValueError, ReferenceError, RuntimeError, KeyError, IndexError,
                    ZeroDivisionError) as exc:
                if isinstance(exc, ValueError) and config.numeric_text in {'-', '.', '-.'}:
                    config.numeric_valid = False
                    return {'RUNNING_MODAL'}
                self._restore(context, data)
                self._clear_modal_state(context, config)
                self.report({'ERROR'}, f"滑动旋转失败：{exc}")
                return {'CANCELLED'}
            return {'RUNNING_MODAL'}

        region = next((item for item in context.area.regions if item.type == 'WINDOW'), None)
        if region is None or not (region.x <= event.mouse_x < region.x + region.width
                                  and region.y <= event.mouse_y < region.y + region.height):
            return {'RUNNING_MODAL'}
        current = Vector((event.mouse_x - region.x, event.mouse_y - region.y))
        if not config.mouse_ready:
            config.anchor_x, config.anchor_y = current
            config.mouse_ready = True
            context.area.header_text_set("滑动旋转：起点已定位，继续移动鼠标；或直接输入角度")
            return {'RUNNING_MODAL'}
        pivot = Vector(data['pivot'])
        initial = Vector((config.anchor_x, config.anchor_y)) - pivot
        moved = current - pivot
        if initial.length < 5.0 or moved.length < 5.0:
            angle = (current.x - config.anchor_x) * 0.005
        else:
            angle = math.atan2(initial.cross(moved), initial.dot(moved))
        if event.shift:
            angle *= 0.1
        if event.ctrl:
            angle = round(angle / math.radians(5.0)) * math.radians(5.0)

        try:
            self._preview(context, config, data, angle)
        except (ReferenceError, RuntimeError, ValueError, KeyError, IndexError, ZeroDivisionError) as exc:
            self._restore(context, data)
            self._clear_modal_state(context, config)
            self.report({'ERROR'}, f"滑动旋转失败：{exc}")
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}


_CLASSES = (EdgeRailRotateSettings, MESH_OT_EdgeRailRotate)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.edge_rail_rotate_settings = PointerProperty(type=EdgeRailRotateSettings)


def unregister():
    if hasattr(bpy.types.Scene, 'edge_rail_rotate_settings'):
        del bpy.types.Scene.edge_rail_rotate_settings
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
