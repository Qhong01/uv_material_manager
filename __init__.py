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
import importlib
from bpy.types import AddonPreferences
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    StringProperty,
)

if "modeling_tools" in locals():
    importlib.reload(workspace)
    importlib.reload(utils)
    importlib.reload(uv_manager)
    importlib.reload(material_manager)
    importlib.reload(modifier_manager)
    if "edge_rail_rotate" in locals():
        importlib.reload(edge_rail_rotate)
    else:
        from . import edge_rail_rotate
    if "quick_mirror" in locals():
        importlib.reload(quick_mirror)
    else:
        from . import quick_mirror
    if "focus_tools" in locals():
        importlib.reload(focus_tools)
    else:
        from . import focus_tools
    if "smart_vertex" in locals():
        importlib.reload(smart_vertex)
    else:
        from . import smart_vertex
    if "smart_edge" in locals():
        importlib.reload(smart_edge)
    else:
        from . import smart_edge
    if "smart_shortcuts" in locals():
        smart_shortcuts.unregister()
        importlib.reload(smart_shortcuts)
    else:
        from . import smart_shortcuts
    if "shortcut_panel" in locals():
        importlib.reload(shortcut_panel)
    else:
        from . import shortcut_panel
    if "model_checker" in locals():
        model_checker.unregister()
        importlib.reload(model_checker.diagnostic)
        importlib.reload(model_checker.overlay)
        importlib.reload(model_checker.operators)
        importlib.reload(model_checker.panel)
        importlib.reload(model_checker)
    else:
        from . import model_checker
    importlib.reload(ui)
    importlib.reload(shading_manager)
    importlib.reload(modeling_tools)
else:
    from . import workspace
    from . import utils
    from . import uv_manager
    from . import material_manager
    from . import modifier_manager
    from . import edge_rail_rotate
    from . import quick_mirror
    from . import focus_tools
    from . import smart_vertex
    from . import smart_edge
    from . import smart_shortcuts
    from . import shortcut_panel
    from . import model_checker
    from . import ui
    from . import shading_manager
    from . import modeling_tools

from .material_manager import MaterialTemplateManager

# 插件首选项面板
class UVMM_AddonPreferences(AddonPreferences):
    bl_idname = __package__

    active_tab: EnumProperty(
        name="设置页面",
        items=(
            ('MATERIAL_LIB', "材质库", "材质模板库管理"),
            ('SHORTCUTS', "快捷键", "快捷键设置"),
        ),
        default='MATERIAL_LIB',
    )
    model_checker_migrated: BoolProperty(default=False, options={'HIDDEN'})
    removed_shortcuts: StringProperty(default="", options={'HIDDEN'})
    uv_mode_normalizing: BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})
    ngon_clear_selection: BoolProperty(name="运行前清除其他选择", default=True, update=model_checker._schedule_preferences_save)
    ngon_sides: IntProperty(name="边数大于", description="顶点数超过此值的面会被选中", default=4, min=3, max=100, update=model_checker._schedule_preferences_save)
    uv_show_stretch: BoolProperty(name="显示 UV 拉伸", default=False, update=model_checker._update_scene_overlay)
    uv_stretch_opacity: FloatProperty(name="热力图透明度", default=0.85, min=0.0, max=1.0, update=model_checker._update_scene_overlay)
    uv_stretch_base_color: FloatVectorProperty(name="无拉伸颜色", subtype='COLOR', size=3, default=(0.43, 0.78, 1.0), min=0.0, max=1.0, update=model_checker._update_scene_overlay)
    uv_stretch_max_color: FloatVectorProperty(name="拉伸颜色", subtype='COLOR', size=3, default=(0.72, 0.34, 0.025), min=0.0, max=1.0, update=model_checker._update_scene_overlay)
    uv_stretch_strength: FloatProperty(name="拉伸显示强度", description="提高后较轻微的拉伸也会有更明显的颜色变化", default=2.0, min=0.25, max=8.0, update=model_checker._update_scene_overlay)
    uv_stretch_threshold: FloatProperty(name="可接受拉伸阈值", description="低于此阈值的拉伸按无拉伸颜色显示", default=0.0, min=0.0, max=0.999, subtype='PERCENTAGE', update=model_checker._update_scene_overlay)
    uv_show_boundaries: BoolProperty(name="显示 UV 分界边", default=False, update=model_checker._update_scene_overlay)
    uv_boundary_color: FloatVectorProperty(name="边界颜色", subtype='COLOR', size=3, default=(1.0, 0.55, 0.08), min=0.0, max=1.0, update=model_checker._update_scene_overlay)
    uv_boundary_width: FloatProperty(name="分界线粗细", default=4.0, min=1.0, max=12.0, update=model_checker._update_boundary_width)
    uv_show_checker: BoolProperty(name="显示棋盘格", default=False, update=model_checker._update_scene_overlay)
    uv_checker_opacity: FloatProperty(name="棋盘格透明度", default=0.75, min=0.0, max=1.0, update=model_checker._update_checker_settings)
    uv_checker_density: IntProperty(name="棋盘格密度", default=8, min=1, max=256, update=model_checker._update_checker_settings)
    uv_checker_color_a: FloatVectorProperty(name="棋盘格颜色 A", subtype='COLOR', size=3, default=(0.95, 0.95, 0.95), min=0.0, max=1.0, update=model_checker._update_checker_settings)
    uv_checker_color_b: FloatVectorProperty(name="棋盘格颜色 B", subtype='COLOR', size=3, default=(0.12, 0.12, 0.12), min=0.0, max=1.0, update=model_checker._update_checker_settings)
    uv_show_orientation: BoolProperty(name="显示 UV 正反", default=False, update=model_checker._update_scene_overlay)
    uv_orientation_opacity: FloatProperty(name="UV 正反透明度", default=0.9, min=0.0, max=1.0, update=model_checker._update_scene_overlay)
    uv_orientation_front_color: FloatVectorProperty(name="正向颜色", subtype='COLOR', size=3, default=(0.43, 0.78, 1.0), min=0.0, max=1.0, update=model_checker._update_scene_overlay)
    uv_orientation_back_color: FloatVectorProperty(name="反向颜色", subtype='COLOR', size=3, default=(0.94, 0.045, 0.055), min=0.0, max=1.0, update=model_checker._update_scene_overlay)
    isolated_include_hidden: BoolProperty(name="包括隐藏顶点", default=False, update=model_checker._schedule_preferences_save)
    isolated_clear_selection: BoolProperty(name="运行前清除其他选择", default=True, update=model_checker._schedule_preferences_save)
    uv_overlay_tuning_version: IntProperty(default=0, options={'HIDDEN'})

    def draw(self, context):
        layout = self.layout

        tabs = layout.row(align=True)
        tabs.scale_y = 1.2
        tabs.prop(self, "active_tab", expand=True)
        layout.separator()

        if self.active_tab == 'MATERIAL_LIB':
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
        elif self.active_tab == 'SHORTCUTS':
            shortcut_panel.draw_shortcut_preferences(layout, context)

def safe_register_class(cls):
    try:
        bpy.utils.register_class(cls)
    except ValueError:
        try:
            bpy.utils.unregister_class(cls)
            bpy.utils.register_class(cls)
        except Exception:
            pass

def safe_unregister_class(cls):
    try:
        bpy.utils.unregister_class(cls)
    except Exception:
        pass

# 注册所有模块
def register():
    safe_register_class(UVMM_AddonPreferences)
    workspace.register()
    utils.register()
    uv_manager.register()
    material_manager.register()
    modifier_manager.register()
    edge_rail_rotate.register()
    quick_mirror.register()
    focus_tools.register()
    smart_vertex.register()
    smart_edge.register()
    smart_shortcuts.register()
    ui.register()
    shading_manager.register()
    modeling_tools.register()
    shortcut_panel.register()
    if not bpy.app.timers.is_registered(model_checker.migrate_preferences):
        bpy.app.timers.register(model_checker.migrate_preferences, first_interval=0.2)
    model_checker.register()

# 注销所有模块
def unregister():
    shortcut_panel.unregister()
    modeling_tools.unregister()
    model_checker.unregister()
    shading_manager.unregister()
    ui.unregister()
    focus_tools.unregister()
    smart_shortcuts.unregister()
    smart_edge.unregister()
    smart_vertex.unregister()
    quick_mirror.unregister()
    edge_rail_rotate.unregister()
    modifier_manager.unregister()
    material_manager.unregister()
    uv_manager.unregister()
    utils.unregister()
    workspace.unregister()
    safe_unregister_class(UVMM_AddonPreferences)

if __name__ == "__main__":
    register()
