"""Individual model checks with settings in Blender's last-operation panel."""

import bpy
import bmesh
import types
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    StringProperty,
)

from . import diagnostic

ADDON_ID = __package__.split('.')[0]

_UV_FEATURE_SETTINGS = {
    'uv_show_stretch': (
        "uv_stretch_base_color", "uv_stretch_max_color", "uv_stretch_strength",
        "uv_stretch_threshold", "uv_stretch_opacity",
    ),
    'uv_show_boundaries': ("uv_boundary_color", "uv_boundary_width"),
    'uv_show_checker': (),
    'uv_show_orientation': (
        "uv_orientation_front_color", "uv_orientation_back_color",
        "uv_orientation_opacity",
    ),
}


def _copy_preferences_to_operator(operator, prefs, names):
    for name in names:
        setattr(operator, name, getattr(prefs, name))


def _copy_operator_to_preferences(operator, prefs, names):
    for name in names:
        value = getattr(operator, name)
        current = getattr(prefs, name)
        if hasattr(value, "__len__") and not isinstance(value, str):
            differs = tuple(value) != tuple(current)
        else:
            differs = value != current
        if differs:
            setattr(prefs, name, value)


def _selected_meshes(context):
    return tuple(obj for obj in context.selected_objects if obj.type == 'MESH')


def _clear_selection(bm):
    for face in bm.faces:
        face.select_set(False)
    for edge in bm.edges:
        edge.select_set(False)
    for vertex in bm.verts:
        vertex.select_set(False)


def _count_findings(
    objects,
    *,
    check_ngons=False,
    ngon_sides=4,
    check_isolated=False,
    isolated_include_hidden=False,
):
    """Count findings without altering mesh, selection, or mode."""
    unique_meshes = {}
    for obj in objects:
        if obj.type == 'MESH':
            unique_meshes.setdefault(obj.data, obj)

    found_faces = 0
    found_vertices = 0
    for mesh, obj in unique_meshes.items():
        edit_mode = obj.mode == 'EDIT'
        bm = bmesh.from_edit_mesh(mesh) if edit_mode else bmesh.new()
        try:
            if not edit_mode:
                bm.from_mesh(mesh)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
            if check_ngons:
                found_faces += sum(len(face.verts) > ngon_sides for face in bm.faces)
            if check_isolated:
                found_vertices += sum(
                    not vertex.link_edges
                    and (isolated_include_hidden or not vertex.hide)
                    for vertex in bm.verts
                )
        finally:
            if not edit_mode:
                bm.free()
    return found_faces, found_vertices


def _select_findings(
    context,
    *,
    check_ngons=False,
    ngon_sides=4,
    check_isolated=False,
    clear_selection=True,
    isolated_include_hidden=False,
):
    selected_meshes = _selected_meshes(context)
    if not selected_meshes:
        return 0, 0
    if context.mode == 'EDIT_MESH':
        candidate_objects = tuple(context.objects_in_mode_unique_data)
    else:
        candidate_objects = selected_meshes
    findings = _count_findings(
        candidate_objects,
        check_ngons=check_ngons,
        ngon_sides=ngon_sides,
        check_isolated=check_isolated,
        isolated_include_hidden=isolated_include_hidden,
    )
    if not any(findings):
        return findings
    if context.object is None or context.object.type != 'MESH':
        context.view_layer.objects.active = selected_meshes[0]
    if context.object.mode != 'EDIT':
        bpy.ops.object.mode_set(mode='EDIT')
    context.tool_settings.mesh_select_mode = (True, True, True)
    mesh_objects = tuple(context.objects_in_mode_unique_data)
    if not mesh_objects:
        mesh_objects = selected_meshes
    found_faces = 0
    found_vertices = 0
    for obj in mesh_objects:
        if obj.type != 'MESH':
            continue
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        if clear_selection:
            _clear_selection(bm)
        if check_ngons:
            for face in bm.faces:
                if len(face.verts) > ngon_sides:
                    face.select_set(True)
                    found_faces += 1
        if check_isolated:
            for vertex in bm.verts:
                if vertex.link_edges or (vertex.hide and not isolated_include_hidden):
                    continue
                vertex.select_set(True)
                found_vertices += 1
        bm.normal_update()
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    return found_faces, found_vertices


def _has_selected_mesh(context):
    return context.area is not None and context.area.type == 'VIEW_3D' and bool(_selected_meshes(context))


def _visible_meshes(context):
    return diagnostic.visible_meshes(context.scene, context.view_layer)


def _ensure_object_mode(context):
    """Exit Edit Mode if active, remaining strictly in Object Mode."""
    if context.mode == 'EDIT_MESH' or (context.active_object and context.active_object.mode == 'EDIT'):
        if context.view_layer.objects.active is None:
            for obj in context.view_layer.objects:
                if obj.mode == 'EDIT':
                    context.view_layer.objects.active = obj
                    break
        if context.view_layer.objects.active and context.view_layer.objects.active.mode == 'EDIT':
            bpy.ops.object.mode_set(mode='OBJECT')


def _select_uv_findings(context, prefs, feature=None):
    """Select visible mesh objects with qualifying UV findings without entering Edit Mode."""
    _ensure_object_mode(context)
    if feature == 'uv_show_stretch':
        check_prefs = types.SimpleNamespace(
            uv_show_stretch=True,
            uv_show_orientation=False,
            uv_stretch_threshold=getattr(prefs, 'uv_stretch_threshold', 0.0),
        )
    elif feature == 'uv_show_orientation':
        check_prefs = types.SimpleNamespace(
            uv_show_stretch=False,
            uv_show_orientation=True,
            uv_stretch_threshold=getattr(prefs, 'uv_stretch_threshold', 0.0),
        )
    else:
        check_prefs = prefs

    check_stretch = bool(getattr(check_prefs, 'uv_show_stretch', False))
    check_flipped = bool(getattr(check_prefs, 'uv_show_orientation', False))
    if not (check_stretch or check_flipped):
        return 0
    mesh_objects = _visible_meshes(context)
    if not mesh_objects:
        return 0

    matching_objects = tuple(
        obj for obj in mesh_objects
        if diagnostic.count_uv_findings((obj,), check_prefs) > 0
    )
    for obj in tuple(context.view_layer.objects):
        if obj.select_get():
            obj.select_set(False)
    for obj in matching_objects:
        obj.select_set(True)
    active = context.view_layer.objects.active
    if active not in matching_objects:
        active = matching_objects[0] if matching_objects else None
    context.view_layer.objects.active = active
    return len(matching_objects)


def _read_preferences(context):
    addon = context.preferences.addons.get(ADDON_ID)
    return addon.preferences if addon is not None else None


def _tag_viewports(context):
    if context.screen is None:
        return
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


class MC_OT_check_ngons(bpy.types.Operator):
    bl_idname = "mesh.model_checker_check_ngons"
    bl_label = "检查多边面"
    bl_description = "选中顶点数超过设置阈值的面"
    bl_options = {'REGISTER', 'UNDO'}

    ngon_sides: IntProperty(name="边数大于", default=4, min=3, max=100)
    ngon_clear_selection: BoolProperty(name="运行前清除其他选择", default=True)

    @classmethod
    def poll(cls, context):
        return _has_selected_mesh(context)

    def invoke(self, context, event):
        prefs = _read_preferences(context)
        if prefs is None:
            return {'CANCELLED'}
        _copy_preferences_to_operator(self, prefs, ("ngon_sides", "ngon_clear_selection"))
        return self.execute(context)

    def draw(self, context):
        row = self.layout.row()
        row.label(text="边数大于")
        row.prop(self, "ngon_sides", text="")
        self.layout.prop(self, "ngon_clear_selection")

    def execute(self, context):
        prefs = _read_preferences(context)
        if prefs is None:
            self.report({'ERROR'}, "模型检查插件设置不可用")
            return {'CANCELLED'}
        try:
            faces, _ = _select_findings(
                context,
                check_ngons=True,
                ngon_sides=self.ngon_sides,
                clear_selection=self.ngon_clear_selection,
            )
        except RuntimeError as exc:
            self.report({'ERROR'}, f"无法进入网格编辑模式：{exc}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"已选中 {faces} 个多边面")
        _copy_operator_to_preferences(
            self, prefs, ("ngon_sides", "ngon_clear_selection"),
        )
        _tag_viewports(context)
        return {'FINISHED'}


class MC_OT_check_isolated_vertices(bpy.types.Operator):
    bl_idname = "mesh.model_checker_check_isolated"
    bl_label = "检查孤立顶点"
    bl_description = "选中没有连接边的顶点"
    bl_options = {'REGISTER', 'UNDO'}

    isolated_include_hidden: BoolProperty(name="包括隐藏顶点", default=False)
    isolated_clear_selection: BoolProperty(name="运行前清除其他选择", default=True)

    @classmethod
    def poll(cls, context):
        return _has_selected_mesh(context)

    def invoke(self, context, event):
        prefs = _read_preferences(context)
        if prefs is None:
            return {'CANCELLED'}
        _copy_preferences_to_operator(
            self, prefs, ("isolated_include_hidden", "isolated_clear_selection"),
        )
        return self.execute(context)

    def draw(self, context):
        self.layout.prop(self, "isolated_include_hidden")
        self.layout.prop(self, "isolated_clear_selection")

    def execute(self, context):
        prefs = _read_preferences(context)
        if prefs is None:
            self.report({'ERROR'}, "模型检查插件设置不可用")
            return {'CANCELLED'}
        try:
            _, vertices = _select_findings(
                context,
                check_isolated=True,
                clear_selection=self.isolated_clear_selection,
                isolated_include_hidden=self.isolated_include_hidden,
            )
        except RuntimeError as exc:
            self.report({'ERROR'}, f"无法进入网格编辑模式：{exc}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"已选中 {vertices} 个孤立顶点")
        _copy_operator_to_preferences(
            self, prefs, ("isolated_include_hidden", "isolated_clear_selection"),
        )
        _tag_viewports(context)
        return {'FINISHED'}


class MC_OT_toggle_uv_overlay(bpy.types.Operator):
    bl_idname = "mesh.model_checker_toggle_uv"
    bl_label = "切换 UV 显示"
    bl_description = "独立开启或关闭场景中可见模型的 UV 显示"
    bl_options = {'REGISTER', 'UNDO'}

    feature: EnumProperty(
        name="UV 显示项",
        items=(
            ('uv_show_stretch', "UV 拉伸", "切换 UV 拉伸热力图"),
            ('uv_show_boundaries', "UV 分界边", "切换 UV 分界边"),
            ('uv_show_checker', "棋盘格", "切换 UV 棋盘格"),
            ('uv_show_orientation', "UV 正反", "切换 UV 正反显示"),
        ),
    )
    enabled: BoolProperty(name="启用显示", default=False, options={'HIDDEN'})
    uv_stretch_base_color: FloatVectorProperty(name="无拉伸颜色", subtype='COLOR', size=3, default=(0.43, 0.78, 1.0), min=0.0, max=1.0)
    uv_stretch_max_color: FloatVectorProperty(name="拉伸颜色", subtype='COLOR', size=3, default=(0.72, 0.34, 0.025), min=0.0, max=1.0)
    uv_stretch_strength: FloatProperty(name="拉伸显示强度", default=2.0, min=0.25, max=8.0)
    uv_stretch_threshold: FloatProperty(name="可接受拉伸阈值", default=0.0, min=0.0, max=0.999, subtype='PERCENTAGE')
    uv_stretch_opacity: FloatProperty(name="热力图透明度", default=0.85, min=0.0, max=1.0)
    uv_boundary_color: FloatVectorProperty(name="边界颜色", subtype='COLOR', size=3, default=(1.0, 0.55, 0.08), min=0.0, max=1.0)
    uv_boundary_width: FloatProperty(name="分界线粗细", default=4.0, min=1.0, max=12.0)
    uv_checker_opacity: FloatProperty(name="棋盘格透明度", default=0.75, min=0.0, max=1.0)
    uv_checker_density: IntProperty(name="棋盘格密度", default=8, min=1, max=256)
    uv_checker_color_a: FloatVectorProperty(name="棋盘格颜色 A", subtype='COLOR', size=3, default=(0.95, 0.95, 0.95), min=0.0, max=1.0)
    uv_checker_color_b: FloatVectorProperty(name="棋盘格颜色 B", subtype='COLOR', size=3, default=(0.12, 0.12, 0.12), min=0.0, max=1.0)
    uv_checker_pattern: EnumProperty(
        name="棋盘格图案",
        items=diagnostic.CHECKER_PATTERN_ITEMS,
        default='CLASSIC',
    )
    uv_checker_custom_image: StringProperty(
        name="自定义棋盘格图片",
        subtype='FILE_PATH',
        default="",
    )
    uv_orientation_front_color: FloatVectorProperty(name="正向颜色", subtype='COLOR', size=3, default=(0.43, 0.78, 1.0), min=0.0, max=1.0)
    uv_orientation_back_color: FloatVectorProperty(name="反向颜色", subtype='COLOR', size=3, default=(0.94, 0.045, 0.055), min=0.0, max=1.0)
    uv_orientation_opacity: FloatProperty(name="UV 正反透明度", default=0.9, min=0.0, max=1.0)

    @classmethod
    def poll(cls, context):
        return (
            context.area is not None
            and context.area.type == 'VIEW_3D'
            and context.scene is not None
            and bool(_visible_meshes(context))
        )

    def invoke(self, context, event):
        prefs = _read_preferences(context)
        if prefs is None:
            return {'CANCELLED'}
        self.enabled = not bool(getattr(prefs, self.feature))
        _copy_preferences_to_operator(self, prefs, _UV_FEATURE_SETTINGS[self.feature])
        return self.execute(context)

    def draw(self, context):
        layout = self.layout
        if self.feature == 'uv_show_stretch':
            layout.label(text="UV 拉伸")
            layout.prop(self, "uv_stretch_base_color")
            layout.prop(self, "uv_stretch_max_color")
            layout.prop(self, "uv_stretch_strength", slider=True)
            layout.prop(self, "uv_stretch_threshold", slider=True)
            layout.prop(self, "uv_stretch_opacity", slider=True)
        elif self.feature == 'uv_show_boundaries':
            layout.label(text="UV 分界边")
            layout.prop(self, "uv_boundary_color")
            layout.prop(self, "uv_boundary_width", slider=True)
        elif self.feature == 'uv_show_checker':
            layout.label(text="棋盘格")
            prefs = _read_preferences(context)
            if prefs is None:
                layout.label(text="棋盘格设置不可用", icon='ERROR')
            else:
                layout.prop(prefs, "uv_checker_pattern")
                layout.prop(prefs, "uv_checker_opacity", slider=True)
                layout.prop(prefs, "uv_checker_density")
                layout.label(text="每个图案/图片独立记忆密度", icon='INFO')
                if prefs.uv_checker_pattern == 'CLASSIC':
                    layout.prop(prefs, "uv_checker_color_a")
                    layout.prop(prefs, "uv_checker_color_b")
                elif prefs.uv_checker_pattern == 'CUSTOM':
                    layout.prop(prefs, "uv_checker_custom_image")
                    if not diagnostic.checker_custom_image_available(prefs):
                        layout.label(text="图片无效，将暂时使用经典棋盘", icon='ERROR')
                    layout.label(text="密度 8 = 原图 1:1，数值越大平铺越密", icon='INFO')
                else:
                    layout.label(text="密度 8 = 原图 1:1（8×8 格），越大越密", icon='INFO')
        else:
            layout.label(text="UV 正反")
            layout.prop(self, "uv_orientation_front_color")
            layout.prop(self, "uv_orientation_back_color")
            layout.prop(self, "uv_orientation_opacity", slider=True)

    def execute(self, context):
        prefs = _read_preferences(context)
        if prefs is None:
            self.report({'ERROR'}, "模型检查插件设置不可用")
            return {'CANCELLED'}
        if self.enabled and self.feature in {'uv_show_stretch', 'uv_show_orientation'}:
            _ensure_object_mode(context)
        setting_names = _UV_FEATURE_SETTINGS[self.feature]
        _copy_operator_to_preferences(self, prefs, setting_names)
        previous_value = getattr(prefs, self.feature)
        next_value = self.enabled
        if next_value and self.feature == 'uv_show_checker':
            prefs.uv_show_stretch = False
            prefs.uv_show_orientation = False
        elif next_value and self.feature in {'uv_show_stretch', 'uv_show_orientation'}:
            prefs.uv_show_checker = False
        if previous_value != next_value:
            setattr(prefs, self.feature, next_value)
        visible_meshes = _visible_meshes(context)
        if getattr(prefs, self.feature) and not any(obj.data.uv_layers.active for obj in visible_meshes):
            self.report({'WARNING'}, "场景中可见的模型没有 UV 层")
        if (
            self.feature in {'uv_show_stretch', 'uv_show_orientation'}
            and getattr(prefs, self.feature)
        ):
            state = context.scene.model_checker_state
            state.refresh_in_progress = True
            try:
                found_objects = _select_uv_findings(context, prefs, feature=self.feature)
            except RuntimeError as exc:
                setattr(prefs, self.feature, previous_value)
                self.report({'ERROR'}, f"无法选择 UV 问题物体：{exc}")
                return {'CANCELLED'}
            finally:
                state.refresh_in_progress = False
            self.report({'INFO'}, f"已在物体模式下选中 {found_objects} 个含 UV 问题的物体")
        _tag_viewports(context)
        return {'FINISHED'}


class MC_OT_settings_popup(bpy.types.Operator):
    bl_idname = "wm.model_checker_settings"
    bl_label = "功能设置"
    bl_options = {'REGISTER', 'UNDO'}

    target: EnumProperty(
        name="设置项",
        items=(
            ('NGONS', "多边面", "设置多边面检查"),
            ('UV_STRETCH', "UV 拉伸", "设置 UV 热力图"),
            ('UV_BOUNDARIES', "UV 分界边", "设置 UV 分界边"),
            ('UV_CHECKER', "棋盘格", "设置 UV 棋盘格"),
            ('UV_ORIENTATION', "UV 正反", "设置 UV 正反显示"),
            ('ISOLATED', "孤立顶点", "设置孤立顶点检查"),
        ),
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=320)

    def draw(self, context):
        prefs = _read_preferences(context)
        if prefs is None:
            self.layout.label(text="模型检查插件设置不可用", icon='ERROR')
            return
        layout = self.layout
        if self.target == 'NGONS':
            layout.label(text="多边面检查")
            row = layout.row()
            row.label(text="边数大于")
            row.prop(prefs, "ngon_sides", text="")
            layout.prop(prefs, "ngon_clear_selection")
        elif self.target == 'UV_STRETCH':
            layout.label(text="整面 UV 拉伸热力图")
            layout.prop(prefs, "uv_stretch_base_color")
            layout.prop(prefs, "uv_stretch_max_color")
            layout.prop(prefs, "uv_stretch_strength", slider=True)
            layout.prop(prefs, "uv_stretch_threshold", slider=True)
            layout.prop(prefs, "uv_stretch_opacity", slider=True)
        elif self.target == 'UV_BOUNDARIES':
            layout.label(text="UV 分界边")
            layout.prop(prefs, "uv_boundary_color")
            layout.prop(prefs, "uv_boundary_width", slider=True)
        elif self.target == 'UV_CHECKER':
            layout.label(text="UV 棋盘格")
            layout.prop(prefs, "uv_checker_pattern")
            layout.prop(prefs, "uv_checker_opacity", slider=True)
            layout.prop(prefs, "uv_checker_density")
            layout.label(text="每个图案/图片独立记忆密度", icon='INFO')
            if prefs.uv_checker_pattern == 'CLASSIC':
                layout.prop(prefs, "uv_checker_color_a")
                layout.prop(prefs, "uv_checker_color_b")
            elif prefs.uv_checker_pattern == 'CUSTOM':
                layout.prop(prefs, "uv_checker_custom_image")
                if not diagnostic.checker_custom_image_available(prefs):
                    layout.label(text="图片无效，将暂时使用经典棋盘", icon='ERROR')
                layout.label(text="密度 8 = 原图 1:1，数值越大平铺越密", icon='INFO')
            else:
                layout.label(text="密度 8 = 原图 1:1（8×8 格），越大越密", icon='INFO')
        elif self.target == 'UV_ORIENTATION':
            layout.label(text="UV 正反")
            layout.prop(prefs, "uv_orientation_front_color")
            layout.prop(prefs, "uv_orientation_back_color")
            layout.prop(prefs, "uv_orientation_opacity", slider=True)
        else:
            layout.label(text="孤立顶点检查")
            layout.prop(prefs, "isolated_include_hidden")
            layout.prop(prefs, "isolated_clear_selection")

    def execute(self, context):
        return {'FINISHED'}


def unregister_keymap():
    """Remove any legacy aggregate-check shortcut left by an older version."""
    keyconfig = bpy.context.window_manager.keyconfigs.addon if bpy.context.window_manager else None
    if keyconfig is None:
        return
    keymap = keyconfig.keymaps.get("3D View")
    if keymap is None:
        return
    for item in tuple(keymap.keymap_items):
        if item.idname == "mesh.model_checker_run":
            keymap.keymap_items.remove(item)
