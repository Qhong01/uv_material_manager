import bpy
from bpy.types import Operator, Menu
from bpy.props import StringProperty, BoolProperty
from bpy.app.handlers import persistent
from . import modifier_layouts

# 修改器官方矢量图标映射
MODIFIER_ICONS = {
    'DATA_TRANSFER': 'MOD_DATA_TRANSFER',
    'MESH_CACHE': 'MOD_MESHDEFORM',
    'MESH_SEQUENCE_CACHE': 'MOD_MESHDEFORM',
    'NORMAL_EDIT': 'MOD_NORMALEDIT',
    'WEIGHTED_NORMAL': 'MOD_NORMALEDIT',
    'UV_PROJECT': 'MOD_UVPROJECT',
    'UV_WARP': 'MOD_UVPROJECT',
    'VERTEX_WEIGHT_EDIT': 'MOD_VERTEX_WEIGHT',
    'VERTEX_WEIGHT_MIX': 'MOD_VERTEX_WEIGHT',
    'VERTEX_WEIGHT_PROXIMITY': 'MOD_VERTEX_WEIGHT',
    'ARRAY': 'MOD_ARRAY',
    'BEVEL': 'MOD_BEVEL',
    'BOOLEAN': 'MOD_BOOLEAN',
    'BUILD': 'MOD_BUILD',
    'DECIMATE': 'MOD_DECIM',
    'EDGE_SPLIT': 'MOD_EDGESPLIT',
    'NODES': 'GEOMETRY_NODES',
    'MASK': 'MOD_MASK',
    'MIRROR': 'MOD_MIRROR',
    'MULTIRES': 'MOD_MULTIRES',
    'REMESH': 'MOD_REMESH',
    'SCREW': 'MOD_SCREW',
    'SKIN': 'MOD_SKIN',
    'SOLIDIFY': 'MOD_SOLIDIFY',
    'SUBSURF': 'MOD_SUBSURF',
    'TRIANGULATE': 'MOD_TRIANGULATE',
    'VOLUME_TO_MESH': 'VOLUME_DATA',
    'MESH_TO_VOLUME': 'VOLUME_DATA',
    'WELD': 'AUTOMERGE_OFF',
    'WIREFRAME': 'MOD_WIREFRAME',
    'ARMATURE': 'MOD_ARMATURE',
    'CAST': 'MOD_CAST',
    'CURVE': 'MOD_CURVE',
    'DISPLACE': 'MOD_DISPLACE',
    'HOOK': 'HOOK',
    'LAPLACIANDEFORM': 'MOD_MESHDEFORM',
    'LAPLACIANSMOOTH': 'MOD_SMOOTH',
    'LATTICE': 'MOD_LATTICE',
    'MESH_DEFORM': 'MOD_MESHDEFORM',
    'SHRINKWRAP': 'MOD_SHRINKWRAP',
    'SIMPLE_DEFORM': 'MOD_SIMPLEDEFORM',
    'SMOOTH': 'MOD_SMOOTH',
    'CORRECTIVE_SMOOTH': 'MOD_SMOOTH',
    'SURFACE_DEFORM': 'MOD_MESHDEFORM',
    'WARP': 'MOD_WARP',
    'WAVE': 'MOD_WAVE',
    'CLOTH': 'MOD_CLOTH',
    'COLLISION': 'MOD_PHYSICS',
    'DYNAMIC_PAINT': 'MOD_DYNAMICPAINT',
    'EXPLODE': 'MOD_EXPLODE',
    'FLUID': 'MOD_FLUID',
    'OCEAN': 'MOD_OCEAN',
    'PARTICLE_INSTANCE': 'MOD_PARTICLES',
    'PARTICLE_SYSTEM': 'MOD_PARTICLES',
    'SOFT_BODY': 'MOD_SOFT',
    'SURFACE': 'MOD_PHYSICS',
}

def get_modifier_icon(mod_type):
    return MODIFIER_ICONS.get(mod_type, 'MODIFIER')


# =========================================================================
# 实时自动同步引擎 (仅在修改参数时同步，切换选中时不触发)
# =========================================================================

class ModifierAutoSync:
    _snapshots = {}
    _is_syncing = False
    _last_selection = ()

    @classmethod
    def serialize_value(cls, val):
        if val is None:
            return None
        if isinstance(val, (int, float, str, bool)):
            return val
        if isinstance(val, bpy.types.bpy_struct):
            return getattr(val, 'name', str(val))
        try:
            if not isinstance(val, (str, bytes)):
                return tuple(cls.serialize_value(x) for x in val)
        except TypeError:
            pass
        return str(val)

    @classmethod
    def get_snapshot(cls, mod):
        snap = {}
        ignored = {
            'name', 'type', 'show_expanded', 'is_active',
            'execution_time', 'is_override_data'
        }
        for p in mod.bl_rna.properties:
            if not p.is_readonly and p.identifier not in ignored:
                try:
                    val = getattr(mod, p.identifier)
                    snap[p.identifier] = cls.serialize_value(val)
                except Exception:
                    pass
        return snap

    @classmethod
    def ensure_snapshots(cls, objects):
        """确保当前选中的所有物体及其修改器在快照池中都有基准记录"""
        for obj in objects:
            if obj and obj.type == 'MESH':
                for mod in obj.modifiers:
                    key = (obj.name, mod.name)
                    cls._snapshots[key] = cls.get_snapshot(mod)

    @classmethod
    def update_handler(cls, scene, depsgraph):
        if cls._is_syncing:
            return

        if hasattr(scene, "um_auto_sync_modifiers") and not scene.um_auto_sync_modifiers:
            return

        context = bpy.context
        active_obj = context.view_layer.objects.active
        if not active_obj or active_obj.type != 'MESH':
            return

        selected_objs = [o for o in context.selected_objects if o.type == 'MESH']
        if not selected_objs:
            return

        curr_selection = (active_obj.name, tuple(sorted(o.name for o in selected_objs)))

        # 如果用户刚在视图中切换了选择，仅更新基准快照，绝不触发参数同步
        if curr_selection != cls._last_selection:
            cls._last_selection = curr_selection
            cls.ensure_snapshots(selected_objs)
            return

        other_selected = [o for o in selected_objs if o != active_obj]
        if not other_selected:
            for mod in active_obj.modifiers:
                cls._snapshots[(active_obj.name, mod.name)] = cls.get_snapshot(mod)
            return

        cls._is_syncing = True
        try:
            for mod in active_obj.modifiers:
                key = (active_obj.name, mod.name)
                curr_snap = cls.get_snapshot(mod)
                prev_snap = cls._snapshots.get(key)

                changed_props = []
                if prev_snap is not None:
                    for prop_id, curr_val in curr_snap.items():
                        if prop_id in prev_snap and prev_snap[prop_id] != curr_val:
                            changed_props.append((prop_id, curr_val))

                if changed_props:
                    for prop_id, curr_val in changed_props:
                        raw_val = getattr(mod, prop_id)
                        for target_obj in other_selected:
                            target_mod = target_obj.modifiers.get(mod.name)
                            if not target_mod:
                                for m in target_obj.modifiers:
                                    if m.type == mod.type:
                                        target_mod = m
                                        break
                            if target_mod and hasattr(target_mod, prop_id):
                                try:
                                    if isinstance(curr_val, tuple):
                                        setattr(target_mod, prop_id, list(raw_val))
                                    else:
                                        setattr(target_mod, prop_id, raw_val)
                                    target_key = (target_obj.name, target_mod.name)
                                    if target_key in cls._snapshots:
                                        cls._snapshots[target_key][prop_id] = curr_val
                                    else:
                                        cls._snapshots[target_key] = cls.get_snapshot(target_mod)
                                except Exception:
                                    pass

                cls._snapshots[key] = curr_snap
        finally:
            cls._is_syncing = False


@persistent
def depsgraph_update_handler(scene, depsgraph):
    ModifierAutoSync.update_handler(scene, depsgraph)


# =========================================================================
# 操作符定义
# =========================================================================

class MODIFIER_OT_SelectByModifier(Operator):
    """在场景中选中所有使用该修改器的物体"""
    bl_idname = "object.select_by_modifier"
    bl_label = "选择使用此修改器的物体"
    bl_options = {'REGISTER', 'UNDO'}

    modifier_name: StringProperty(name="修改器名称")
    modifier_type: StringProperty(name="修改器类型")

    def execute(self, context):
        matched = []
        for obj in context.view_layer.objects:
            if obj.type == 'MESH':
                if self.modifier_name in obj.modifiers:
                    matched.append(obj)
                elif self.modifier_type and any(m.type == self.modifier_type for m in obj.modifiers):
                    matched.append(obj)

        if not matched:
            self.report({'WARNING'}, f"场景中没有使用修改器 {self.modifier_name} 的网格物体")
            return {'CANCELLED'}

        bpy.ops.object.select_all(action='DESELECT')
        for obj in matched:
            obj.select_set(True)
        context.view_layer.objects.active = matched[0]
        self.report({'INFO'}, f"已在场景中选中 {len(matched)} 个使用修改器 '{self.modifier_name}' 的物体")
        return {'FINISHED'}


class MODIFIER_OT_ToggleExpanded(Operator):
    """展开或折叠修改器"""
    bl_idname = "object.custom_modifier_toggle_expanded"
    bl_label = "展开/折叠修改器"
    bl_options = {'INTERNAL'}

    modifier_name: StringProperty(name="修改器名称")
    target_object_name: StringProperty(name="目标物体名称", default="")

    def execute(self, context):
        obj = None
        if self.target_object_name:
            obj = bpy.data.objects.get(self.target_object_name)
        if not obj:
            obj = context.active_object

        if obj and self.modifier_name in obj.modifiers:
            mod = obj.modifiers[self.modifier_name]
            mod.show_expanded = not mod.show_expanded
        return {'FINISHED'}


class MODIFIER_OT_AddCustom(Operator):
    """为选中的所有网格物体添加修改器"""
    bl_idname = "object.add_custom_modifier"
    bl_label = "添加修改器"
    bl_options = {'REGISTER', 'UNDO'}

    modifier_type: StringProperty(name="修改器类型")

    def execute(self, context):
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not selected_objs and context.active_object and context.active_object.type == 'MESH':
            selected_objs = [context.active_object]

        if not selected_objs:
            self.report({'WARNING'}, "未选择任何网格物体")
            return {'CANCELLED'}

        added_count = 0
        for obj in selected_objs:
            try:
                mod = obj.modifiers.new(
                    name=self.modifier_type.capitalize().replace('_', ' '),
                    type=self.modifier_type
                )
                ModifierAutoSync._snapshots[(obj.name, mod.name)] = ModifierAutoSync.get_snapshot(mod)
                added_count += 1
            except Exception as e:
                self.report({'WARNING'}, f"在物体 {obj.name} 上添加修改器失败: {str(e)}")

        self.report({'INFO'}, f"已为 {added_count} 个物体添加修改器")
        return {'FINISHED'}


class MODIFIER_OT_ApplyToAll(Operator):
    """应用该修改器到所有拥有它的选中物体"""
    bl_idname = "object.apply_modifier_to_all"
    bl_label = "应用修改器到全部"
    bl_options = {'REGISTER', 'UNDO'}

    modifier_name: StringProperty(name="修改器名称")

    def execute(self, context):
        processed = 0
        errors = []
        original_active = context.view_layer.objects.active

        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue

            mod = obj.modifiers.get(self.modifier_name)
            if not mod:
                continue

            try:
                context.view_layer.objects.active = obj
                bpy.ops.object.modifier_apply(modifier=mod.name)
                ModifierAutoSync._snapshots.pop((obj.name, mod.name), None)
                processed += 1
            except Exception as e:
                errors.append(f"{obj.name}: {str(e)}")

        context.view_layer.objects.active = original_active

        if errors:
            self.report({'WARNING'}, f"{processed}个成功，失败{len(errors)}个：{', '.join(errors)}")
        else:
            self.report({'INFO'}, f"成功应用到 {processed} 个物体")
        return {'FINISHED'}


class MODIFIER_OT_RemoveFromSelected(Operator):
    """从选中的物体中移除此修改器"""
    bl_idname = "object.remove_modifier_from_selected"
    bl_label = "批量移除修改器"
    bl_options = {'REGISTER', 'UNDO'}

    modifier_name: StringProperty(name="修改器名称")

    def execute(self, context):
        processed = 0
        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue
            if mod := obj.modifiers.get(self.modifier_name):
                ModifierAutoSync._snapshots.pop((obj.name, mod.name), None)
                obj.modifiers.remove(mod)
                processed += 1

        self.report({'INFO'}, f"已从 {processed} 个物体移除修改器")
        return {'FINISHED'}


# =========================================================================
# 添加修改器分类菜单（与 Blender 原生一致）
# =========================================================================

class OBJECT_MT_CustomModifierAdd(Menu):
    bl_idname = "OBJECT_MT_custom_modifier_add"
    bl_label = "添加修改器"

    def draw(self, context):
        layout = self.layout
        layout.menu("OBJECT_MT_custom_modifier_add_modify", text="修改 (Edit/Modify)", icon='MOD_DATA_TRANSFER')
        layout.menu("OBJECT_MT_custom_modifier_add_generate", text="生成 (Generate)", icon='MOD_BEVEL')
        layout.menu("OBJECT_MT_custom_modifier_add_deform", text="变形 (Deform)", icon='MOD_SIMPLEDEFORM')
        layout.menu("OBJECT_MT_custom_modifier_add_physics", text="物理 (Physics)", icon='MOD_PHYSICS')


class OBJECT_MT_CustomModifierAdd_Modify(Menu):
    bl_idname = "OBJECT_MT_custom_modifier_add_modify"
    bl_label = "修改 (Modify)"

    def draw(self, context):
        layout = self.layout
        items = [
            ("DATA_TRANSFER", "数据传递 (Data Transfer)", 'MOD_DATA_TRANSFER'),
            ("NORMAL_EDIT", "法向编辑 (Normal Edit)", 'MOD_NORMALEDIT'),
            ("WEIGHTED_NORMAL", "加权法向 (Weighted Normal)", 'MOD_NORMALEDIT'),
            ("UV_PROJECT", "UV 投射 (UV Project)", 'MOD_UVPROJECT'),
            ("UV_WARP", "UV 扭曲 (UV Warp)", 'MOD_UVPROJECT'),
            ("VERTEX_WEIGHT_EDIT", "顶点权重编辑 (Vertex Weight Edit)", 'MOD_VERTEX_WEIGHT'),
            ("VERTEX_WEIGHT_MIX", "顶点权重混合 (Vertex Weight Mix)", 'MOD_VERTEX_WEIGHT'),
            ("VERTEX_WEIGHT_PROXIMITY", "顶点权重邻近 (Vertex Weight Proximity)", 'MOD_VERTEX_WEIGHT'),
        ]
        for type_id, name, icon in items:
            op = layout.operator("object.add_custom_modifier", text=name, icon=icon)
            op.modifier_type = type_id


class OBJECT_MT_CustomModifierAdd_Generate(Menu):
    bl_idname = "OBJECT_MT_custom_modifier_add_generate"
    bl_label = "生成 (Generate)"

    def draw(self, context):
        layout = self.layout
        items = [
            ("ARRAY", "阵列 (Array)", 'MOD_ARRAY'),
            ("BEVEL", "倒角 (Bevel)", 'MOD_BEVEL'),
            ("BOOLEAN", "布尔 (Boolean)", 'MOD_BOOLEAN'),
            ("BUILD", "建形 (Build)", 'MOD_BUILD'),
            ("DECIMATE", "精简/减面 (Decimate)", 'MOD_DECIM'),
            ("EDGE_SPLIT", "边缘分拆 (Edge Split)", 'MOD_EDGESPLIT'),
            ("NODES", "几何节点 (Geometry Nodes)", 'GEOMETRY_NODES'),
            ("MASK", "遮罩 (Mask)", 'MOD_MASK'),
            ("MIRROR", "镜像 (Mirror)", 'MOD_MIRROR'),
            ("MULTIRES", "多级分辨率 (Multiresolution)", 'MOD_MULTIRES'),
            ("REMESH", "重构网格 (Remesh)", 'MOD_REMESH'),
            ("SCREW", "螺旋 (Screw)", 'MOD_SCREW'),
            ("SKIN", "蒙皮/皮肤 (Skin)", 'MOD_SKIN'),
            ("SOLIDIFY", "实体化 (Solidify)", 'MOD_SOLIDIFY'),
            ("SUBSURF", "细分曲面 (Subdivision Surface)", 'MOD_SUBSURF'),
            ("TRIANGULATE", "三角化 (Triangulate)", 'MOD_TRIANGULATE'),
            ("WELD", "焊接 (Weld)", 'AUTOMERGE_OFF'),
            ("WIREFRAME", "线框 (Wireframe)", 'MOD_WIREFRAME'),
        ]
        for type_id, name, icon in items:
            op = layout.operator("object.add_custom_modifier", text=name, icon=icon)
            op.modifier_type = type_id


class OBJECT_MT_CustomModifierAdd_Deform(Menu):
    bl_idname = "OBJECT_MT_custom_modifier_add_deform"
    bl_label = "变形 (Deform)"

    def draw(self, context):
        layout = self.layout
        items = [
            ("ARMATURE", "骨架 (Armature)", 'MOD_ARMATURE'),
            ("CAST", "铸型 (Cast)", 'MOD_CAST'),
            ("CURVE", "曲线 (Curve)", 'MOD_CURVE'),
            ("DISPLACE", "置换 (Displace)", 'MOD_DISPLACE'),
            ("HOOK", "钩挂 (Hook)", 'HOOK'),
            ("LAPLACIANDEFORM", "拉普拉斯变形 (Laplacian Deform)", 'MOD_MESHDEFORM'),
            ("LAPLACIANSMOOTH", "拉普拉斯平滑 (Laplacian Smooth)", 'MOD_SMOOTH'),
            ("LATTICE", "晶格 (Lattice)", 'MOD_LATTICE'),
            ("MESH_DEFORM", "网格变形 (Mesh Deform)", 'MOD_MESHDEFORM'),
            ("SHRINKWRAP", "收缩包裹 (Shrinkwrap)", 'MOD_SHRINKWRAP'),
            ("SIMPLE_DEFORM", "简易形变 (Simple Deform)", 'MOD_SIMPLEDEFORM'),
            ("SMOOTH", "平滑 (Smooth)", 'MOD_SMOOTH'),
            ("CORRECTIVE_SMOOTH", "平滑修正 (Corrective Smooth)", 'MOD_SMOOTH'),
            ("SURFACE_DEFORM", "表面变形 (Surface Deform)", 'MOD_MESHDEFORM'),
            ("WARP", "弯折/扭曲 (Warp)", 'MOD_WARP'),
            ("WAVE", "波浪 (Wave)", 'MOD_WAVE'),
        ]
        for type_id, name, icon in items:
            op = layout.operator("object.add_custom_modifier", text=name, icon=icon)
            op.modifier_type = type_id


class OBJECT_MT_CustomModifierAdd_Physics(Menu):
    bl_idname = "OBJECT_MT_custom_modifier_add_physics"
    bl_label = "物理 (Physics)"

    def draw(self, context):
        layout = self.layout
        items = [
            ("CLOTH", "布料 (Cloth)", 'MOD_CLOTH'),
            ("COLLISION", "碰撞 (Collision)", 'MOD_PHYSICS'),
            ("DYNAMIC_PAINT", "动态画笔 (Dynamic Paint)", 'MOD_DYNAMICPAINT'),
            ("FLUID", "流体 (Fluid)", 'MOD_FLUID'),
            ("OCEAN", "海洋 (Ocean)", 'MOD_OCEAN'),
            ("PARTICLE_SYSTEM", "粒子系统 (Particle System)", 'MOD_PARTICLES'),
            ("SOFT_BODY", "柔体 (Soft Body)", 'MOD_SOFT'),
        ]
        for type_id, name, icon in items:
            op = layout.operator("object.add_custom_modifier", text=name, icon=icon)
            op.modifier_type = type_id


# =========================================================================
# 原生修改器参数排版（100% 紧凑对齐 Blender 官方 5.2 原生）
# =========================================================================

def draw_modifier_body(layout, ob, md):
    """根据修改器类型以 Blender 官方原生布局排版参数"""
    mtype = md.type
    layout.use_property_split = True
    layout.use_property_decorate = False

    if mtype == 'NORMAL_EDIT':
        draw_normal_edit_layout(layout, ob, md)
    elif mtype == 'BOOLEAN':
        draw_boolean_layout(layout, ob, md)
    elif mtype == 'MIRROR':
        draw_mirror_layout(layout, ob, md)
    elif mtype == 'ARRAY':
        draw_array_layout(layout, ob, md)
    elif mtype == 'BEVEL':
        draw_bevel_layout(layout, ob, md)
    elif mtype == 'SUBSURF':
        draw_subsurf_layout(layout, ob, md)
    elif mtype == 'SOLIDIFY':
        draw_solidify_layout(layout, ob, md)
    elif mtype == 'DECIMATE':
        draw_decimate_layout(layout, ob, md)
    elif mtype == 'WEIGHTED_NORMAL':
        draw_weighted_normal_layout(layout, ob, md)
    elif mtype == 'WELD':
        draw_weld_layout(layout, ob, md)
    elif mtype == 'REMESH':
        draw_remesh_layout(layout, ob, md)
    elif mtype == 'SIMPLE_DEFORM':
        draw_simple_deform_layout(layout, ob, md)
    elif mtype == 'EDGE_SPLIT':
        draw_edge_split_layout(layout, ob, md)
    elif mtype == 'TRIANGULATE':
        draw_triangulate_layout(layout, ob, md)
    elif mtype == 'WIREFRAME':
        draw_wireframe_layout(layout, ob, md)
    elif mtype == 'SHRINKWRAP':
        draw_shrinkwrap_layout(layout, ob, md)
    elif mtype == 'DISPLACE':
        draw_displace_layout(layout, ob, md)
    elif mtype == 'SMOOTH':
        draw_smooth_layout(layout, ob, md)
    elif mtype == 'SCREW':
        draw_screw_layout(layout, ob, md)
    elif mtype == 'CURVE':
        draw_curve_layout(layout, ob, md)
    elif mtype == 'LATTICE':
        draw_lattice_layout(layout, ob, md)
    elif mtype == 'CAST':
        draw_cast_layout(layout, ob, md)
    elif mtype == 'NODES':
        draw_nodes_layout(layout, ob, md)
    else:
        dp = modifier_layouts.DATA_PT_modifiers(bpy.context)
        func = getattr(dp, mtype, None)
        if func:
            try:
                func(layout, ob, md)
                return
            except Exception:
                pass
        draw_generic_layout(layout, ob, md)


# --- 几何节点修改器 (Geometry Nodes / 按角度平滑) ---
def draw_nodes_layout(layout, ob, md):
    """绘制几何节点修改器（如 按角度平滑 / Smooth by Angle 等），100% 对齐官方原生布局"""
    if not md.node_group:
        layout.template_ID(md, "node_group", new="node.new_geometry_node_group_assign")
        return

    ng = md.node_group
    layout.template_ID(md, "node_group", new="node.new_geometry_node_group_assign")
    layout.separator(factor=0.5)

    # 绘制节点组输入参数
    if hasattr(md, "properties") and hasattr(md.properties, "inputs"):
        inputs_obj = md.properties.inputs
        if hasattr(ng, "interface") and hasattr(ng.interface, "items_tree"):
            for item in ng.interface.items_tree:
                if getattr(item, "item_type", None) == 'SOCKET' and getattr(item, "in_out", None) == 'INPUT':
                    if getattr(item, "socket_type", None) == 'NodeSocketGeometry':
                        continue
                    ident = getattr(item, "identifier", None)
                    if not ident:
                        continue
                    inp = getattr(inputs_obj, ident, None)
                    if not inp:
                        continue

                    row = layout.row(align=True)
                    if hasattr(inp, "value"):
                        row.prop(inp, "value", text=item.name)
                    elif hasattr(inp, "default_value"):
                        row.prop(inp, "default_value", text=item.name)


# --- 法线编辑 (Normal Edit) ---
def draw_normal_edit_layout(layout, ob, md):
    layout.row().prop(md, "mode", expand=True)
    layout.prop(md, "target", text="目标")
    layout.prop(md, "use_direction_parallel", text="平行法向")

    header, panel = layout.panel("normal_edit_mix", default_closed=False)
    header.label(text="混合")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        panel.prop(md, "mix_mode", text="混合模式")
        panel.prop(md, "mix_factor", text="混合系数")

        row_v = panel.row(heading="顶点组", align=True)
        row_v.prop_search(md, "vertex_group", ob, "vertex_groups", text="")
        sub_v = row_v.row(align=True)
        sub_v.active = bool(md.vertex_group)
        sub_v.prop(md, "invert_vertex_group", text="", icon='ARROW_LEFTRIGHT')

        panel.prop(md, "mix_limit", text="最大角度")

    header, panel = layout.panel("normal_edit_offset", default_closed=True)
    header.label(text="偏移")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        panel.prop(md, "offset", index=0, text="偏移 X")
        panel.prop(md, "offset", index=1, text="Y")
        panel.prop(md, "offset", index=2, text="Z")


# --- 布尔 (Boolean) ---
def draw_boolean_layout(layout, ob, md):
    layout.row().prop(md, "operation", expand=True)
    layout.prop(md, "operand_type", text="运算对象类型")
    if md.operand_type == 'OBJECT':
        layout.prop(md, "object", text="物体")
    else:
        layout.prop(md, "collection", text="集合")

    row_s = layout.row(heading="解算器", align=True)
    row_s.prop(md, "solver", expand=True)

    header, panel = layout.panel("boolean_solver_options", default_closed=True)
    header.label(text="解算器选项")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        if hasattr(md, "double_threshold"):
            panel.prop(md, "double_threshold", text="重合阈值")
        if hasattr(md, "use_self"):
            panel.prop(md, "use_self", text="自相交")
        if hasattr(md, "use_hole_tolerant"):
            panel.prop(md, "use_hole_tolerant", text="容孔")


# --- 镜像 (Mirror) ---
def draw_mirror_layout(layout, ob, md):
    row = layout.row(heading="轴向", align=True)
    row.prop(md, "use_axis", index=0, text="X", toggle=True)
    row.prop(md, "use_axis", index=1, text="Y", toggle=True)
    row.prop(md, "use_axis", index=2, text="Z", toggle=True)

    row_b = layout.row(heading="切分", align=True)
    row_b.prop(md, "use_bisect_axis", index=0, text="X", toggle=True)
    row_b.prop(md, "use_bisect_axis", index=1, text="Y", toggle=True)
    row_b.prop(md, "use_bisect_axis", index=2, text="Z", toggle=True)

    row_f = layout.row(heading="翻转", align=True)
    row_f.prop(md, "use_bisect_flip_axis", index=0, text="X", toggle=True)
    row_f.prop(md, "use_bisect_flip_axis", index=1, text="Y", toggle=True)
    row_f.prop(md, "use_bisect_flip_axis", index=2, text="Z", toggle=True)

    layout.prop(md, "mirror_object", text="镜像物体")
    layout.prop(md, "use_clip", text="范围限制")

    row_m = layout.row(heading="合并", align=True)
    row_m.prop(md, "use_mirror_merge", text="")
    sub_m = row_m.row(align=True)
    sub_m.active = md.use_mirror_merge
    sub_m.prop(md, "merge_threshold", text="")

    layout.prop(md, "bisect_threshold", text="切分距离")

    # 数据子面板（可折叠）
    header, panel = layout.panel("mirror_data", default_closed=False)
    header.label(text="数据")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        col = panel.column()
        row_u = col.row(heading="镜像 U", align=True)
        row_u.prop(md, "use_mirror_u", text="")
        sub_u = row_u.row(align=True)
        sub_u.active = md.use_mirror_u
        sub_u.prop(md, "mirror_offset_u", text="")

        row_v = col.row(heading="V", align=True)
        row_v.prop(md, "use_mirror_v", text="")
        sub_v = row_v.row(align=True)
        sub_v.active = md.use_mirror_v
        sub_v.prop(md, "mirror_offset_v", text="")

        col.prop(md, "offset_u", text="偏移 U")

        col.prop(md, "offset_v", text="V")

        col.prop(md, "use_mirror_vertex_groups", text="顶点组")

        col.prop(md, "use_mirror_udim", text="翻转UDIM")


# --- 阵列 (Array) ---
def draw_array_layout(layout, ob, md):
    layout.prop(md, "fit_type", text="适配类型")
    if md.fit_type == 'FIXED_COUNT':
        layout.prop(md, "count", text="数量")
    elif md.fit_type == 'FIT_LENGTH':
        layout.prop(md, "fit_length", text="长度")
    elif md.fit_type == 'FIT_CURVE':
        layout.prop(md, "curve", text="曲线")

    # 相对偏移（原生可折叠子面板）
    header, panel = layout.panel("array_relative_offset", default_closed=not md.use_relative_offset)
    header.prop(md, "use_relative_offset", text="相对偏移")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        col = panel.column(align=True)
        col.active = md.use_relative_offset
        col.prop(md, "relative_offset_displace", index=0, text="系数 X")
        col.prop(md, "relative_offset_displace", index=1, text="Y")
        col.prop(md, "relative_offset_displace", index=2, text="Z")

    # 恒定偏移（原生可折叠子面板）
    header, panel = layout.panel("array_constant_offset", default_closed=not md.use_constant_offset)
    header.prop(md, "use_constant_offset", text="恒定偏移")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        col = panel.column(align=True)
        col.active = md.use_constant_offset
        col.prop(md, "constant_offset_displace", index=0, text="距离 X")
        col.prop(md, "constant_offset_displace", index=1, text="Y")
        col.prop(md, "constant_offset_displace", index=2, text="Z")

    # 物体偏移（原生可折叠子面板）
    header, panel = layout.panel("array_object_offset", default_closed=not md.use_object_offset)
    header.prop(md, "use_object_offset", text="物体偏移")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        col = panel.column()
        col.active = md.use_object_offset
        col.prop(md, "offset_object", text="目标物体")

    # 合并（原生可折叠子面板）
    header, panel = layout.panel("array_merge", default_closed=not md.use_merge_vertices)
    header.prop(md, "use_merge_vertices", text="合并")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        col = panel.column()
        col.active = md.use_merge_vertices
        col.prop(md, "merge_threshold", text="合并距离")
        if hasattr(md, "use_merge_vertices_cap"):
            col.prop(md, "use_merge_vertices_cap", text="首末相连")

    # UV（原生可折叠子面板）
    header, panel = layout.panel("array_uv", default_closed=True)
    header.label(text="UV")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        panel.prop(md, "offset_u", text="偏移 U")
        panel.prop(md, "offset_v", text="V")

    # 端盖样式（原生可折叠子面板）
    header, panel = layout.panel("array_caps", default_closed=True)
    header.label(text="端盖样式")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        panel.prop(md, "start_cap", text="起始端")
        panel.prop(md, "end_cap", text="末端")


# --- 倒角 (Bevel) ---
def draw_bevel_layout(layout, ob, md):
    layout.row().prop(md, "affect", expand=True)

    layout.prop(md, "offset_type", text="宽度类型")
    if md.offset_type == 'PERCENT':
        layout.prop(md, "width_pct", text="宽度百分比")
    else:
        layout.prop(md, "width", text="数量")

    layout.prop(md, "segments", text="分段")
    layout.prop(md, "limit_method", text="限定方式")
    if md.limit_method == 'ANGLE':
        layout.prop(md, "angle_limit", text="角度")
    elif md.limit_method == 'WEIGHT':
        if hasattr(md, "edge_weight"):
            layout.prop(md, "edge_weight", text="边权重")
        elif hasattr(md, "vertex_weight"):
            layout.prop(md, "vertex_weight", text="顶点权重")
    elif md.limit_method == 'VGROUP':
        row_v = layout.row(heading="顶点组", align=True)
        row_v.prop_search(md, "vertex_group", ob, "vertex_groups", text="")
        sub_v = row_v.row(align=True)
        sub_v.active = bool(md.vertex_group)
        sub_v.prop(md, "invert_vertex_group", text="", icon='ARROW_LEFTRIGHT')

    header, panel = layout.panel("bevel_profile", default_closed=False)
    header.label(text="轮廓")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        panel.prop(md, "profile_type", text="轮廓类型")
        if md.profile_type == 'SUPERELLIPSE':
            panel.prop(md, "profile", text="轮廓形状")
        panel.prop(md, "material", text="材质编号")

    header, panel = layout.panel("bevel_geometry", default_closed=True)
    header.label(text="几何数据")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        panel.prop(md, "use_clamp_overlap", text="钳制重叠")
        panel.prop(md, "loop_slide", text="环切线滑移")
        panel.prop(md, "mark_seam", text="标记为缝合边")
        panel.prop(md, "mark_sharp", text="标记锐边")
        panel.prop(md, "miter_outer", text="外斜接")
        panel.prop(md, "miter_inner", text="内斜接")

    header, panel = layout.panel("bevel_shading", default_closed=True)
    header.label(text="着色方式")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        panel.prop(md, "harden_normals", text="硬化法向")
        panel.prop(md, "face_strength_mode", text="面强度")


# --- 细分曲面 (Subsurf) ---
def draw_subsurf_layout(layout, ob, md):
    layout.row().prop(md, "subdivision_type", expand=True)
    layout.prop(md, "levels", text="视图级别")
    layout.prop(md, "render_levels", text="渲染级别")
    layout.prop(md, "quality", text="质量")

    header, panel = layout.panel("subsurf_advanced", default_closed=True)
    header.label(text="高级")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        panel.prop(md, "uv_smooth", text="UV 平滑")
        panel.prop(md, "boundary_smooth", text="边界平滑")
        panel.prop(md, "use_creases", text="使用折痕")
        panel.prop(md, "use_custom_normals", text="使用自定义法线")


# --- 实体化 (Solidify) ---
def draw_solidify_layout(layout, ob, md):
    layout.row().prop(md, "solidify_mode", expand=True)
    layout.prop(md, "thickness", text="厚度")
    layout.prop(md, "offset", text="偏移量")
    layout.prop(md, "use_even_offset", text="均一厚度")
    layout.prop(md, "use_rim", text="填充边缘")
    layout.prop(md, "use_rim_only", text="仅边缘")
    layout.prop(md, "use_flip_normals", text="翻转法向")

    header, panel = layout.panel("solidify_materials", default_closed=True)
    header.label(text="材质与顶点组")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        panel.prop(md, "material_offset", text="材质偏移")
        panel.prop(md, "material_offset_rim", text="边缘材质")
        row_v = panel.row(heading="顶点组", align=True)
        row_v.prop_search(md, "vertex_group", ob, "vertex_groups", text="")
        sub_v = row_v.row(align=True)
        sub_v.active = bool(md.vertex_group)
        sub_v.prop(md, "invert_vertex_group", text="", icon='ARROW_LEFTRIGHT')


# --- 减面/精简 (Decimate) ---
def draw_decimate_layout(layout, ob, md):
    layout.row().prop(md, "decimate_type", expand=True)
    if md.decimate_type == 'COLLAPSE':
        layout.prop(md, "ratio", text="比率")
        layout.prop(md, "use_symmetry", text="对称")
        layout.prop(md, "use_collapse_triangulate", text="三角化")
    elif md.decimate_type == 'UNSUBDIV':
        layout.prop(md, "iterations", text="迭代次数")
    elif md.decimate_type == 'DISSOLVE':
        layout.prop(md, "angle_limit", text="角度限制")
        layout.prop(md, "delimit", text="限定")

    row_v = layout.row(heading="顶点组", align=True)
    row_v.prop_search(md, "vertex_group", ob, "vertex_groups", text="")
    sub_v = row_v.row(align=True)
    sub_v.active = bool(md.vertex_group)
    sub_v.prop(md, "invert_vertex_group", text="", icon='ARROW_LEFTRIGHT')


# --- 加权法向 (Weighted Normal) ---
def draw_weighted_normal_layout(layout, ob, md):
    layout.prop(md, "weight", text="权重")
    layout.prop(md, "mode", text="模式")
    layout.prop(md, "thresh", text="阈值")
    layout.prop(md, "keep_sharp", text="保持锐边")
    layout.prop(md, "use_face_influence", text="面影响")

    row_v = layout.row(heading="顶点组", align=True)
    row_v.prop_search(md, "vertex_group", ob, "vertex_groups", text="")
    sub_v = row_v.row(align=True)
    sub_v.active = bool(md.vertex_group)
    sub_v.prop(md, "invert_vertex_group", text="", icon='ARROW_LEFTRIGHT')


# --- 焊接 (Weld) ---
def draw_weld_layout(layout, ob, md):
    layout.prop(md, "mode", text="模式")
    layout.prop(md, "merge_threshold", text="距离阈值")
    row_v = layout.row(heading="顶点组", align=True)
    row_v.prop_search(md, "vertex_group", ob, "vertex_groups", text="")
    sub_v = row_v.row(align=True)
    sub_v.active = bool(md.vertex_group)
    sub_v.prop(md, "invert_vertex_group", text="", icon='ARROW_LEFTRIGHT')


# --- 重构网格 (Remesh) ---
def draw_remesh_layout(layout, ob, md):
    layout.row().prop(md, "mode", expand=True)
    if md.mode == 'VOXEL':
        layout.prop(md, "voxel_size", text="体素大小")
        layout.prop(md, "adaptivity", text="自适应")
        layout.prop(md, "use_smooth_shade", text="平滑着色")
    elif md.mode == 'SMOOTH':
        layout.prop(md, "octree_depth", text="八叉树深度")
        layout.prop(md, "scale", text="缩放")
        layout.prop(md, "use_smooth_shade", text="平滑着色")
    elif md.mode == 'BLOCKS':
        layout.prop(md, "octree_depth", text="八叉树深度")
        layout.prop(md, "scale", text="缩放")


# --- 简易形变 (Simple Deform) ---
def draw_simple_deform_layout(layout, ob, md):
    layout.row().prop(md, "deform_method", expand=True)
    if md.deform_method in {'TWIST', 'BEND'}:
        layout.prop(md, "angle", text="角度")
    else:
        layout.prop(md, "factor", text="系数")
    layout.prop(md, "origin", text="原点")
    row_a = layout.row(heading="形变轴", align=True)
    row_a.prop(md, "deform_axis", expand=True)
    layout.prop(md, "limits", text="限制范围")
    row_v = layout.row(heading="顶点组", align=True)
    row_v.prop_search(md, "vertex_group", ob, "vertex_groups", text="")
    sub_v = row_v.row(align=True)
    sub_v.active = bool(md.vertex_group)
    sub_v.prop(md, "invert_vertex_group", text="", icon='ARROW_LEFTRIGHT')


# --- 边缘分拆 (Edge Split) ---
def draw_edge_split_layout(layout, ob, md):
    layout.prop(md, "use_edge_angle", text="从边角度")
    if md.use_edge_angle:
        layout.prop(md, "split_angle", text="分拆角度")
    layout.prop(md, "use_edge_sharp", text="从锐边")


# --- 三角化 (Triangulate) ---
def draw_triangulate_layout(layout, ob, md):
    layout.prop(md, "quad_method", text="四边形方法")
    layout.prop(md, "ngon_method", text="多边形方法")
    layout.prop(md, "min_vertices", text="最小顶点数")
    layout.prop(md, "keep_custom_normals", text="保留自定义法线")


# --- 线框 (Wireframe) ---
def draw_wireframe_layout(layout, ob, md):
    layout.prop(md, "thickness", text="厚度")
    layout.prop(md, "offset", text="偏移")
    layout.prop(md, "use_boundary", text="边界")
    layout.prop(md, "use_replace", text="替换原网格")
    layout.prop(md, "use_even_offset", text="均一厚度")
    layout.prop(md, "material_offset", text="材质编号")


# --- 收缩包裹 (Shrinkwrap) ---
def draw_shrinkwrap_layout(layout, ob, md):
    layout.prop(md, "wrap_method", text="包裹方法")
    layout.prop(md, "wrap_mode", text="包裹模式")
    layout.prop(md, "target", text="目标")
    layout.prop(md, "offset", text="偏移量")
    row_v = layout.row(heading="顶点组", align=True)
    row_v.prop_search(md, "vertex_group", ob, "vertex_groups", text="")
    sub_v = row_v.row(align=True)
    sub_v.active = bool(md.vertex_group)
    sub_v.prop(md, "invert_vertex_group", text="", icon='ARROW_LEFTRIGHT')


# --- 置换 (Displace) ---
def draw_displace_layout(layout, ob, md):
    layout.prop(md, "texture", text="纹理")
    layout.prop(md, "direction", text="方向")
    layout.prop(md, "texture_coords", text="纹理坐标")
    layout.prop(md, "strength", text="强度")
    layout.prop(md, "mid_level", text="中间电平")
    row_v = layout.row(heading="顶点组", align=True)
    row_v.prop_search(md, "vertex_group", ob, "vertex_groups", text="")
    sub_v = row_v.row(align=True)
    sub_v.active = bool(md.vertex_group)
    sub_v.prop(md, "invert_vertex_group", text="", icon='ARROW_LEFTRIGHT')


# --- 平滑 (Smooth) ---
def draw_smooth_layout(layout, ob, md):
    row = layout.row(heading="轴向", align=True)
    row.prop(md, "use_x", text="X", toggle=True)
    row.prop(md, "use_y", text="Y", toggle=True)
    row.prop(md, "use_z", text="Z", toggle=True)
    layout.prop(md, "factor", text="系数")
    layout.prop(md, "iterations", text="重复")
    row_v = layout.row(heading="顶点组", align=True)
    row_v.prop_search(md, "vertex_group", ob, "vertex_groups", text="")
    sub_v = row_v.row(align=True)
    sub_v.active = bool(md.vertex_group)
    sub_v.prop(md, "invert_vertex_group", text="", icon='ARROW_LEFTRIGHT')


# --- 螺旋 (Screw) ---
def draw_screw_layout(layout, ob, md):
    row_a = layout.row(heading="轴向", align=True)
    row_a.prop(md, "axis", expand=True)
    layout.prop(md, "screw_offset", text="螺旋")
    layout.prop(md, "iterations", text="圈数")
    layout.prop(md, "angle", text="角度")
    layout.prop(md, "steps", text="轴向步数")
    layout.prop(md, "render_steps", text="渲染步数")
    layout.prop(md, "use_smooth_shade", text="平滑着色")
    layout.prop(md, "use_merge_vertices", text="合并顶点")


# --- 曲线 (Curve) ---
def draw_curve_layout(layout, ob, md):
    layout.prop(md, "object", text="曲线物体")
    row_a = layout.row(heading="形变轴", align=True)
    row_a.prop(md, "deform_axis", expand=True)
    row_v = layout.row(heading="顶点组", align=True)
    row_v.prop_search(md, "vertex_group", ob, "vertex_groups", text="")
    sub_v = row_v.row(align=True)
    sub_v.active = bool(md.vertex_group)
    sub_v.prop(md, "invert_vertex_group", text="", icon='ARROW_LEFTRIGHT')


# --- 晶格 (Lattice) ---
def draw_lattice_layout(layout, ob, md):
    layout.prop(md, "object", text="晶格物体")
    layout.prop(md, "strength", text="强度")
    row_v = layout.row(heading="顶点组", align=True)
    row_v.prop_search(md, "vertex_group", ob, "vertex_groups", text="")
    sub_v = row_v.row(align=True)
    sub_v.active = bool(md.vertex_group)
    sub_v.prop(md, "invert_vertex_group", text="", icon='ARROW_LEFTRIGHT')


# --- 铸型 (Cast) ---
def draw_cast_layout(layout, ob, md):
    layout.row().prop(md, "cast_type", expand=True)
    layout.prop(md, "factor", text="系数")
    layout.prop(md, "radius", text="半径")
    layout.prop(md, "size", text="大小")
    layout.prop(md, "object", text="控制物体")


# --- 通用排版回退 ---
def draw_generic_layout(layout, ob, md):
    ignored = {
        "name", "type", "show_expanded", "show_viewport", "show_render",
        "show_in_editmode", "show_on_cage", "is_active", "use_pin_to_last",
        "use_apply_on_spline", "is_override_data", "execution_time"
    }
    for prop in md.bl_rna.properties:
        if not prop.is_readonly and prop.identifier not in ignored:
            layout.prop(md, prop.identifier)


# =========================================================================
# 修改器主面板渲染 (去重聚合显示所有选中物体的修改器)
# =========================================================================

def draw_modifiers_section(layout, context):
    """绘制原生风格的修改器堆栈卡片列表（显示所有选中物体的修改器）"""
    selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']
    active_obj = context.active_object if context.active_object and context.active_object.type == 'MESH' else None

    # 如果没有活动网格物体，但在多选中存在网格物体，选取第一个网格物体
    if not active_obj and selected_objs:
        active_obj = selected_objs[0]

    # 顶部标题与添加按钮
    header_row = layout.row(align=True)
    header_row.menu("OBJECT_MT_custom_modifier_add", text="新建修改器", icon='ADD')

    if not selected_objs and not active_obj:
        layout.label(text="请选择网格物体", icon='INFO')
        return

    if not selected_objs and active_obj:
        selected_objs = [active_obj]

    selected_count = len(selected_objs)

    # 1. 聚合所有选中物体的修改器（去重有序收集）
    unique_mod_dict = {}  # key: mod_name -> { 'type': ..., 'instance': ..., 'owner': ..., 'count': ... }
    
    # 优先保证 active_obj 的修改器排在前面
    scan_order = [active_obj] + [o for o in selected_objs if o != active_obj]
    for obj in scan_order:
        if not obj:
            continue
        for mod in obj.modifiers:
            if mod.name not in unique_mod_dict:
                # 统计拥有该修改器的物体数量
                has_count = sum(1 for o in selected_objs if mod.name in o.modifiers)
                unique_mod_dict[mod.name] = {
                    'name': mod.name,
                    'type': mod.type,
                    'instance': mod,
                    'owner': obj,
                    'count': has_count
                }

    if not unique_mod_dict:
        box = layout.box()
        box.label(text="当前选中的物体无修改器", icon='INFO')
        return

    main_col = layout.column(align=True)

    # 2. 遍历聚合的修改器卡片进行绘制
    for mod_info in unique_mod_dict.values():
        mod_name = mod_info['name']
        mod_type = mod_info['type']
        mod = mod_info['instance']
        owner_obj = mod_info['owner']
        has_count = mod_info['count']

        # 使用原生 panel 容器，自动具备官方折叠三角图标旋转及拖拽连续批量折叠功能
        header, panel = main_col.panel(f"umm_mod_{mod_name}", default_closed=not mod.show_expanded)

        # 1. 点击修改器图标：在场景中选择所有拥有此修改器的物体
        sel_op = header.operator(
            "object.select_by_modifier",
            text="",
            icon=get_modifier_icon(mod_type),
            emboss=False
        )
        sel_op.modifier_name = mod.name
        sel_op.modifier_type = mod_type

        # 2. 修改器名称（可直接修改）
        header.prop(mod, "name", text="")

        # 3. 多物体覆盖统计 (如 2/4 或 4/4)
        if selected_count > 1:
            header.label(text=f"({has_count}/{selected_count})")

        # 4. 右侧控制图标组（完全对齐官方原生蓝底高亮与标准图标）
        right_group = header.row(align=True)
        right_group.alignment = 'RIGHT'

        if hasattr(mod, "show_on_cage"):
            right_group.prop(mod, "show_on_cage", text="")

        if hasattr(mod, "show_in_editmode"):
            right_group.prop(mod, "show_in_editmode", text="")

        right_group.prop(mod, "show_viewport", text="")
        right_group.prop(mod, "show_render", text="")

        # 下拉/应用操作
        apply_op = right_group.operator("object.apply_modifier_to_all", text="", icon='CHECKMARK', emboss=False)
        apply_op.modifier_name = mod.name

        # 删除按钮
        remove_op = right_group.operator("object.remove_modifier_from_selected", text="", icon='X', emboss=False)
        remove_op.modifier_name = mod.name

        # 卡片主体 (Body) - 仅在展开时显示原生参数
        if panel:
            panel.use_property_split = True
            panel.use_property_decorate = False
            draw_modifier_body(panel, owner_obj, mod)


# =========================================================================
# 注册与注销
# =========================================================================

classes = (
    MODIFIER_OT_SelectByModifier,
    MODIFIER_OT_ToggleExpanded,
    MODIFIER_OT_AddCustom,
    MODIFIER_OT_ApplyToAll,
    MODIFIER_OT_RemoveFromSelected,
    OBJECT_MT_CustomModifierAdd,
    OBJECT_MT_CustomModifierAdd_Modify,
    OBJECT_MT_CustomModifierAdd_Generate,
    OBJECT_MT_CustomModifierAdd_Deform,
    OBJECT_MT_CustomModifierAdd_Physics,
)

def register():
    bpy.types.Scene.um_auto_sync_modifiers = BoolProperty(
        name="自动同步修改器参数",
        description="修改活动物体修改器的某项参数时，自动同步该项参数到所有选中的物体",
        default=True
    )

    for cls in classes:
        bpy.utils.register_class(cls)

    if depsgraph_update_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(depsgraph_update_handler)

def unregister():
    if depsgraph_update_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(depsgraph_update_handler)

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    if hasattr(bpy.types.Scene, "um_auto_sync_modifiers"):
        del bpy.types.Scene.um_auto_sync_modifiers