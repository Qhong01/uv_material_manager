"""Viewport gesture for adding an axis-aligned Mirror modifier."""

import json
import math

import blf
import bpy
import bpy_extras
import gpu
from bpy.props import BoolProperty, FloatProperty, FloatVectorProperty
from bpy.props import PointerProperty, StringProperty
from bpy.types import Operator, PropertyGroup
from gpu_extras.batch import batch_for_shader
from mathutils import Vector


_PREVIEW_THRESHOLD = 4.0
_APPLY_RADIUS = 88.0


def _find_cursor_pivot(scene, location, rotation):
    cursor_location = Vector(location)
    for obj in scene.objects:
        if (obj.type != 'EMPTY'
                or not obj.name.startswith("UVMM Mirror Pivot")
                or (Vector(obj.location) - cursor_location).length >= 1e-6):
            continue
        pivot_rotation = obj.rotation_euler.to_quaternion()
        if pivot_rotation.rotation_difference(rotation).angle < 1e-6:
            return obj
    return None


class QuickMirrorRuntimeState(PropertyGroup):
    running: BoolProperty(options={'SKIP_SAVE'})
    area_pointer: StringProperty(options={'SKIP_SAVE'})
    anchor_ready: BoolProperty(options={'SKIP_SAVE'})
    anchor_x: FloatProperty(options={'SKIP_SAVE'})
    anchor_y: FloatProperty(options={'SKIP_SAVE'})
    mouse_x: FloatProperty(options={'SKIP_SAVE'})
    mouse_y: FloatProperty(options={'SKIP_SAVE'})
    preview_axis: StringProperty(options={'SKIP_SAVE'})
    base_axes: StringProperty(options={'SKIP_SAVE'})
    base_bisect_axes: StringProperty(options={'SKIP_SAVE'})
    targets_json: StringProperty(options={'SKIP_SAVE'})
    reference_object: StringProperty(options={'SKIP_SAVE'})


class OBJECT_OT_QuickMirrorGesture(Operator):
    bl_idname = "object.quick_mirror_gesture"
    bl_label = "快速镜像"
    bl_description = "在视口中拖动鼠标，按当前视角推断镜像轴并添加 Mirror 修改器"
    bl_options = {'REGISTER', 'UNDO'}

    use_3d_cursor: BoolProperty(name="3D 游标", default=False)
    axis_x: BoolProperty(name="X", default=True)
    axis_y: BoolProperty(name="Y", default=False)
    axis_z: BoolProperty(name="Z", default=False)
    bisect_x: BoolProperty(name="X", default=False)
    bisect_y: BoolProperty(name="Y", default=False)
    bisect_z: BoolProperty(name="Z", default=False)
    mirror_object_name: StringProperty(name="镜像物体")
    use_clip: BoolProperty(name="范围限制", default=False)
    use_mirror_merge: BoolProperty(name="合并", default=True)
    merge_threshold: FloatProperty(
        name="合并距离", default=0.001, min=0.0, precision=4, subtype='DISTANCE'
    )
    cursor_location: FloatVectorProperty(
        name="游标位置",
        size=3,
        subtype='TRANSLATION',
        options={'HIDDEN'},
    )
    targets_json: StringProperty(options={'HIDDEN'})
    reference_object: StringProperty(options={'HIDDEN'})

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "use_3d_cursor", text="3D 游标")

        row = layout.row(align=True)
        row.label(text="轴向")
        for prop_name in ("axis_x", "axis_y", "axis_z"):
            row.prop(self, prop_name, toggle=True)

        row = layout.row(align=True)
        row.label(text="切分")
        for prop_name in ("bisect_x", "bisect_y", "bisect_z"):
            row.prop(self, prop_name, toggle=True)

        object_row = layout.row()
        object_row.enabled = not self.use_3d_cursor
        object_row.prop_search(self, "mirror_object_name", bpy.data, "objects",
                               text="镜像物体")
        layout.prop(self, "use_clip", text="范围限制")

        merge_row = layout.row(align=True)
        merge_row.prop(self, "use_mirror_merge", text="合并")
        threshold_row = merge_row.row(align=True)
        threshold_row.enabled = self.use_mirror_merge
        threshold_row.prop(self, "merge_threshold", text="")

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT'
                and context.area is not None
                and context.area.type == 'VIEW_3D'
                and any(obj.type == 'MESH' for obj in context.selected_objects))

    def _remove_draw_handler(self):
        draw_handler = getattr(self, "_draw_handler", None)
        if draw_handler is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(draw_handler, 'WINDOW')
            except (RuntimeError, ValueError):
                pass
            self._draw_handler = None

    def _clear_runtime(self, context, state):
        self._remove_draw_handler()
        target_area_pointer = state.area_pointer
        state.running = False
        state.area_pointer = ""
        state.anchor_ready = False
        state.preview_axis = ""
        state.base_axes = ""
        state.base_bisect_axes = ""
        state.targets_json = ""
        state.reference_object = ""
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if (area.type == 'VIEW_3D'
                        and str(area.as_pointer()) == target_area_pointer):
                    area.header_text_set(None)
                    area.tag_redraw()

    def _draw_preview(self):
        context = bpy.context
        area = context.area
        region = context.region
        if (area is None or area.type != 'VIEW_3D' or region is None
                or region.type != 'WINDOW'):
            return

        state = context.scene.quick_mirror_runtime
        if (not state.running
                or str(area.as_pointer()) != state.area_pointer
                or not state.anchor_ready):
            return

        ring_points = []
        dash_count = 32
        for index in range(dash_count):
            start_angle = index * math.tau / dash_count
            end_angle = start_angle + math.tau * 0.55 / dash_count
            ring_points.extend((
                (state.anchor_x + _APPLY_RADIUS * math.cos(start_angle),
                 state.anchor_y + _APPLY_RADIUS * math.sin(start_angle)),
                (state.anchor_x + _APPLY_RADIUS * math.cos(end_angle),
                 state.anchor_y + _APPLY_RADIUS * math.sin(end_angle)),
            ))
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        batch = batch_for_shader(shader, 'LINES', {'pos': ring_points})
        try:
            gpu.state.blend_set('ALPHA')
            gpu.state.line_width_set(1.5)
            shader.bind()
            shader.uniform_float('color', (0.88, 0.9, 0.95, 0.82))
            batch.draw(shader)
        finally:
            gpu.state.line_width_set(1.0)
            gpu.state.blend_set('NONE')

        self._draw_axis_indicator(context, state, region, shader)

    def _draw_axis_indicator(self, context, state, region, shader):
        obj = bpy.data.objects.get(state.reference_object)
        if obj is None or obj.type != 'MESH':
            return

        area = context.area
        region_3d = area.spaces.active.region_3d
        bounds_center = sum((Vector(corner) for corner in obj.bound_box), Vector()) / 8.0
        axis_origin_world = obj.matrix_world @ bounds_center
        axis_origin_screen = bpy_extras.view3d_utils.location_3d_to_region_2d(
            region, region_3d, axis_origin_world
        )
        if axis_origin_screen is None:
            return
        center = Vector((state.anchor_x, state.anchor_y))
        axis_origin_screen = Vector(axis_origin_screen)
        drag = Vector((state.mouse_x - state.anchor_x,
                       state.mouse_y - state.anchor_y))
        axis_colors = {
            'X': (1.0, 0.22, 0.18),
            'Y': (0.24, 1.0, 0.3),
            'Z': (0.25, 0.56, 1.0),
        }
        local_axes = (
            ('X', Vector((1.0, 0.0, 0.0))),
            ('Y', Vector((0.0, 1.0, 0.0))),
            ('Z', Vector((0.0, 0.0, 1.0))),
        )
        step = max(max(obj.dimensions), 1e-3) * 0.35
        draw_lines = []
        labels = []
        for axis_name, local_axis in local_axes:
            world_axis = obj.matrix_world.to_3x3() @ local_axis
            if world_axis.length_squared < 1e-12:
                continue
            world_axis.normalize()
            axis_screen = bpy_extras.view3d_utils.location_3d_to_region_2d(
                region, region_3d, axis_origin_world + world_axis * step
            )
            if axis_screen is None:
                continue
            direction = Vector(axis_screen) - axis_origin_screen
            if direction.length_squared < 1e-6:
                continue
            direction.normalize()

            negative_end = center - direction * 23.0
            positive_end = center + direction * 46.0
            draw_lines.append((axis_name, [tuple(negative_end), tuple(center),
                                           tuple(center), tuple(positive_end)]))
            labels.append((axis_name, positive_end + direction * 4.0,
                           axis_colors[axis_name]))

            if state.preview_axis == axis_name:
                sign = 1.0 if drag.dot(direction) >= 0.0 else -1.0
                highlight_end = center + direction * (46.0 * sign)
                draw_lines.append((f'{axis_name}_active', [tuple(center),
                                                            tuple(highlight_end)]))
                perpendicular = Vector((-direction.y, direction.x))
                arrow_back = highlight_end - direction * (8.0 * sign)
                arrow_side_a = arrow_back + perpendicular * 4.0
                arrow_side_b = arrow_back - perpendicular * 4.0
                draw_lines.append((f'{axis_name}_active', [
                    tuple(highlight_end), tuple(arrow_side_a),
                    tuple(highlight_end), tuple(arrow_side_b),
                ]))
                sign_label = '+' if sign > 0.0 else '−'
                labels.append((f'{sign_label}{axis_name}',
                               highlight_end + direction * (sign * 8.0),
                               axis_colors[axis_name]))

        try:
            gpu.state.blend_set('ALPHA')
            for label, points in draw_lines:
                selected = label.endswith('_active')
                axis_name = label.split('_', 1)[0]
                color = axis_colors[axis_name]
                shader.bind()
                shader.uniform_float('color', (*color, 1.0 if selected else 0.62))
                gpu.state.line_width_set(3.0 if selected else 1.4)
                batch_for_shader(shader, 'LINES', {'pos': points}).draw(shader)
        finally:
            gpu.state.line_width_set(1.0)
            gpu.state.blend_set('NONE')

        blf.size(0, 13)
        blf.enable(0, blf.SHADOW)
        blf.shadow(0, 3, 0.0, 0.0, 0.0, 0.9)
        for label, position, color in labels:
            blf.color(0, *color, 1.0)
            blf.position(0, position.x, position.y, 0)
            blf.draw(0, label)
        blf.disable(0, blf.SHADOW)

    @staticmethod
    def _infer_axis(context, obj, drag):
        region = next((item for item in context.area.regions if item.type == 'WINDOW'), None)
        if region is None:
            return None

        region_3d = context.area.spaces.active.region_3d
        bounds_center = sum((Vector(corner) for corner in obj.bound_box), Vector()) / 8.0
        world_center = obj.matrix_world @ bounds_center
        center_screen = bpy_extras.view3d_utils.location_3d_to_region_2d(
            region, region_3d, world_center
        )
        if center_screen is None:
            return None

        drag_direction = Vector(drag)
        if drag_direction.length_squared < 1e-12:
            return None
        drag_direction.normalize()

        step = max(max(obj.dimensions), 1e-3) * 0.5
        best_axis = None
        best_score = -1.0
        local_axes = (
            ('X', Vector((1.0, 0.0, 0.0))),
            ('Y', Vector((0.0, 1.0, 0.0))),
            ('Z', Vector((0.0, 0.0, 1.0))),
        )
        for axis_name, local_axis in local_axes:
            world_axis = obj.matrix_world.to_3x3() @ local_axis
            if world_axis.length_squared < 1e-12:
                continue
            world_axis.normalize()
            axis_screen = bpy_extras.view3d_utils.location_3d_to_region_2d(
                region, region_3d, world_center + world_axis * step
            )
            if axis_screen is None:
                continue
            projected_axis = Vector(axis_screen) - Vector(center_screen)
            if projected_axis.length_squared < 1e-6:
                continue
            score = abs(projected_axis.normalized().dot(drag_direction))
            if score > best_score:
                best_axis = axis_name
                best_score = score
        return best_axis

    def invoke(self, context, event):
        targets = [obj for obj in context.selected_objects if obj.type == 'MESH']
        reference = context.active_object if context.active_object in targets else targets[0]
        self.targets_json = json.dumps([obj.name for obj in targets], ensure_ascii=False)
        self.reference_object = reference.name
        self.cursor_location = tuple(context.scene.cursor.location)

        state = context.scene.quick_mirror_runtime
        if state.running:
            self.report({'WARNING'}, "快速镜像已在运行，请先按 Esc 取消")
            return {'CANCELLED'}

        state.running = True
        state.area_pointer = str(context.area.as_pointer())
        state.targets_json = self.targets_json
        state.reference_object = self.reference_object
        state.anchor_ready = False
        state.preview_axis = ""

        if self.use_3d_cursor:
            pivot_object = _find_cursor_pivot(
                context.scene,
                self.cursor_location,
                reference.matrix_world.to_quaternion(),
            )
        else:
            pivot_object = bpy.data.objects.get(self.mirror_object_name)
        existing = next((item for item in reference.modifiers
                         if item.type == 'MIRROR'
                         and item.mirror_object == pivot_object), None)
        base_axes = list(existing.use_axis) if existing is not None else [False] * 3
        base_bisect_axes = (list(existing.use_bisect_axis) if existing is not None
                            else [False] * 3)
        state.base_axes = json.dumps(base_axes)
        state.base_bisect_axes = json.dumps(base_bisect_axes)
        self.axis_x, self.axis_y, self.axis_z = base_axes
        self.bisect_x, self.bisect_y, self.bisect_z = base_bisect_axes
        if existing is not None:
            self.use_clip = existing.use_clip
            self.use_mirror_merge = existing.use_mirror_merge
            self.merge_threshold = existing.merge_threshold

        region = next((item for item in context.area.regions if item.type == 'WINDOW'), None)
        if (region is not None and region.x <= event.mouse_x < region.x + region.width
                and region.y <= event.mouse_y < region.y + region.height):
            state.anchor_x = event.mouse_x - region.x
            state.anchor_y = event.mouse_y - region.y
            state.mouse_x = state.anchor_x
            state.mouse_y = state.anchor_y
            state.anchor_ready = True

        context.area.header_text_set("快速镜像：拖出虚线范围自动应用；Esc 取消")
        try:
            self._draw_handler = bpy.types.SpaceView3D.draw_handler_add(
                self._draw_preview, (), 'WINDOW', 'POST_PIXEL'
            )
        except RuntimeError as exc:
            self._clear_runtime(context, state)
            self.report({'ERROR'}, f"无法显示快速镜像预览：{exc}")
            return {'CANCELLED'}
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        state = context.scene.quick_mirror_runtime
        if not state.running:
            self._remove_draw_handler()
            return {'CANCELLED'}

        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            self._clear_runtime(context, state)
            self.report({'INFO'}, "已取消快速镜像")
            return {'CANCELLED'}

        if event.type != 'MOUSEMOVE' or context.area is None:
            return {'RUNNING_MODAL'}
        if str(context.area.as_pointer()) != state.area_pointer:
            return {'RUNNING_MODAL'}

        region = next((item for item in context.area.regions if item.type == 'WINDOW'), None)
        if region is None or not (region.x <= event.mouse_x < region.x + region.width
                                  and region.y <= event.mouse_y < region.y + region.height):
            return {'RUNNING_MODAL'}

        current_x = event.mouse_x - region.x
        current_y = event.mouse_y - region.y
        reference = bpy.data.objects.get(state.reference_object)
        if reference is None or reference.type != 'MESH':
            self._clear_runtime(context, state)
            self.report({'WARNING'}, "激活物体不可用，快速镜像已取消")
            return {'CANCELLED'}

        if not state.anchor_ready:
            state.anchor_x = current_x
            state.anchor_y = current_y
            state.mouse_x = current_x
            state.mouse_y = current_y
            state.anchor_ready = True
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        drag = Vector((current_x - state.anchor_x, current_y - state.anchor_y))
        state.mouse_x = current_x
        state.mouse_y = current_y
        if drag.length >= _PREVIEW_THRESHOLD:
            preview_axis = self._infer_axis(context, reference, drag)
            state.preview_axis = preview_axis or ""
            if preview_axis is not None:
                base_axes = json.loads(state.base_axes)
                base_bisect_axes = json.loads(state.base_bisect_axes)
                axes = (base_axes[0] or preview_axis == 'X',
                        base_axes[1] or preview_axis == 'Y',
                        base_axes[2] or preview_axis == 'Z')
                bisect_axes = (base_bisect_axes[0] or preview_axis == 'X',
                               base_bisect_axes[1] or preview_axis == 'Y',
                               base_bisect_axes[2] or preview_axis == 'Z')
                self.axis_x, self.axis_y, self.axis_z = axes
                self.bisect_x, self.bisect_y, self.bisect_z = bisect_axes
            else:
                self.axis_x, self.axis_y, self.axis_z = json.loads(state.base_axes)
                self.bisect_x, self.bisect_y, self.bisect_z = json.loads(
                    state.base_bisect_axes
                )
        else:
            state.preview_axis = ""
            self.axis_x, self.axis_y, self.axis_z = json.loads(state.base_axes)
            self.bisect_x, self.bisect_y, self.bisect_z = json.loads(
                state.base_bisect_axes
            )
        context.area.header_text_set(
            f"快速镜像：{state.preview_axis + ' 轴' if state.preview_axis else '移动鼠标预览轴'}；"
            "拖出虚线范围自动应用；Esc 取消"
        )
        context.area.tag_redraw()

        if drag.length < _APPLY_RADIUS:
            return {'RUNNING_MODAL'}

        axis = self._infer_axis(context, reference, drag)
        if axis is None:
            self._clear_runtime(context, state)
            self.report({'WARNING'}, "无法从当前视角识别镜像轴，请调整视角或取消重试")
            return {'CANCELLED'}
        base_axes = json.loads(state.base_axes)
        base_bisect_axes = json.loads(state.base_bisect_axes)
        axes = (base_axes[0] or axis == 'X',
                base_axes[1] or axis == 'Y',
                base_axes[2] or axis == 'Z')
        bisect_axes = (base_bisect_axes[0] or axis == 'X',
                       base_bisect_axes[1] or axis == 'Y',
                       base_bisect_axes[2] or axis == 'Z')
        self.axis_x, self.axis_y, self.axis_z = axes
        self.bisect_x, self.bisect_y, self.bisect_z = bisect_axes

        self.targets_json = state.targets_json
        self.reference_object = state.reference_object
        self._clear_runtime(context, state)
        return self.execute(context)

    def execute(self, context):
        try:
            target_names = json.loads(self.targets_json) if self.targets_json else [
                obj.name for obj in context.selected_objects if obj.type == 'MESH'
            ]
        except json.JSONDecodeError as exc:
            self.report({'ERROR'}, f"快速镜像目标数据无效：{exc}")
            return {'CANCELLED'}

        targets = [bpy.data.objects.get(name) for name in target_names]
        targets = [obj for obj in targets if obj is not None and obj.type == 'MESH']
        if not targets:
            self.report({'WARNING'}, "没有可添加镜像修改器的网格物体")
            return {'CANCELLED'}

        reference = bpy.data.objects.get(self.reference_object) if self.reference_object else targets[0]
        if reference is None or reference.type != 'MESH':
            self.report({'WARNING'}, "无法找到用于推断镜像轴的激活物体")
            return {'CANCELLED'}

        mirror_object = bpy.data.objects.get(self.mirror_object_name)
        if self.use_3d_cursor:
            cursor_rotation = reference.matrix_world.to_quaternion()
            mirror_object = _find_cursor_pivot(
                context.scene, self.cursor_location, cursor_rotation
            )
            if mirror_object is None:
                mirror_object = bpy.data.objects.new("UVMM Mirror Pivot", None)
                mirror_object.empty_display_type = 'PLAIN_AXES'
                mirror_object.empty_display_size = 0.2
                mirror_object.location = self.cursor_location
                mirror_object.rotation_mode = 'QUATERNION'
                mirror_object.rotation_quaternion = cursor_rotation
                mirror_object.hide_render = True
                context.scene.collection.objects.link(mirror_object)
                mirror_object.hide_set(True, view_layer=context.view_layer)

        axes = (self.axis_x, self.axis_y, self.axis_z)
        bisect_axes = (self.bisect_x, self.bisect_y, self.bisect_z)
        reused_count = 0
        created_count = 0
        for obj in targets:
            modifier = next((item for item in obj.modifiers
                             if item.type == 'MIRROR'
                             and item.mirror_object == mirror_object), None)

            if modifier is not None:
                reused_count += 1
            else:
                modifier = obj.modifiers.new(name="Mirror", type='MIRROR')
                created_count += 1

            modifier.use_axis = tuple(axes)
            modifier.use_bisect_axis = bisect_axes
            modifier.use_clip = self.use_clip
            modifier.use_mirror_merge = self.use_mirror_merge
            modifier.merge_threshold = self.merge_threshold
            modifier.mirror_object = mirror_object

        active_axes = ", ".join(name for name, enabled in zip("XYZ", axes) if enabled)
        active_axes = active_axes or "无"
        self.report(
            {'INFO'},
            f"已设置 {len(targets)} 个物体的 {active_axes} 轴镜像：复用 {reused_count} 个，新增 {created_count} 个修改器",
        )
        return {'FINISHED'}


_CLASSES = (QuickMirrorRuntimeState, OBJECT_OT_QuickMirrorGesture)


def register():
    for cls in _CLASSES:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            bpy.utils.unregister_class(cls)
            bpy.utils.register_class(cls)
    if hasattr(bpy.types.Scene, 'quick_mirror_runtime'):
        del bpy.types.Scene.quick_mirror_runtime
    bpy.types.Scene.quick_mirror_runtime = PointerProperty(type=QuickMirrorRuntimeState)


def unregister():
    if hasattr(bpy.types.Scene, 'quick_mirror_runtime'):
        del bpy.types.Scene.quick_mirror_runtime
    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
