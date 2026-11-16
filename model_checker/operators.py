"""Individual model checks with settings in Blender's last-operation panel."""

import bpy
import bmesh
import types
import math
import numpy as np
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    StringProperty,
)

from . import diagnostic, overlay

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


def _selection_mode_for_findings(check_ngons, check_isolated):
    if check_ngons and check_isolated:
        return (True, False, True)
    if check_ngons:
        return (False, False, True)
    if check_isolated:
        return (True, False, False)
    return (True, True, True)


def _check_result_message(check_name, count, result_name):
    if count:
        return f"{check_name}检查完成：已选中 {count} 个{result_name}。"
    return f"{check_name}检查完成：未发现{result_name}。"


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
    context.tool_settings.mesh_select_mode = _selection_mode_for_findings(
        check_ngons, check_isolated,
    )
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


def _collapsed_uv_faces(obj, tolerance):
    """Read the active UV layer; a line or a small-area island is not a point."""
    if obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        bm.faces.index_update()
        layer = bm.loops.layers.uv.active
        if layer is None:
            return None
        indices = []
        for face in bm.faces:
            if face.hide or not face.loops:
                continue
            coords = [loop[layer].uv for loop in face.loops]
            if not all(math.isfinite(value) for coord in coords for value in coord):
                continue
            width = max(coord.x for coord in coords) - min(coord.x for coord in coords)
            height = max(coord.y for coord in coords) - min(coord.y for coord in coords)
            if width * width + height * height < tolerance * tolerance:
                indices.append(face.index)
        return indices
    mesh = obj.data
    layer = mesh.uv_layers.active
    if layer is None:
        return None
    if not mesh.polygons:
        return []
    coords = np.empty(len(mesh.loops) * 2, dtype=np.float64)
    layer.uv.foreach_get('vector', coords)
    coords = coords.reshape((-1, 2))
    starts = np.empty(len(mesh.polygons), dtype=np.int32)
    hidden = np.empty(len(mesh.polygons), dtype=bool)
    mesh.polygons.foreach_get('loop_start', starts)
    mesh.polygons.foreach_get('hide', hidden)
    span = np.maximum.reduceat(coords, starts) - np.minimum.reduceat(coords, starts)
    finite = np.logical_and.reduceat(np.isfinite(coords).all(axis=1), starts)
    return np.flatnonzero((np.einsum('ij,ij->i', span, span) < tolerance * tolerance)
                          & finite & ~hidden).tolist()


def _model_boundary_findings(objects):
    findings, cache = [], {}
    for obj in objects:
        if obj.type != 'MESH' or not obj.is_editable or not obj.data.is_editable:
            continue
        key = obj.data.as_pointer()
        if key not in cache:
            editing = obj.mode == 'EDIT'
            bm = bmesh.from_edit_mesh(obj.data) if editing else bmesh.new()
            try:
                if not editing:
                    bm.from_mesh(obj.data)
                bm.verts.ensure_lookup_table()
                bm.edges.ensure_lookup_table()
                bm.faces.ensure_lookup_table()
                bm.edges.index_update()
                cache[key] = [edge.index for edge in bm.edges if not edge.hide and edge.is_boundary]
            finally:
                if not editing:
                    bm.free()
        if cache[key]:
            findings.append((obj, cache[key]))
    return findings


def _select_model_boundaries(context, findings):
    if not findings:
        return
    _ensure_object_mode(context)
    for obj in context.selected_objects:
        obj.select_set(False)
    for obj, _ in findings:
        obj.select_set(True)
    context.view_layer.objects.active = findings[0][0]
    bpy.ops.object.mode_set(mode='EDIT')
    context.tool_settings.mesh_select_mode = (False, True, False)
    unique = set()
    for obj, edges in findings:
        if obj.data.as_pointer() in unique:
            continue
        unique.add(obj.data.as_pointer())
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        _clear_selection(bm)
        bm.select_history.clear()
        for index in edges:
            bm.edges[index].select_set(True)
        bm.normal_update()
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)


class MC_OT_check_model_boundaries(bpy.types.Operator):
    bl_idname = 'mesh.model_checker_check_model_boundaries'
    bl_label = '检查模型边界'
    bl_description = '进入边选择模式，选中所选模型的开放边界和孔洞边缘'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _has_selected_mesh(context) and context.mode in {'OBJECT', 'EDIT_MESH'}

    def execute(self, context):
        findings = _model_boundary_findings(_selected_meshes(context))
        _select_model_boundaries(context, findings)
        count = sum(len(edges) for _, edges in findings)
        message = _check_result_message('模型边界', count, '边界边')
        overlay.show_check_feedback(context, message, 'INFO' if count else 'WARNING')
        self.report({'INFO'}, message)
        _tag_viewports(context)
        return {'FINISHED'}


class MC_OT_check_collapsed_uv(bpy.types.Operator):
    bl_idname = 'mesh.model_checker_check_collapsed_uv'
    bl_label = '检查 UV 缩点'
    bl_description = '检查当前 UV 层中全部 UV 坐标重合成一点的面；多物体选物体，单物体选问题面'
    bl_options = {'REGISTER', 'UNDO'}

    tolerance: FloatProperty(name='坐标容差', default=1e-6, min=1e-9, max=0.001, precision=6)

    @classmethod
    def poll(cls, context):
        return (context.mode in {'OBJECT', 'EDIT_MESH'} and context.area is not None
                and context.area.type == 'VIEW_3D')

    def execute(self, context):
        if context.mode == 'EDIT_MESH':
            objects = tuple(obj for obj in context.objects_in_mode if obj.type == 'MESH')
        else:
            objects = _selected_meshes(context) or tuple(_visible_meshes(context))
        findings, cache = [], {}
        missing_uv = 0
        skipped = 0
        for obj in objects:
            if not obj.is_editable or not obj.data.is_editable:
                skipped += 1
                continue
            key = obj.data.as_pointer()
            if key not in cache:
                cache[key] = _collapsed_uv_faces(obj, self.tolerance)
            indices = cache[key]
            if indices is None:
                missing_uv += 1
            elif indices:
                findings.append((obj, indices))
        count = sum(len(indices) for _, indices in findings)
        if findings:
            _ensure_object_mode(context)
            for obj in context.selected_objects:
                obj.select_set(False)
            for obj, _ in findings:
                obj.select_set(True)
            context.view_layer.objects.active = findings[0][0]
            if len(findings) == 1:
                obj, indices = findings[0]
                bpy.ops.object.mode_set(mode='EDIT')
                context.tool_settings.mesh_select_mode = (False, False, True)
                bm = bmesh.from_edit_mesh(obj.data)
                bm.verts.ensure_lookup_table()
                bm.edges.ensure_lookup_table()
                bm.faces.ensure_lookup_table()
                _clear_selection(bm)
                bm.select_history.clear()
                for index in indices:
                    bm.faces[index].select_set(True)
                bm.faces.active = bm.faces[indices[0]]
                bm.normal_update()
                bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        message = (f'UV 缩点检查完成：{len(findings)} 个问题物体，{count} 个问题面。'
                   if findings else 'UV 缩点检查完成：未发现 UV 缩成一点的面。')
        if missing_uv:
            message += f' {missing_uv} 个物体没有 UV，已跳过。'
        if skipped:
            message += f' {skipped} 个物体不可编辑，已跳过。'
        overlay.show_check_feedback(context, message, 'INFO' if findings else 'WARNING')
        self.report({'INFO'}, message)
        _tag_viewports(context)
        return {'FINISHED'}


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
        result_level = 'INFO' if faces else 'WARNING'
        result_message = _check_result_message("多边面", faces, "问题面")
        overlay.show_check_feedback(context, result_message, result_level)
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
        result_level = 'INFO' if vertices else 'WARNING'
        result_message = _check_result_message("孤立顶点", vertices, "孤立顶点")
        overlay.show_check_feedback(context, result_message, result_level)
        _copy_operator_to_preferences(
            self, prefs, ("isolated_include_hidden", "isolated_clear_selection"),
        )
        _tag_viewports(context)
        return {'FINISHED'}


def _update_operator_feature(self, feature):
    if self.uv_controls_initializing or self.uv_display_mode != 'CLASSIC' or not getattr(self, feature):
        return
    self.uv_controls_initializing = True
    try:
        if feature == 'uv_show_checker':
            self.uv_show_stretch = self.uv_show_orientation = False
        else:
            self.uv_show_checker = False
    finally:
        self.uv_controls_initializing = False


def _operator_stretch(self, context):
    _update_operator_feature(self, 'uv_show_stretch')


def _operator_orientation(self, context):
    _update_operator_feature(self, 'uv_show_orientation')


def _operator_checker(self, context):
    _update_operator_feature(self, 'uv_show_checker')


def _operator_display_mode(self, context):
    if self.uv_controls_initializing:
        return
    if self.uv_display_mode == 'CLASSIC' and (self.uv_show_stretch or self.uv_show_orientation):
        self.uv_show_checker = False


def _capture_uv_display(operator, prefs):
    operator.uv_controls_initializing = True
    try:
        for name in ('uv_display_mode','uv_show_stretch','uv_show_orientation','uv_show_checker'):
            setattr(operator,name,getattr(prefs,name))
    finally:
        operator.uv_controls_initializing = False


def _apply_uv_display(operator, prefs, context):
    prefs.uv_mode_normalizing = True
    try:
        for name in ('uv_display_mode','uv_show_stretch','uv_show_orientation','uv_show_checker'):
            setattr(prefs,name,getattr(operator,name))
    finally:
        prefs.uv_mode_normalizing = False
    from . import _update_scene_overlay
    _update_scene_overlay(prefs, context)
    from . import native_preview
    native_preview.prepare_display(context,prefs)


class MC_OT_toggle_uv_overlay(bpy.types.Operator):
    bl_idname = "mesh.model_checker_toggle_uv"
    bl_label = "切换 UV 显示"
    bl_description = "独立开启或关闭场景中可见模型的 UV 显示"
    bl_options = {'REGISTER', 'UNDO'}
    uv_controls_initializing: BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})
    uv_display_mode: EnumProperty(name='显示方式', items=(
        ('CLASSIC','实体',''),('MATERIAL','材质','')), default='MATERIAL', update=_operator_display_mode)
    uv_show_stretch: BoolProperty(name='拉伸', default=False, update=_operator_stretch)
    uv_show_orientation: BoolProperty(name='正反', default=False, update=_operator_orientation)
    uv_show_checker: BoolProperty(name='棋盘格', default=False, update=_operator_checker)

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
    uv_stretch_max_color: FloatVectorProperty(name="拉伸颜色", subtype='COLOR', size=3, default=(1.0, 0.0, 0.0), min=0.0, max=1.0)
    uv_stretch_strength: FloatProperty(name="拉伸显示强度", default=2.0, min=0.25, max=8.0)
    uv_stretch_threshold: FloatProperty(name="可接受拉伸阈值", default=0.2, min=0.0, max=0.999, subtype='PERCENTAGE')
    uv_stretch_opacity: FloatProperty(name="热力图透明度", default=0.85, min=0.0, max=1.0)
    uv_boundary_color: FloatVectorProperty(name="边界颜色", subtype='COLOR', size=3, default=(1.0, 0.55, 0.08), min=0.0, max=1.0)
    uv_boundary_width: FloatProperty(name="分界线粗细", default=4.0, min=1.0, max=12.0)
    uv_checker_opacity: FloatProperty(name="棋盘格透明度", default=0.75, min=0.0, max=1.0)
    uv_checker_density: IntProperty(name="棋盘格密度", default=64, min=1, max=256)
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
    uv_orientation_opacity: FloatProperty(name="UV 正反透明度", default=0.8, min=0.0, max=1.0)

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
        _capture_uv_display(self, prefs)
        if self.feature != 'uv_show_boundaries':
            setattr(self, self.feature, self.enabled)
        _copy_preferences_to_operator(self, prefs, _UV_FEATURE_SETTINGS[self.feature])
        return self.execute(context)

    def draw(self, context):
        layout = self.layout
        prefs = _read_preferences(context)
        if prefs is not None and self.feature != 'uv_show_boundaries':
            _draw_uv_display_controls(layout, self)
        if prefs is not None and prefs.uv_display_mode == 'MATERIAL' and self.feature in {'uv_show_stretch', 'uv_show_orientation'}:
            _draw_native_colors(layout, prefs, self.feature == 'uv_show_stretch')
            return
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
        if self.feature != 'uv_show_boundaries' and not self.properties.is_property_set('uv_display_mode'):
            _capture_uv_display(self, prefs)
            setattr(self, self.feature, self.enabled)
        if self.feature in {'uv_show_stretch','uv_show_orientation'} and getattr(self,self.feature):
            _ensure_object_mode(context)
        setting_names = _UV_FEATURE_SETTINGS[self.feature]
        _copy_operator_to_preferences(self, prefs, setting_names)
        previous_value = getattr(prefs, self.feature)
        if self.feature == 'uv_show_boundaries':
            setattr(prefs,self.feature,self.enabled)
        else:
            _apply_uv_display(self,prefs,context)
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


class MC_OT_uv_checks(bpy.types.Operator):
    bl_idname = "mesh.model_checker_uv_checks"
    bl_label = "UV 综合检查"
    bl_description = "切换 UV 拉伸、正反和棋盘格；左下角可分别勾选这三项"
    bl_options = {'REGISTER', 'UNDO'}
    uv_controls_initializing: BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})
    uv_display_mode: EnumProperty(name='显示方式', items=(
        ('CLASSIC','实体',''),('MATERIAL','材质','')), default='MATERIAL', update=_operator_display_mode)
    uv_show_stretch: BoolProperty(name='拉伸', default=False, update=_operator_stretch)
    uv_show_orientation: BoolProperty(name='正反', default=False, update=_operator_orientation)
    uv_show_checker: BoolProperty(name='棋盘格', default=False, update=_operator_checker)
    enabled: BoolProperty(default=True, options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return MC_OT_toggle_uv_overlay.poll(context)

    def invoke(self, context, event):
        prefs = _read_preferences(context)
        if prefs is None:
            return {'CANCELLED'}
        self.enabled = not any((prefs.uv_show_stretch, prefs.uv_show_orientation, prefs.uv_show_checker))
        self._prepare(prefs)
        return self.execute(context)

    def _prepare(self, prefs):
        self.uv_controls_initializing = True
        try:
            self.uv_display_mode = 'MATERIAL' if self.enabled else prefs.uv_display_mode
            self.uv_show_stretch = self.uv_show_orientation = self.uv_show_checker = self.enabled
        finally:
            self.uv_controls_initializing = False

    def draw(self, context):
        _draw_uv_display_controls(self.layout, self)

    def execute(self, context):
        prefs = _read_preferences(context)
        if prefs is None:
            return {'CANCELLED'}
        if not self.properties.is_property_set('uv_display_mode'):
            if not self.properties.is_property_set('enabled'):
                self.enabled = not any((prefs.uv_show_stretch, prefs.uv_show_orientation, prefs.uv_show_checker))
            self._prepare(prefs)
        _apply_uv_display(self, prefs, context)
        _tag_viewports(context)
        active = any((prefs.uv_show_stretch, prefs.uv_show_orientation, prefs.uv_show_checker))
        self.report({'INFO'}, "UV 综合检查已开启，可在左下角选择显示项" if active else "UV 综合检查已关闭")
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
        if self.target in {'UV_STRETCH', 'UV_ORIENTATION', 'UV_CHECKER'}:
            _draw_uv_display_controls(layout, prefs)
        if prefs.uv_display_mode == 'MATERIAL' and self.target in {'UV_STRETCH', 'UV_ORIENTATION'}:
            _draw_native_colors(layout, prefs, self.target == 'UV_STRETCH')
            return
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


def _draw_native_colors(layout, prefs, stretch):
    if stretch:
        layout.prop(prefs, 'uv_material_base_color')
        layout.prop(prefs, 'uv_material_max_color')
        layout.prop(prefs, 'uv_stretch_strength', slider=True)
        layout.prop(prefs, 'uv_stretch_threshold', slider=True)
    else:
        layout.prop(prefs, 'uv_material_front_color')
        layout.prop(prefs, 'uv_material_back_color')
    layout.label(text='Alpha 0：显示下层棋盘格或原材质', icon='INFO')
    layout.label(text='原材质按材质预览的光照显示')


def _draw_uv_display_controls(layout, prefs):
    layout.row(align=True).prop(prefs, 'uv_display_mode', expand=True)
    row = layout.row(align=True)
    row.prop(prefs, 'uv_show_stretch', text='拉伸')
    row.prop(prefs, 'uv_show_orientation', text='正反')
    row.prop(prefs, 'uv_show_checker', text='棋盘格')
    if prefs.uv_display_mode == 'CLASSIC':
        layout.label(text='实体模式：棋盘格与拉伸、正反互斥', icon='INFO')

