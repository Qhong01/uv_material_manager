import bpy
from bpy.types import Operator

# 工作区切换功能
class WORKSPACE_OT_ToggleUVLayout(Operator):
    bl_idname = "workspace.toggle_uv_layout"
    bl_label = "切换UV布局"
    bl_description = "切换UV编辑器布局"

    def execute(self, context):
        # 确保在3D视图中操作
        if context.area.type != 'VIEW_3D':
            self.report({'WARNING'}, "请在3D视图中执行此操作")
            return {'CANCELLED'}

        screen = context.screen

        # 检查是否存在材质布局，如果有则替换为UV布局
        material_area = next((area for area in screen.areas if area.type == 'NODE_EDITOR'), None)
        if material_area:
            # 保存当前材质编辑器的侧边栏状态
            material_space = material_area.spaces.active
            show_region_toolbar = material_space.show_region_toolbar
            show_region_ui = material_space.show_region_ui
            
            # 切换到UV编辑器
            material_area.type = 'IMAGE_EDITOR'
            uv_space = material_area.spaces.active
            uv_space.mode = 'UV'
            
            # 恢复侧边栏状态
            uv_space.show_region_toolbar = show_region_toolbar
            uv_space.show_region_ui = show_region_ui
            
            material_area.tag_redraw()
            self.report({'INFO'}, "材质编辑器已替换为UV编辑器")
            return {'FINISHED'}

        # 检查是否存在UV布局，如果有则恢复原始布局
        uv_area = next((area for area in screen.areas if area.type == 'IMAGE_EDITOR'), None)
        if uv_area:
            return self.restore_original_layout(context, screen)

        # 如果没有找到相关布局，则创建UV布局
        return self.create_uv_layout(context)

    def create_uv_layout(self, context):
        """创建UV编辑器布局"""
        try:
            original_area = context.area
            # 保存原始区域的侧边栏状态
            original_space = original_area.spaces.active
            show_region_toolbar = original_space.show_region_toolbar
            show_region_ui = original_space.show_region_ui
            
            with context.temp_override(
                window=context.window,
                area=original_area,
                region=original_area.regions[-1]
            ):
                bpy.ops.screen.area_split(direction='VERTICAL', factor=0.4)

            uv_area = context.screen.areas[-1]
            uv_area.type = 'IMAGE_EDITOR'
            uv_space = uv_area.spaces.active
            uv_space.mode = 'UV'
            
            # 应用原始区域的侧边栏状态
            uv_space.show_region_toolbar = show_region_toolbar
            uv_space.show_region_ui = show_region_ui

            uv_area.tag_redraw()
            self.report({'INFO'}, "UV编辑器已创建")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"创建失败: {str(e)}")
            return {'CANCELLED'}

    def restore_original_layout(self, context, screen):
        """恢复原始布局"""
        try:
            areas_to_close = [a for a in screen.areas if a.type == 'IMAGE_EDITOR']
            for area in reversed(areas_to_close):
                with context.temp_override(
                    window=context.window,
                    area=area,
                    region=area.regions[-1]
                ):
                    bpy.ops.screen.area_close()

            if context.area:
                context.area.tag_redraw()

            self.report({'INFO'}, "UV布局已恢复")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"恢复失败: {str(e)}")
            return {'CANCELLED'}


class WORKSPACE_OT_ToggleMaterialLayout(Operator):
    bl_idname = "workspace.toggle_material_layout"
    bl_label = "切换材质布局"
    bl_description = "切换材质编辑器布局"

    def execute(self, context):
        # 确保在3D视图中操作
        if context.area.type != 'VIEW_3D':
            self.report({'WARNING'}, "请在3D视图中执行此操作")
            return {'CANCELLED'}

        screen = context.screen

        # 检查是否存在UV布局，如果有则替换为材质布局
        uv_area = next((area for area in screen.areas if area.type == 'IMAGE_EDITOR'), None)
        if uv_area:
            # 保存当前UV编辑器的侧边栏状态
            uv_space = uv_area.spaces.active
            show_region_toolbar = uv_space.show_region_toolbar
            show_region_ui = uv_space.show_region_ui
            
            # 切换到材质编辑器
            uv_area.type = 'NODE_EDITOR'
            shader_space = uv_area.spaces.active
            shader_space.tree_type = 'ShaderNodeTree'
            
            # 恢复侧边栏状态
            shader_space.show_region_toolbar = show_region_toolbar
            shader_space.show_region_ui = show_region_ui
            
            uv_area.tag_redraw()
            context.scene.uv_layout_active = False
            self.report({'INFO'}, "UV编辑器已替换为材质编辑器")
            return {'FINISHED'}

        # 检查是否存在材质布局，如果有则恢复原始布局
        material_area = next((area for area in screen.areas if area.type == 'NODE_EDITOR'), None)
        if material_area:
            return self.restore_original_layout(context, screen)

        # 如果没有找到相关布局，则创建材质布局
        return self.create_material_layout(context)

    def create_material_layout(self, context):
        """创建材质编辑器布局"""
        try:
            original_area = context.area
            # 保存原始区域的侧边栏状态
            original_space = original_area.spaces.active
            show_region_toolbar = original_space.show_region_toolbar
            show_region_ui = original_space.show_region_ui
            
            with context.temp_override(
                window=context.window,
                area=original_area,
                region=original_area.regions[-1]
            ):
                bpy.ops.screen.area_split(direction='VERTICAL', factor=0.4)

            shader_area = context.screen.areas[-1]
            shader_area.type = 'NODE_EDITOR'
            shader_space = shader_area.spaces.active
            shader_space.tree_type = 'ShaderNodeTree'
            
            # 应用原始区域的侧边栏状态
            shader_space.show_region_toolbar = show_region_toolbar
            shader_space.show_region_ui = show_region_ui

            shader_area.tag_redraw()
            context.scene.uv_layout_active = False
            self.report({'INFO'}, "材质编辑器已创建")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"创建失败: {str(e)}")
            return {'CANCELLED'}

    def restore_original_layout(self, context, screen):
        """恢复原始布局"""
        try:
            areas_to_close = [a for a in screen.areas if a.type == 'NODE_EDITOR']  # 修复area变量作用域问题
            for area in reversed(areas_to_close):
                with context.temp_override(
                    window=context.window,
                    area=area,
                    region=area.regions[-1]
                ):
                    bpy.ops.screen.area_close()

            if context.area:
                context.area.tag_redraw()

            context.scene.uv_layout_active = True
            self.report({'INFO'}, "材质布局已恢复")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"恢复失败: {str(e)}")
            return {'CANCELLED'}

addon_keymaps = []

# 注册函数
def register():
    bpy.utils.register_class(WORKSPACE_OT_ToggleUVLayout)
    bpy.utils.register_class(WORKSPACE_OT_ToggleMaterialLayout)

    # 注册快捷键
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon

    if kc:
        # 切换UV布局快捷键
        km = kc.keymaps.new(name='3D View', space_type='VIEW_3D')
        kmi = km.keymap_items.new(
            WORKSPACE_OT_ToggleUVLayout.bl_idname,
            'T', 'PRESS', shift=True
        )
        addon_keymaps.append((km, kmi))

        # 切换材质布局快捷键
        kmi = km.keymap_items.new(
            WORKSPACE_OT_ToggleMaterialLayout.bl_idname,
            'Y', 'PRESS', shift=True
        )
        addon_keymaps.append((km, kmi))

# 注销函数
def unregister():
    bpy.utils.unregister_class(WORKSPACE_OT_ToggleMaterialLayout)
    bpy.utils.unregister_class(WORKSPACE_OT_ToggleUVLayout)

    # 注销快捷键
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon

    if kc:
        for km, kmi in addon_keymaps:
            km.keymap_items.remove(kmi)
        addon_keymaps.clear()