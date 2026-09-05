bl_info = {
    "name": "UV_Material_Manager",
    "author": "xiaoshui",
    "version": (1, 0, 0),
    "blender": (4, 3, 0),
    "location": "View3D > Sidebar > UV Tools",
    "description": "完整UV和材质管理（含材质数据同步和状态显示）",
    "category": "Mesh",
}

import bpy
from bpy.types import AddonPreferences
from . import workspace
from . import utils
from . import uv_manager
from . import material_manager
from . import modifier_manager
from . import ui
from . import uv_checker
from . import shading_manager
from .material_manager import MaterialTemplateManager

# 插件首选项面板
class UVMM_AddonPreferences(AddonPreferences):
    bl_idname = __package__

    def draw(self, context):
        layout = self.layout
        templates = sorted(MaterialTemplateManager.get_template_names())
        lib_path = MaterialTemplateManager.get_lib_path()
        
        box = layout.box()
        header = box.row(align=True)
        header.label(text="材质模板库管理 (跨工程/跨电脑通用)", icon='ASSET_MANAGER')
        
        info_col = box.column(align=True)
        info_col.label(text=f"当前已保存材质模板数量: {len(templates)} 个", icon='MATERIAL')
        info_col.label(text=f"本地物理库文件路径: {lib_path}", icon='FILE_BLEND')
        
        box.separator(factor=0.5)
        row = box.row(align=True)
        row.scale_y = 1.2
        row.operator("material.export_template_library", text="导出材质库 (.blend)", icon='EXPORT')
        row.operator("material.import_template_library", text="导入材质库 (.blend)", icon='IMPORT')
        row.operator("material.open_template_library_folder", text="打开存放文件夹", icon='FILE_FOLDER')

# 注册所有模块
def register():
    bpy.utils.register_class(UVMM_AddonPreferences)
    workspace.register()
    utils.register()
    uv_manager.register()
    material_manager.register()
    modifier_manager.register()
    ui.register()
    uv_checker.register()
    shading_manager.register()

# 注销所有模块
def unregister():
    shading_manager.unregister()
    uv_checker.unregister()
    ui.unregister()
    modifier_manager.unregister()
    material_manager.unregister()
    uv_manager.unregister()
    utils.unregister()
    workspace.unregister()
    bpy.utils.unregister_class(UVMM_AddonPreferences)

if __name__ == "__main__":
    register()