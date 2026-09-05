import bpy
import bmesh
from bpy.types import Operator, Panel
from bpy.props import FloatProperty
from .modifier_manager import draw_modifiers_section

# 清除法线功能
class CUSTOM_OT_ClearNormals(Operator):
    """清除选中物体的自定义法线数据"""
    bl_idname = "custom.clear_normals"
    bl_label = "清除法线"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(obj.type == 'MESH' for obj in context.selected_objects)

    def execute(self, context):
        processed = 0
        original_active = context.active_object  # 记录原始活动物体
        
        # 遍历所有选中物体
        for obj in context.selected_objects:
            if obj.type == 'MESH':
                try:
                    # 强制进入物体模式
                    if context.mode != 'OBJECT':
                        bpy.ops.object.mode_set(mode='OBJECT')
                    
                    # 设置当前物体为活动对象
                    context.view_layer.objects.active = obj
                    
                    # 使用临时上下文覆盖
                    with context.temp_override(
                        selected_editable_objects=[obj],
                        object=obj,
                        active_object=obj
                    ):
                        # 执行法线清除操作
                        bpy.ops.mesh.customdata_custom_splitnormals_clear()
                        processed += 1
                        
                except Exception as e:
                    self.report({'WARNING'}, f"{obj.name} 无法清除法线: {str(e)}")
        
        # 恢复原始活动物体
        context.view_layer.objects.active = original_active
        self.report({'INFO'}, f"已处理 {processed} 个网格物体")
        return {'FINISHED'}

# 自动平滑功能
class OBJECT_OT_AutoSmooth(Operator):
    """应用自动平滑（兼容 Blender 4.x / 5.x 及旧版本）"""
    bl_idname = "object.auto_smooth_pro"
    bl_label = "自动平滑"
    bl_options = {'REGISTER', 'UNDO'}
    
    angle: FloatProperty(
        name="平滑角度",
        default=30,
        min=0,
        max=180
    )

    def execute(self, context):
        import math
        radians_value = math.radians(self.angle)
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not selected_objs and context.object and context.object.type == 'MESH':
            selected_objs = [context.object]

        if not selected_objs:
            self.report({'WARNING'}, "未选择网格物体")
            return {'CANCELLED'}

        original_active = context.view_layer.objects.active
        original_mode = context.mode
        if original_mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        processed = 0
        for obj in selected_objs:
            try:
                with context.temp_override(
                    active_object=obj,
                    selected_editable_objects=[obj],
                    selected_objects=[obj]
                ):
                    if hasattr(bpy.ops.object, 'shade_auto_smooth'):
                        bpy.ops.object.shade_auto_smooth(angle=radians_value)
                    else:
                        bpy.ops.object.shade_smooth()
                        if hasattr(obj.data, 'use_auto_smooth'):
                            obj.data.use_auto_smooth = True
                            obj.data.auto_smooth_angle = radians_value
                processed += 1
            except Exception as e:
                self.report({'WARNING'}, f"{obj.name} 处理失败: {str(e)}")
                continue

        if original_mode != 'OBJECT':
            bpy.ops.object.mode_set(mode=original_mode)
        if original_active:
            context.view_layer.objects.active = original_active

        context.view_layer.update()
        self.report({'INFO'}, f"成功应用 {int(self.angle)}° 自动平滑到 {processed} 个物体")
        return {'FINISHED'}

# 主面板
class VIEW3D_PT_UltimateManager(Panel):
    bl_label = "UV & 材质管理"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "UV Tools"
    bl_order = 0  # 确保面板显示在顶部

    def draw(self, context):
        layout = self.layout
        props = context.scene.um_props
        
        # 0. 常用建模与大纲视图工具栏 (拍平面、合并材质、填充孔洞、大纲视图)
        tools_row = layout.row(align=True)
        tools_row.operator("mesh.flatten_face_by_three_vertices", text="拍平面", icon='SNAP_FACE')
        tools_row.operator("object.ultra_material_combine", text="合并材质", icon='NODETREE')
        tools_row.operator("mesh.zhineng_fill_holes", text="填充孔洞", icon='SELECT_DIFFERENCE')

        outliner_state = getattr(context.scene, "outliner_state", None)
        if outliner_state and outliner_state.is_open:
            tools_row.operator("view3d.toggle_outliner", text="关闭大纲", icon='X')
        else:
            tools_row.operator("view3d.toggle_outliner", text="大纲视图", icon='OUTLINER')

        # 1. 动态自定义分屏与着色工具栏
        from .workspace import ensure_custom_layouts_loaded, get_editor_icon
        ensure_custom_layouts_loaded(context.scene)

        top_row = layout.row(align=True)
        active_id = getattr(context.window_manager, "um_active_layout_id", "")
        custom_layouts = getattr(context.scene, "um_custom_layouts", [])

        if len(custom_layouts) == 0:
            top_row.operator("workspace.add_custom_layout", text="添加分屏视图", icon='ADD')
        else:
            for item in custom_layouts:
                is_active = (active_id == item.id)
                icon_name = get_editor_icon(item.editor_type)
                btn = top_row.operator("workspace.toggle_custom_layout", text=item.name, icon=icon_name, depress=is_active)
                btn.item_id = item.id

        top_row.operator("custom.clear_normals", icon='NORMALS_FACE', text="清除法线")
        top_row.popover(panel="VIEW3D_PT_QuickShadingPopover", text="着色菜单", icon='SHADING_SOLID')
        top_row.prop(context.scene, "um_show_layout_settings", text="", icon='PREFERENCES' if not context.scene.um_show_layout_settings else 'DOWNARROW_HLT')

        # 2. 分屏视图管理配置展开面板
        if getattr(context.scene, "um_show_layout_settings", False):
            cfg_box = layout.box()
            cfg_hdr = cfg_box.row(align=True)
            cfg_hdr.label(text="自定义分屏视图管理器", icon='PREFERENCES')
            cfg_hdr.operator("workspace.add_custom_layout", text="添加新项", icon='ADD')

            if len(custom_layouts) == 0:
                cfg_box.label(text="暂未添加任何分屏视图，点击右上角「添加新项」开始配置", icon='INFO')
            else:
                for idx, item in enumerate(custom_layouts):
                    ibox = cfg_box.box()
                    r1 = ibox.row(align=True)
                    r1.prop(item, "name", text="名称")
                    r1.prop(item, "editor_type", text="")

                    r2 = ibox.row(align=True)
                    r2.prop(item, "direction", expand=True)
                    r2.prop(item, "ratio", text="占比", slider=True)
                    del_op = r2.operator("workspace.remove_custom_layout", text="", icon='TRASH')
                    del_op.index = idx

        # 自动平滑功能区
        smooth_box = layout.box()
        
        # 第一行：预设角度按钮
        angles_row = smooth_box.row(align=True)
        for angle in [30, 60, 90, 180]:
            op = angles_row.operator("object.auto_smooth_pro", text=str(angle))
            op.angle = angle
        
        obj = context.object      

    def draw_uv_section(self, layout, context, obj):
        if not obj or obj.type != 'MESH':
            layout.label(text="请选择网格物体", icon='ERROR')
            return

        grid = layout.grid_flow(columns=4, align=True)
        grid.operator("uv.add_layer_pro", text="新建", icon='ADD')
        grid.operator("uv.remove_layer_pro", text="删除", icon='REMOVE')
        grid.operator("uv.sync_layers_pro", text="同步", icon='UV_SYNC_SELECT')
        grid.operator("uv.select_max_layers_pro", text="最大层", icon='SELECT_EXTEND')

        layout.template_list(
            "UV_UL_LayersListPro",
            "",
            obj.data, "uv_layers",
            obj.data.uv_layers, "active_index",
            rows=3
        )

        self.draw_uv_stats(layout, context)

    def draw_uv_stats(self, layout, context):
        selected = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        stats_row = layout.row()
        box = stats_row.box()
        row = box.row(align=True)
        
        row.label(icon='UV')
        selected_max = max((len(obj.data.uv_layers) for obj in selected), default=0)
        selected_min = min((len(obj.data.uv_layers) for obj in selected), default=0) if selected else 0
        
        row.label(text=f"最大层: {selected_max}")
        row.separator()
        row.label(text=f"最小: {selected_min}")
        row.separator()
        row.label(text=f"选中: {len(selected)}")
        
        row.scale_x = 0.85
        row.alignment = 'CENTER'

    def draw_material_section(self, mat_box, context):
        props = context.scene.um_props
        
        # 获取同步状态
        from .material_manager import MATERIAL_OT_ToggleSyncRevert
        global_sync_state = getattr(MATERIAL_OT_ToggleSyncRevert, "global_sync_state", False)
        has_individual_synced = len(getattr(MATERIAL_OT_ToggleSyncRevert, "sync_states", {})) > 0
        
        # 1. 源材质与本地模板管理行
        src_row = mat_box.row(align=True)
        src_row.prop(context.scene, "um_source_mat_enum", text="")
        src_row.separator(factor=0.6)
        btn_sub = src_row.row(align=True)
        btn_sub.scale_x = 1.15
        btn_sub.menu("MATERIAL_MT_TemplateLibraryMenu", text="", icon='ASSET_MANAGER')
        btn_sub.operator("material.save_source_template", text="", icon='ADD')
        btn_sub.operator("material.delete_source_template", text="", icon='TRASH')

        # 2. 材质操作工具栏（应用源材质、新建材质、同步、恢复）
        tools_row = mat_box.row(align=True)
        tools_row.operator("material.apply_from_scene", text="应用", icon='FORWARD')
        tools_row.operator("material.create_new_pro", text="新建", icon='ADD')

        sync_op = tools_row.operator(
            "material.sync_shader_pro", 
            icon='FILE_REFRESH', 
            text="同步"
        )
        sync_op.target_material_name = ""
        sync_sub = tools_row.row(align=True)
        sync_sub.enabled = not (global_sync_state or has_individual_synced)
        
        tools_row.operator("material.revert_shader_pro", icon='LOOP_BACK', text="恢复")

        # 统计信息
        stats_row = mat_box.row(align=True)
        stats_row.label(text=f"场景材质总数: {len(bpy.data.materials)}", icon='WORLD_DATA')
        stats_row.separator()
        # 确保材质列表即时更新
        from .utils import update_material_list_handler
        update_material_list_handler(context.scene)

        # 材质列表
        mat_box.template_list(
            "MATERIAL_UL_CustomListPro",
            "",
            props, "material_collection",
            props, "material_list_index",
            rows=props.max_material_rows
        )
    
    def get_selected_materials(self, context):
        """获取当前选中的材质名称集合"""
        selected_mats = set()
        if context.mode == 'EDIT_MESH':
            # 编辑模式：收集选中面的材质
            obj = context.active_object
            if obj and obj.type == 'MESH':
                bm = bmesh.from_edit_mesh(obj.data)
                selected_faces = [f for f in bm.faces if f.select]
                for face in selected_faces:
                    if face.material_index < len(obj.material_slots):
                        mat = obj.material_slots[face.material_index].material
                        if mat:
                            selected_mats.add(mat.name)
        else:
            # 对象模式：取活动物体的活动材质
            obj = context.active_object
            if obj and obj.type == 'MESH' and obj.active_material:
                selected_mats.add(obj.active_material.name)
        return selected_mats        


# 修改器管理面板
class VIEW3D_PT_ModifierManager(Panel):
    bl_label = "修改器"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "UV Tools"
    bl_order = 1

    def draw(self, context):
        layout = self.layout
        draw_modifiers_section(layout, context)

# UV层管理面板
class VIEW3D_PT_UVLayerManager(Panel):
    bl_label = "UV层管理"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "UV Tools"  # 与主面板保持同一分类
    bl_order = 1  # 显示在材质管理面板下方

    def draw(self, context):
        layout = self.layout
        obj = context.object
        
        # 主容器
        main_box = layout.box()
        
        # 工具行添加复制粘贴按钮
        tool_row = main_box.row(align=True)
        tool_row.operator("uv.add_layer_pro", icon='ADD', text="新建")
        tool_row.operator("uv.remove_layer_pro", icon='REMOVE', text="删除")
        tool_row.operator("uv.sync_layers_pro", icon='UV_SYNC_SELECT', text="同步")
        tool_row.operator("uv.select_max_layers_pro", icon='SELECT_EXTEND', text="最大层")

        # UV层列表
        list_box = main_box.box()
        if obj and obj.type == 'MESH':
            list_box.template_list(
                "UV_UL_LayersListPro",
                "",
                obj.data, "uv_layers",
                obj.data.uv_layers, "active_index",
                rows=2
            )
            
            # 底部复制粘贴操作栏
            button_row = list_box.row(align=True)
            button_row.scale_x = 1.2  # 横向扩展按钮宽度

            # 左侧复制按钮
            left_col = button_row.column(align=True)
            left_col.operator(
                "uv.copy_active_layer_pro",
                icon='COPY_ID', 
                text="复制当前UV层"
            )
            
            # 右侧粘贴按钮
            right_col = button_row.column(align=True)
            right_col.operator(
                "uv.paste_to_active_layer_pro",
                icon='PASTEDOWN',
                text="粘贴到当前层"
            )
        else:
            list_box.label(text="请选择网格物体", icon='ERROR')

        # 新增统计信息区块 (仅检测选择的物体，不包括未选择的物体)
        stats_box = main_box.box()
        row = stats_box.row(align=True)
        
        selected_objs = [o for o in context.selected_objects if o.type == 'MESH']
        selected_max = max((len(o.data.uv_layers) for o in selected_objs), default=0)
        selected_min = min((len(o.data.uv_layers) for o in selected_objs), default=0) if selected_objs else 0
        
        row.label(icon='MOD_UVPROJECT')
        row.label(text=f"最大层: {selected_max}")
        row.separator()
        row.label(text=f"最小: {selected_min}")
        row.separator()
        row.label(text=f"选中: {len(selected_objs)}")


class IMAGE_PT_UVManager(Panel):
    bl_label = "UV层管理"
    bl_space_type = 'IMAGE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "UV Tools"
    
    def draw(self, context):
        VIEW3D_PT_UVLayerManager.draw(self, context)

# 材质管理面板
class VIEW3D_PT_MaterialManager(Panel):
    bl_label = "材质管理"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "UV Tools"  # 保持与主面板相同分类
    bl_order = 3  # 显示在主面板下方

    def draw_header(self, context):
        layout = self.layout
        layout.operator("material.reload_all_textures", text="", icon='FILE_REFRESH', emboss=False)

    def draw(self, context):
        layout = self.layout
        props = context.scene.um_props
        
        # 0. 菜单最顶上一键刷新全部材质贴图
        top_row = layout.row(align=True)
        top_row.scale_y = 1.15
        top_row.operator("material.reload_all_textures", text="刷新全部贴图", icon='FILE_REFRESH')

        # 主容器
        main_box = layout.box()
        
        # 1. 源材质与本地模板管理行
        src_row = main_box.row(align=True)
        src_row.prop(context.scene, "um_source_mat_enum", text="")
        src_row.separator(factor=0.6)
        btn_sub = src_row.row(align=True)
        btn_sub.scale_x = 1.15
        btn_sub.popover(panel="MATERIAL_PT_TemplateLibraryPopover", text="", icon='ASSET_MANAGER')
        btn_sub.operator("material.save_source_template", text="", icon='ADD')

        # 2. 材质操作工具栏（添加源材质、新建材质、全局同步/恢复）
        tools_row = main_box.row(align=True)
        tools_row.operator("material.apply_from_scene", text="添加", icon='IMPORT')
        tools_row.operator("material.create_new_pro", text="新建", icon='ADD')

        from .material_manager import MATERIAL_OT_ToggleSyncRevert
        global_sync_state = getattr(MATERIAL_OT_ToggleSyncRevert, "global_sync_state", False)
        sync_states = getattr(MATERIAL_OT_ToggleSyncRevert, "sync_states", {})
        has_individual_synced = any(state for state in sync_states.values())
        
        toggle_icon = 'LOOP_BACK' if global_sync_state else 'FILE_REFRESH'
        toggle_text = '恢复' if global_sync_state else '同步'
        
        toggle_subgroup = tools_row.row(align=True)
        toggle_subgroup.enabled = not has_individual_synced
        toggle_subgroup.operator(
            "material.global_toggle_sync_revert", 
            icon=toggle_icon, 
            text=toggle_text
        )

        # 确保材质列表即时更新
        from .utils import update_material_list_handler
        update_material_list_handler(context.scene)

        # 3. 选中材质专属操作栏（4个按钮严格四等分 25%/25%/25%/25%：赋予、基础色、单材质同步/恢复、删除）
        selected_mat = None
        if props.material_collection and 0 <= props.material_list_index < len(props.material_collection):
            selected_mat = props.material_collection[props.material_list_index].material
        elif props.selected_material_name and props.selected_material_name in bpy.data.materials:
            selected_mat = bpy.data.materials[props.selected_material_name]
        elif context.active_object and context.active_object.type == 'MESH' and context.active_object.active_material:
            selected_mat = context.active_object.active_material

        action_row = main_box.row(align=True)
        action_row.enabled = bool(selected_mat)

        # 严格 4 等分分割 (25% / 25% / 25% / 25%)
        s1 = action_row.split(factor=0.25, align=True)
        col1 = s1.column(align=True)
        
        s2 = s1.split(factor=0.333333, align=True)
        col2 = s2.column(align=True)
        
        s3 = s2.split(factor=0.5, align=True)
        col3 = s3.column(align=True)
        col4 = s3.column(align=True)

        # 按钮 1 (25%): 赋予选中面/物体
        assign_btn = col1.operator("material.assign_to_selected_faces_pro", text="", icon='FORWARD')
        if selected_mat:
            assign_btn.mat_name = selected_mat.name

        # 按钮 2 (25%): 材质基础色选择器
        has_color = False
        if selected_mat and selected_mat.use_nodes and selected_mat.node_tree:
            principled = None
            output = next((n for n in selected_mat.node_tree.nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output), None)
            if not output:
                output = next((n for n in selected_mat.node_tree.nodes if n.type == 'OUTPUT_MATERIAL'), None)
            
            if output and 'Surface' in output.inputs and output.inputs['Surface'].is_linked:
                linked_node = output.inputs['Surface'].links[0].from_node
                if linked_node.type == 'BSDF_PRINCIPLED':
                    principled = linked_node
                    
            if not principled:
                principled = next((n for n in selected_mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
                
            if principled:
                color_input = principled.inputs.get('Base Color')
                if color_input:
                    col2.prop(color_input, "default_value", text="")
                    has_color = True
                    
        if not has_color:
            col2.label(text="", icon='BLANK1')

        # 按钮 3 (25%): 单个材质同步/恢复
        is_synced = False
        if selected_mat:
            is_synced = MATERIAL_OT_ToggleSyncRevert.sync_states.get(selected_mat.name, False) or ("um_sync_backup" in selected_mat) or (selected_mat.use_nodes and selected_mat.node_tree and any(n.name.startswith("SYNC_") for n in selected_mat.node_tree.nodes))
        single_toggle_icon = 'LOOP_BACK' if is_synced else 'FILE_REFRESH'
        
        col3.enabled = bool(selected_mat) and (not global_sync_state)
        single_toggle_btn = col3.operator("material.toggle_sync_revert_pro", text="", icon=single_toggle_icon)
        if selected_mat:
            single_toggle_btn.mat_name = selected_mat.name

        # 按钮 4 (25%): 删除材质
        delete_btn = col4.operator("material.delete_pro", text="", icon='TRASH')
        if selected_mat:
            delete_btn.mat_name = selected_mat.name

        # 4. 材质列表
        list_box = main_box.box()
        list_box.template_list(
            "MATERIAL_UL_CustomListPro",
            "",
            props, "material_collection",
            props, "material_list_index",
            rows=2
        )

        # 统计信息
        stats_row = main_box.row(align=True)
        stats_row.label(text=f"场景材质总数: {len(bpy.data.materials)}", icon='WORLD_DATA')
        stats_row.separator()
        stats_row.label(text=f"当前管理: {len(props.material_collection)}", icon='MATERIAL_DATA')

# 注册函数
def register():
    bpy.utils.register_class(CUSTOM_OT_ClearNormals)
    bpy.utils.register_class(OBJECT_OT_AutoSmooth)
    bpy.utils.register_class(VIEW3D_PT_UltimateManager)
    bpy.utils.register_class(VIEW3D_PT_ModifierManager)
    bpy.utils.register_class(VIEW3D_PT_UVLayerManager)
    bpy.utils.register_class(IMAGE_PT_UVManager)
    bpy.utils.register_class(VIEW3D_PT_MaterialManager)

# 注销函数
def unregister():
    bpy.utils.unregister_class(VIEW3D_PT_MaterialManager)
    bpy.utils.unregister_class(IMAGE_PT_UVManager)
    bpy.utils.unregister_class(VIEW3D_PT_UVLayerManager)
    bpy.utils.unregister_class(VIEW3D_PT_ModifierManager)
    bpy.utils.unregister_class(VIEW3D_PT_UltimateManager)
    bpy.utils.unregister_class(OBJECT_OT_AutoSmooth)
    bpy.utils.unregister_class(CUSTOM_OT_ClearNormals)