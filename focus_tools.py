"""Viewport focus and reversible hide-unselected tools."""

import bpy
import blf
from pathlib import Path
from bpy.app.handlers import persistent
from bpy.props import CollectionProperty, PointerProperty, StringProperty
from bpy.types import Menu, Operator, PropertyGroup


# Blender 返回的绘制句柄是临时 PyCapsule，只用于在插件注销时移除回调。
_LAYER_COUNTER_DRAW_HANDLE = None
_LAYER_COUNTER_FONT_PATH = None
_LAYER_COUNTER_FONT_ID = 0


class FocusHiddenObject(PropertyGroup):
    object: PointerProperty(type=bpy.types.Object)


class FocusVisibilityLevel(PropertyGroup):
    view_layer_name: StringProperty()
    hidden_objects: CollectionProperty(type=FocusHiddenObject)


class FocusVisibilityStack(PropertyGroup):
    levels: CollectionProperty(type=FocusVisibilityLevel)


class VIEW3D_OT_UVMMFocusSelected(Operator):
    bl_idname = "view3d.uvmm_focus_selected"
    bl_label = "聚焦所选"
    bl_description = "将当前选择最大化显示在视图中"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT'
                and context.area is not None
                and context.area.type == 'VIEW_3D'
                and bool(context.selected_objects))

    def execute(self, context):
        window_region = next(
            (region for region in context.area.regions if region.type == 'WINDOW'), None
        )
        if window_region is None:
            self.report({'WARNING'}, "找不到 3D 视图区域，无法聚焦选择")
            return {'CANCELLED'}

        with context.temp_override(
                window=context.window,
                area=context.area,
                region=window_region):
            result = bpy.ops.view3d.view_selected('EXEC_DEFAULT', use_all_regions=False)
        if 'FINISHED' not in result:
            self.report({'WARNING'}, "聚焦选择未执行")
            return {'CANCELLED'}
        return {'FINISHED'}


class VIEW3D_OT_UVMMHideUnselected(Operator):
    bl_idname = "view3d.uvmm_hide_unselected"
    bl_label = "隐藏未选"
    bl_description = "隐藏当前视图层中未选中的可见物体，并记录为一个可回退层级"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if (context.mode != 'OBJECT'
                or context.area is None
                or context.area.type != 'VIEW_3D'):
            return False
        if context.selected_objects:
            return True
        stack = getattr(context.scene, "uvmm_focus_visibility_stack", None)
        return stack is not None and bool(stack.levels)

    def execute(self, context):
        if not context.selected_objects:
            restored_count = _restore_last_visibility_level(context.scene)
            if restored_count is None:
                self.report({'INFO'}, "没有选中物体，也没有可回退层级")
                return {'CANCELLED'}
            context.area.tag_redraw()
            self.report(
                {'INFO'},
                f"未选择物体，已回退一级并恢复 {restored_count} 个物体"
            )
            return {'FINISHED'}

        view_layer = context.view_layer
        selected_pointers = {obj.as_pointer() for obj in context.selected_objects}
        targets = [
            obj for obj in view_layer.objects
            if obj.as_pointer() not in selected_pointers
            and obj.visible_get(view_layer=view_layer)
        ]
        stack = context.scene.uvmm_focus_visibility_stack
        if not targets:
            restored_count = _restore_last_visibility_level(context.scene)
            if restored_count is not None:
                context.area.tag_redraw()
                self.report(
                    {'INFO'},
                    f"没有未选物体可隐藏，已回退一级并恢复 {restored_count} 个物体"
                )
                return {'FINISHED'}
            self.report({'INFO'}, "没有可隐藏的未选中物体")
            return {'CANCELLED'}

        level = stack.levels.add()
        level.view_layer_name = view_layer.name
        for obj in targets:
            level.hidden_objects.add().object = obj

        try:
            for obj in targets:
                obj.hide_set(True, view_layer=view_layer)
        except (ReferenceError, RuntimeError, TypeError) as exc:
            for obj in targets:
                if obj.name in view_layer.objects:
                    obj.hide_set(False, view_layer=view_layer)
            stack.levels.remove(len(stack.levels) - 1)
            self.report({'ERROR'}, f"隐藏未选物体失败，已回滚：{exc}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"已隐藏 {len(targets)} 个未选中物体")
        return {'FINISHED'}


def _restore_last_visibility_level(scene):
    stack = getattr(scene, "uvmm_focus_visibility_stack", None)
    if stack is None or not stack.levels:
        return None

    level_index = len(stack.levels) - 1
    level = stack.levels[level_index]
    view_layer = scene.view_layers.get(level.view_layer_name)
    if view_layer is None:
        stack.levels.remove(level_index)
        return 0

    restored_count = 0
    for entry in level.hidden_objects:
        obj = entry.object
        if obj is not None and obj.name in view_layer.objects:
            obj.hide_set(False, view_layer=view_layer)
            restored_count += 1

    stack.levels.remove(level_index)
    return restored_count


class VIEW3D_OT_UVMMRestoreHiddenLevel(Operator):
    bl_idname = "view3d.uvmm_restore_hidden_level"
    bl_label = "回退显示"
    bl_description = "恢复上一次隐藏的物体，只回退一级"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT'
                and context.area is not None
                and context.area.type == 'VIEW_3D'
                and hasattr(context.scene, 'uvmm_focus_visibility_stack')
                and len(context.scene.uvmm_focus_visibility_stack.levels) > 0)

    def execute(self, context):
        if not context.scene.uvmm_focus_visibility_stack.levels:
            self.report({'INFO'}, "没有可回退的隐藏层级")
            return {'CANCELLED'}

        restored_count = _restore_last_visibility_level(context.scene)
        context.area.tag_redraw()
        self.report({'INFO'}, f"已回退一级，恢复 {restored_count} 个物体")
        return {'FINISHED'}


class VIEW3D_MT_UVMMFocusActions(Menu):
    bl_idname = "VIEW3D_MT_uvmm_focus_actions"
    bl_label = "聚焦"

    def draw(self, context):
        layout = self.layout
        layout.operator(VIEW3D_OT_UVMMFocusSelected.bl_idname,
                        text="聚焦所选", icon='VIEWZOOM')
        layout.separator()
        layout.operator(VIEW3D_OT_UVMMHideUnselected.bl_idname,
                        text="隐藏未选", icon='HIDE_ON')
        layout.operator(VIEW3D_OT_UVMMRestoreHiddenLevel.bl_idname,
                        text="回退显示", icon='LOOP_BACK')


def _draw_focus_layer_counter():
    context = bpy.context
    area = context.area
    region = context.region
    if (area is None or area.type != 'VIEW_3D'
            or region is None or region.type != 'WINDOW'):
        return

    scene = context.scene
    stack = getattr(scene, "uvmm_focus_visibility_stack", None)
    depth = len(stack.levels) if stack is not None else 0
    if depth < 1:
        return

    if _LAYER_COUNTER_FONT_ID > 0:
        label = f"Hidden Level:  {depth}"
        font_id = _LAYER_COUNTER_FONT_ID
    else:
        label = f"Hidden Level:  {depth}"
        font_id = 0
    ui_scale = context.preferences.system.ui_scale
    font_size = max(15, round(21 * ui_scale))
    blf.size(font_id, font_size)
    text_width, text_height = blf.dimensions(font_id, label)
    x = (region.width - text_width) * 0.5
    y = region.height - text_height - round(140 * ui_scale)
    blf.color(font_id, 0.92, 0.96, 1.0, 1.0)
    blf.position(font_id, x, y, 0)
    blf.draw(font_id, label)


def _register_layer_counter_font():
    global _LAYER_COUNTER_FONT_PATH, _LAYER_COUNTER_FONT_ID
    font_path = (
        Path(bpy.utils.resource_path('LOCAL'))
        / "datafiles" / "fonts" / "Inter.woff2"
    )
    if font_path.is_file():
        font_id = blf.load(str(font_path))
        if font_id >= 0:
            _LAYER_COUNTER_FONT_PATH = font_path
            _LAYER_COUNTER_FONT_ID = font_id


def _unregister_layer_counter_font():
    global _LAYER_COUNTER_FONT_PATH, _LAYER_COUNTER_FONT_ID
    if _LAYER_COUNTER_FONT_PATH is not None:
        blf.unload(str(_LAYER_COUNTER_FONT_PATH))
    _LAYER_COUNTER_FONT_PATH = None
    _LAYER_COUNTER_FONT_ID = 0


def _register_layer_counter_draw_handler():
    global _LAYER_COUNTER_DRAW_HANDLE
    if _LAYER_COUNTER_DRAW_HANDLE is not None:
        bpy.types.SpaceView3D.draw_handler_remove(
            _LAYER_COUNTER_DRAW_HANDLE, 'WINDOW'
        )
    _LAYER_COUNTER_DRAW_HANDLE = bpy.types.SpaceView3D.draw_handler_add(
        _draw_focus_layer_counter, (), 'WINDOW', 'POST_PIXEL'
    )


def _unregister_layer_counter_draw_handler():
    global _LAYER_COUNTER_DRAW_HANDLE
    if _LAYER_COUNTER_DRAW_HANDLE is None:
        return
    bpy.types.SpaceView3D.draw_handler_remove(
        _LAYER_COUNTER_DRAW_HANDLE, 'WINDOW'
    )
    _LAYER_COUNTER_DRAW_HANDLE = None


def _tag_viewports_redraw():
    window_manager = getattr(bpy.context, 'window_manager', None)
    if window_manager is None:
        return
    for window in window_manager.windows:
        if window.screen is None:
            continue
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


@persistent
def _clear_focus_stack_when_all_revealed(scene, depsgraph):
    stack = getattr(scene, "uvmm_focus_visibility_stack", None)
    if stack is None or not stack.levels:
        return

    for level in stack.levels:
        view_layer = scene.view_layers.get(level.view_layer_name)
        if view_layer is None:
            continue
        for entry in level.hidden_objects:
            obj = entry.object
            if (obj is not None and obj.name in view_layer.objects
                    and obj.hide_get(view_layer=view_layer)):
                return

    while stack.levels:
        stack.levels.remove(len(stack.levels) - 1)
    _tag_viewports_redraw()


def _register_focus_stack_handler():
    handlers = bpy.app.handlers.depsgraph_update_post
    if _clear_focus_stack_when_all_revealed not in handlers:
        handlers.append(_clear_focus_stack_when_all_revealed)


def _unregister_focus_stack_handler():
    handlers = bpy.app.handlers.depsgraph_update_post
    if _clear_focus_stack_when_all_revealed in handlers:
        handlers.remove(_clear_focus_stack_when_all_revealed)


_CLASSES = (
    FocusHiddenObject,
    FocusVisibilityLevel,
    FocusVisibilityStack,
    VIEW3D_OT_UVMMFocusSelected,
    VIEW3D_OT_UVMMHideUnselected,
    VIEW3D_OT_UVMMRestoreHiddenLevel,
    VIEW3D_MT_UVMMFocusActions,
)

_OPERATOR_IDS = {
    VIEW3D_OT_UVMMFocusSelected.bl_idname,
    VIEW3D_OT_UVMMHideUnselected.bl_idname,
    VIEW3D_OT_UVMMRestoreHiddenLevel.bl_idname,
}


def unregister_keymaps():
    window_manager = getattr(bpy.context, 'window_manager', None)
    keyconfig = window_manager.keyconfigs.addon if window_manager else None
    if keyconfig is None:
        return

    keymap = keyconfig.keymaps.get('Object Mode')
    if keymap is None:
        return
    for item in list(keymap.keymap_items):
        if item.idname in _OPERATOR_IDS:
            keymap.keymap_items.remove(item)


def register_keymaps():
    window_manager = getattr(bpy.context, 'window_manager', None)
    keyconfig = window_manager.keyconfigs.addon if window_manager else None
    if keyconfig is None:
        return

    unregister_keymaps()
    keymap = keyconfig.keymaps.new(
        name='Object Mode', space_type='EMPTY', region_type='WINDOW'
    )
    keymap.keymap_items.new(VIEW3D_OT_UVMMFocusSelected.bl_idname,
                            type='F', value='PRESS')
    keymap.keymap_items.new(VIEW3D_OT_UVMMHideUnselected.bl_idname,
                            type='F', value='PRESS', ctrl=True)
    keymap.keymap_items.new(VIEW3D_OT_UVMMRestoreHiddenLevel.bl_idname,
                            type='F', value='PRESS', ctrl=True, alt=True)


def register():
    if hasattr(bpy.types.Scene, 'uvmm_focus_visibility_stack'):
        del bpy.types.Scene.uvmm_focus_visibility_stack
    for cls in _CLASSES:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            bpy.utils.unregister_class(cls)
            bpy.utils.register_class(cls)
    bpy.types.Scene.uvmm_focus_visibility_stack = PointerProperty(
        type=FocusVisibilityStack
    )
    register_keymaps()
    _register_layer_counter_font()
    _register_layer_counter_draw_handler()
    _register_focus_stack_handler()


def unregister():
    _unregister_layer_counter_draw_handler()
    _unregister_focus_stack_handler()
    _unregister_layer_counter_font()
    unregister_keymaps()
    if hasattr(bpy.types.Scene, 'uvmm_focus_visibility_stack'):
        del bpy.types.Scene.uvmm_focus_visibility_stack
    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
