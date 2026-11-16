"""Viewport focus and reversible hide-unselected tools."""

import bpy
import bmesh
import blf
from pathlib import Path
from contextlib import contextmanager
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, CollectionProperty, IntProperty, PointerProperty, StringProperty
from bpy.types import Menu, Operator, PropertyGroup


# Blender 返回的绘制句柄是临时 PyCapsule，只用于在插件注销时移除回调。
_LAYER_COUNTER_DRAW_HANDLE = None
_LAYER_COUNTER_FONT_PATH = None
_LAYER_COUNTER_FONT_ID = 0


class FocusHiddenObject(PropertyGroup):
    object: PointerProperty(type=bpy.types.Object)


class FocusModifierVisibility(PropertyGroup):
    object: PointerProperty(type=bpy.types.Object)
    modifier_uid: IntProperty()
    show_viewport: BoolProperty()


class FocusVisibilityLevel(PropertyGroup):
    view_layer_name: StringProperty()
    hide_kind: StringProperty(default='UNSELECTED')
    hidden_objects: CollectionProperty(type=FocusHiddenObject)
    modifiers: CollectionProperty(type=FocusModifierVisibility)


class FocusVisibilityStack(PropertyGroup):
    levels: CollectionProperty(type=FocusVisibilityLevel)
    is_updating: BoolProperty(default=False)


class FocusHiddenFace(PropertyGroup):
    object: PointerProperty(type=bpy.types.Object)
    face_index: IntProperty()
    vertices: StringProperty()


class FocusFaceVisibilityLevel(PropertyGroup):
    hide_kind: StringProperty(default='UNSELECTED')
    hidden_faces: CollectionProperty(type=FocusHiddenFace)
    modifiers: CollectionProperty(type=FocusModifierVisibility)


class FocusFaceVisibilityStack(PropertyGroup):
    levels: CollectionProperty(type=FocusFaceVisibilityLevel)
    is_updating: BoolProperty(default=False)


def _edit_meshes(context):
    return [obj for obj in context.objects_in_mode_unique_data
            if obj.type == 'MESH']


@contextmanager
def _focus_modifier_changes(scene):
    # Temporary focus visibility must not propagate through modifier sync.
    from .modifier_manager import ModifierAutoSync
    state = ModifierAutoSync.get_state(scene)
    previous = state.is_syncing if state else False
    if state:
        state.is_syncing = True
    try:
        yield
    finally:
        if state:
            if bpy.context.scene == scene:
                ModifierAutoSync.ensure_snapshots(scene, bpy.context.selected_objects)
            state.is_syncing = previous


def _focus_modifiers_to_hide(objects, show_mirror, show_array):
    types = set()
    if not show_mirror:
        types.add('MIRROR')
    if not show_array:
        types.add('ARRAY')
    return [(obj, mod) for obj in objects if obj.is_editable
            for mod in obj.modifiers if _focus_modifier_type(mod) in types and mod.show_viewport
            and not mod.is_property_readonly('show_viewport')]


def _focus_modifier_type(mod):
    if mod.type == 'NODES' and mod.node_group is not None:
        ref = mod.node_group.library_weak_reference
        if (ref is not None and ref.id_name == 'NTArray'
                and Path(ref.filepath).name == 'geometry_nodes_essentials.blend'):
            return 'ARRAY'
    return mod.type


def _hide_focus_modifiers(scene, level, modifiers):
    with _focus_modifier_changes(scene):
        for obj, mod in modifiers:
            entry = level.modifiers.add()
            entry.object = obj
            entry.modifier_uid = mod.persistent_uid
            entry.show_viewport = mod.show_viewport
            mod.show_viewport = False


def _restore_focus_modifiers(scene, level):
    with _focus_modifier_changes(scene):
        for entry in level.modifiers:
            obj = entry.object
            if obj is None or not obj.is_editable:
                continue
            mod = next((mod for mod in obj.modifiers if mod.persistent_uid == entry.modifier_uid), None)
            if mod is not None:
                mod.show_viewport = entry.show_viewport


def _face_key(face):
    return ','.join(str(vert.index) for vert in face.verts)


def _edit_face_targets(context, selected):
    targets = []
    for obj in _edit_meshes(context):
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        bm.verts.index_update()
        bm.faces.index_update()
        faces = [(face.index, _face_key(face)) for face in bm.faces
                 if not face.hide and face.select == selected]
        if faces:
            targets.append((obj, faces))
    return targets


def _hide_faces_as_level(scene, targets, hide_kind):
    stack = scene.uvmm_focus_face_stack
    level = stack.levels.add()
    level.hide_kind = hide_kind
    for obj, faces in targets:
        for face_index, vertices in faces:
            entry = level.hidden_faces.add()
            entry.object = obj
            entry.face_index = face_index
            entry.vertices = vertices
    for obj, faces in targets:
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        bm.verts.index_update()
        bm.faces.index_update()
        for face_index, vertices in faces:
            if face_index >= len(bm.faces) or _face_key(bm.faces[face_index]) != vertices:
                stack.levels.remove(len(stack.levels) - 1)
                raise RuntimeError("编辑网格在隐藏前发生变化")
    for obj, faces in targets:
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        for face_index, _ in faces:
            bm.faces[face_index].hide_set(True)
        bm.normal_update()
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    return len(level.hidden_faces)


def _restore_last_face_level(scene, context):
    stack = scene.uvmm_focus_face_stack
    if not stack.levels:
        return None
    level = stack.levels[-1]
    edit_objects = {obj.as_pointer(): obj for obj in _edit_meshes(context)}
    faces_by_object = {}
    for entry in level.hidden_faces:
        obj = entry.object
        if obj is None or obj.as_pointer() not in edit_objects:
            _restore_focus_modifiers(scene, level)
            return -1
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        bm.verts.index_update()
        bm.faces.index_update()
        if entry.face_index >= len(bm.faces):
            _restore_focus_modifiers(scene, level)
            return -1
        face = bm.faces[entry.face_index]
        if _face_key(face) != entry.vertices:
            _restore_focus_modifiers(scene, level)
            return -1
        faces_by_object.setdefault(obj.as_pointer(), (obj, []))[1].append(
            entry.face_index)
    for obj, faces in faces_by_object.values():
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        for face_index in faces:
            bm.faces[face_index].hide_set(False)
        bm.normal_update()
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    restored = len(level.hidden_faces)
    _restore_focus_modifiers(scene, level)
    stack.levels.remove(len(stack.levels) - 1)
    return restored


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
    bl_description = "隐藏未选物体或面；上一操作隐藏所选时逐层恢复"
    bl_options = {'REGISTER', 'UNDO'}

    ignore_lights: BoolProperty(name="忽略灯光", description="独显时保留原本可见的灯光", default=True)
    show_mirror: BoolProperty(name="显示镜像", description="保留镜像修改器的原有视口显示；取消时临时隐藏镜像结果", default=True)
    show_array: BoolProperty(name="显示阵列", description="保留阵列修改器的原有视口显示；取消时临时隐藏阵列结果", default=True)

    def draw(self, context):
        self.layout.prop(self, 'ignore_lights')
        self.layout.prop(self, 'show_mirror')
        self.layout.prop(self, 'show_array')

    @classmethod
    def poll(cls, context):
        if (context.mode not in {'OBJECT', 'EDIT_MESH'}
                or context.area is None
                or context.area.type != 'VIEW_3D'):
            return False
        if context.mode == 'EDIT_MESH':
            return bool(_edit_meshes(context))
        if context.selected_objects:
            return True
        stack = getattr(context.scene, "uvmm_focus_visibility_stack", None)
        return stack is not None and bool(stack.levels)

    def execute(self, context):
        if context.mode == 'EDIT_MESH':
            return self._execute_edit_mesh(context)
        stack = context.scene.uvmm_focus_visibility_stack
        if stack.levels and stack.levels[-1].hide_kind == 'SELECTED':
            restored_count = _restore_last_visibility_level(context.scene)
            context.area.tag_redraw()
            self.report({'INFO'}, f"已恢复上一次隐藏的 {restored_count} 个所选物体")
            return {'FINISHED'}

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
            and not (self.ignore_lights and obj.type == 'LIGHT')
        ]
        modifiers = _focus_modifiers_to_hide(context.selected_objects, self.show_mirror, self.show_array)
        if not targets and not modifiers:
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

        previous_levels = len(stack.levels)
        try:
            _hide_objects_as_level(context.scene, view_layer, targets, 'UNSELECTED')
            _hide_focus_modifiers(context.scene, stack.levels[-1], modifiers)
        except (ReferenceError, RuntimeError, TypeError) as exc:
            if len(stack.levels) > previous_levels:
                _restore_last_visibility_level(context.scene)
            self.report({'ERROR'}, f"隐藏未选物体失败，已回滚：{exc}")
            return {'CANCELLED'}

        context.area.tag_redraw()
        self.report({'INFO'}, f"已隐藏 {len(targets)} 个未选中物体")
        return {'FINISHED'}

    def _execute_edit_mesh(self, context):
        stack = context.scene.uvmm_focus_face_stack
        selected = _edit_face_targets(context, True)
        if (stack.levels and stack.levels[-1].hide_kind == 'SELECTED') or not selected:
            restored = _restore_last_face_level(context.scene, context)
            if restored == -1:
                stack.levels.remove(len(stack.levels) - 1)
                self.report({'WARNING'}, "网格或编辑对象已变化，已清除失效的面隐藏记录")
            elif restored is None:
                return {'CANCELLED'}
            else:
                self.report({'INFO'}, f"已恢复上一次隐藏的 {restored} 个面")
            context.area.tag_redraw()
            return {'FINISHED'}
        targets = _edit_face_targets(context, False)
        modifiers = _focus_modifiers_to_hide(context.objects_in_mode, self.show_mirror, self.show_array)
        if not targets and not modifiers:
            return {'CANCELLED'}
        count = _hide_faces_as_level(context.scene, targets, 'UNSELECTED')
        _hide_focus_modifiers(context.scene, stack.levels[-1], modifiers)
        context.area.tag_redraw()
        self.report({'INFO'}, f"已隐藏 {count} 个未选中面")
        return {'FINISHED'}


def _hide_objects_as_level(scene, view_layer, targets, hide_kind):
    stack = scene.uvmm_focus_visibility_stack
    level = stack.levels.add()
    level.view_layer_name = view_layer.name
    level.hide_kind = hide_kind
    for obj in targets:
        level.hidden_objects.add().object = obj

    hidden = []
    try:
        for obj in targets:
            obj.hide_set(True, view_layer=view_layer)
            hidden.append(obj)
    except (ReferenceError, RuntimeError, TypeError):
        for obj in hidden:
            if obj.name in view_layer.objects:
                obj.hide_set(False, view_layer=view_layer)
        stack.levels.remove(len(stack.levels) - 1)
        raise


def _restore_last_visibility_level(scene):
    stack = getattr(scene, "uvmm_focus_visibility_stack", None)
    if stack is None or not stack.levels:
        return None

    level_index = len(stack.levels) - 1
    level = stack.levels[level_index]
    view_layer = scene.view_layers.get(level.view_layer_name)
    if view_layer is None:
        _restore_focus_modifiers(scene, level)
        stack.levels.remove(level_index)
        return 0

    restored_count = 0
    for entry in level.hidden_objects:
        obj = entry.object
        if obj is not None and obj.name in view_layer.objects:
            obj.hide_set(False, view_layer=view_layer)
            restored_count += 1

    _restore_focus_modifiers(scene, level)
    stack.levels.remove(level_index)
    return restored_count


class VIEW3D_OT_UVMMRestoreHiddenLevel(Operator):
    bl_idname = "view3d.uvmm_restore_hidden_level"
    bl_label = "隐藏所选／回退"
    bl_description = "隐藏所选物体或面；没有选择时回退上一隐藏层级"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if (context.mode not in {'OBJECT', 'EDIT_MESH'}
                or context.area is None
                or context.area.type != 'VIEW_3D'):
            return False
        if context.mode == 'EDIT_MESH':
            return bool(_edit_meshes(context))
        return (context.mode == 'OBJECT'
                and context.area.type == 'VIEW_3D'
                and hasattr(context.scene, 'uvmm_focus_visibility_stack')
                and (bool(context.selected_objects)
                     or bool(context.scene.uvmm_focus_visibility_stack.levels)))

    def execute(self, context):
        if context.mode == 'EDIT_MESH':
            targets = _edit_face_targets(context, True)
            if targets:
                count = _hide_faces_as_level(context.scene, targets, 'SELECTED')
                self.report({'INFO'}, f"已隐藏 {count} 个所选面")
            else:
                restored = _restore_last_face_level(context.scene, context)
                if restored == -1:
                    context.scene.uvmm_focus_face_stack.levels.remove(
                        len(context.scene.uvmm_focus_face_stack.levels) - 1)
                    self.report({'WARNING'}, "网格或编辑对象已变化，已清除失效的面隐藏记录")
                elif restored is None:
                    return {'CANCELLED'}
                else:
                    self.report({'INFO'}, f"已回退一级，恢复 {restored} 个面")
            context.area.tag_redraw()
            return {'FINISHED'}
        view_layer = context.view_layer
        targets = [
            obj for obj in context.selected_objects
            if obj.visible_get(view_layer=view_layer)
        ]
        if targets:
            try:
                _hide_objects_as_level(context.scene, view_layer, targets, 'SELECTED')
            except (ReferenceError, RuntimeError, TypeError) as exc:
                self.report({'ERROR'}, f"隐藏所选物体失败，已回滚：{exc}")
                return {'CANCELLED'}
            context.area.tag_redraw()
            self.report({'INFO'}, f"已隐藏 {len(targets)} 个所选物体")
            return {'FINISHED'}

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
                        text="隐藏所选／回退", icon='LOOP_BACK')


def _draw_focus_layer_counter():
    context = bpy.context
    area = context.area
    region = context.region
    if (area is None or area.type != 'VIEW_3D'
            or region is None or region.type != 'WINDOW'):
        return

    scene = context.scene
    stack = getattr(scene, "uvmm_focus_visibility_stack", None)
    face_stack = getattr(scene, "uvmm_focus_face_stack", None)
    if context.mode == 'EDIT_MESH':
        depth = len(face_stack.levels) if face_stack is not None else 0
    else:
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
    if stack is None or not stack.levels or stack.is_updating:
        return

    for level in stack.levels:
        if not level.hidden_objects and level.modifiers:
            return
        view_layer = scene.view_layers.get(level.view_layer_name)
        if view_layer is None:
            continue
        for entry in level.hidden_objects:
            obj = entry.object
            if (obj is not None and obj.name in view_layer.objects
                    and obj.hide_get(view_layer=view_layer)):
                return

    stack.is_updating = True
    try:
        while stack.levels:
            _restore_focus_modifiers(scene, stack.levels[-1])
            stack.levels.remove(len(stack.levels) - 1)
    finally:
        stack.is_updating = False
    _tag_viewports_redraw()


def _face_entry_is_hidden(entry):
    obj = entry.object
    if obj is None or obj.type != 'MESH':
        return False

    bm = None
    owns_bmesh = obj.mode != 'EDIT'
    try:
        if owns_bmesh:
            bm = bmesh.new()
            bm.from_mesh(obj.data)
        else:
            bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        bm.verts.ensure_lookup_table()
        bm.faces.index_update()
        if entry.face_index < 0 or entry.face_index >= len(bm.faces):
            return False
        face = bm.faces[entry.face_index]
        if _face_key(face) != entry.vertices:
            return False
        return face.hide
    except (ReferenceError, RuntimeError, TypeError, ValueError):
        return False
    finally:
        if owns_bmesh and bm is not None:
            bm.free()


def _clear_face_stack_when_all_revealed(scene):
    face_stack = getattr(scene, 'uvmm_focus_face_stack', None)
    if face_stack is None or not face_stack.levels or face_stack.is_updating:
        return False

    for level in face_stack.levels:
        if not level.hidden_faces and level.modifiers:
            return False
        if any(_face_entry_is_hidden(entry) for entry in level.hidden_faces):
            return False

    face_stack.is_updating = True
    try:
        while face_stack.levels:
            _restore_focus_modifiers(scene, face_stack.levels[-1])
            face_stack.levels.remove(len(face_stack.levels) - 1)
    finally:
        face_stack.is_updating = False
    return True


@persistent
def _clear_focus_face_stack_when_all_revealed(scene, depsgraph):
    if _clear_face_stack_when_all_revealed(scene):
        _tag_viewports_redraw()


def _register_focus_stack_handler():
    handlers = bpy.app.handlers.depsgraph_update_post
    if _clear_focus_stack_when_all_revealed not in handlers:
        handlers.append(_clear_focus_stack_when_all_revealed)
    if _clear_focus_face_stack_when_all_revealed not in handlers:
        handlers.append(_clear_focus_face_stack_when_all_revealed)


def _unregister_focus_stack_handler():
    handlers = bpy.app.handlers.depsgraph_update_post
    if _clear_focus_stack_when_all_revealed in handlers:
        handlers.remove(_clear_focus_stack_when_all_revealed)
    if _clear_focus_face_stack_when_all_revealed in handlers:
        handlers.remove(_clear_focus_face_stack_when_all_revealed)


_CLASSES = (
    FocusHiddenObject,
    FocusModifierVisibility,
    FocusVisibilityLevel,
    FocusVisibilityStack,
    FocusHiddenFace,
    FocusFaceVisibilityLevel,
    FocusFaceVisibilityStack,
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

    for name in ('Object Mode', 'Mesh'):
        keymap = keyconfig.keymaps.get(name)
        if keymap is None:
            continue
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
    keymap = keyconfig.keymaps.new(
        name='Mesh', space_type='EMPTY', region_type='WINDOW'
    )
    keymap.keymap_items.new(VIEW3D_OT_UVMMHideUnselected.bl_idname,
                            type='F', value='PRESS', ctrl=True)
    keymap.keymap_items.new(VIEW3D_OT_UVMMRestoreHiddenLevel.bl_idname,
                            type='F', value='PRESS', ctrl=True, alt=True)


def register():
    if hasattr(bpy.types.Scene, 'uvmm_focus_visibility_stack'):
        del bpy.types.Scene.uvmm_focus_visibility_stack
    if hasattr(bpy.types.Scene, 'uvmm_focus_face_stack'):
        del bpy.types.Scene.uvmm_focus_face_stack
    for cls in _CLASSES:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            bpy.utils.unregister_class(cls)
            bpy.utils.register_class(cls)
    bpy.types.Scene.uvmm_focus_visibility_stack = PointerProperty(
        type=FocusVisibilityStack
    )
    bpy.types.Scene.uvmm_focus_face_stack = PointerProperty(
        type=FocusFaceVisibilityStack
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
    for scene in bpy.data.scenes:
        for name in ('uvmm_focus_visibility_stack', 'uvmm_focus_face_stack'):
            stack = getattr(scene, name, None)
            if stack is not None:
                for level in reversed(list(stack.levels)):
                    _restore_focus_modifiers(scene, level)
    if hasattr(bpy.types.Scene, 'uvmm_focus_visibility_stack'):
        del bpy.types.Scene.uvmm_focus_visibility_stack
    if hasattr(bpy.types.Scene, 'uvmm_focus_face_stack'):
        del bpy.types.Scene.uvmm_focus_face_stack
    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
