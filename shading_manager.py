import bpy
import colorsys
from bpy.types import Operator, Panel
from bpy.props import IntProperty

PRESET_COLORS = [
    (0.95, 0.15, 0.15),   # 红色
    (0.98, 0.45, 0.10),   # 橙色
    (0.98, 0.72, 0.12),   # 琥珀黄
    (0.95, 0.95, 0.18),   # 亮黄
    (0.60, 0.92, 0.18),   # 嫩绿
    (0.18, 0.85, 0.25),   # 翠绿
    (0.12, 0.88, 0.82),   # 青色
    (0.18, 0.65, 0.98),   # 天蓝
    (0.15, 0.35, 0.95),   # 宝蓝
    (0.55, 0.18, 0.95),   # 紫色
    (0.92, 0.18, 0.75),   # 洋红
    (0.95, 0.95, 0.95),   # 纯白
]

PRESET_ICONS = [
    'COLORSET_01_VEC',
    'COLORSET_02_VEC',
    'COLORSET_03_VEC',
    'COLORSET_04_VEC',
    'COLORSET_05_VEC',
    'COLORSET_06_VEC',
    'COLORSET_07_VEC',
    'COLORSET_08_VEC',
    'COLORSET_09_VEC',
    'COLORSET_10_VEC',
    'COLORSET_11_VEC',
    'COLORSET_12_VEC',
]


class SHADING_OT_SetObjectColorIndex(Operator):
    """设置选中物体的视口显示颜色"""
    bl_idname = "shading.set_object_color_index"
    bl_label = "设置视口颜色"
    bl_description = "将所选预设调色板颜色应用到所有选中物体的视口颜色中"
    bl_options = {'REGISTER', 'UNDO'}

    color_index: IntProperty(default=0)

    def execute(self, context):
        if 0 <= self.color_index < len(PRESET_COLORS):
            r, g, b = PRESET_COLORS[self.color_index]
            objs = [o for o in context.selected_objects if o.type in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}]
            if not objs and context.object:
                objs = [context.object]

            for o in objs:
                alpha = o.color[3] if len(o.color) > 3 else 1.0
                o.color = (r, g, b, alpha)

            for a in context.screen.areas:
                if a.type == 'VIEW_3D':
                    for space in a.spaces:
                        if space.type == 'VIEW_3D' and hasattr(space, 'shading'):
                            space.shading.color_type = 'OBJECT'
                            a.tag_redraw()

        return {'FINISHED'}


class SHADING_OT_RandomizeObjectColor(Operator):
    """为选中的每个物体分配互不相同的随机视口颜色"""
    bl_idname = "shading.randomize_object_color"
    bl_label = "随机化颜色"
    bl_description = "为每一个选中的物体分配独立的随机视口颜色，方便直观区分各个模型构件"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        objs = [o for o in context.selected_objects if o.type in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}]
        if not objs and context.object:
            objs = [context.object]

        if not objs:
            self.report({'WARNING'}, "未选择任何物体")
            return {'CANCELLED'}

        count = len(objs)
        for i, obj in enumerate(objs):
            hue = ((i * 0.618033988749895) + 0.12) % 1.0
            sat = 0.75 + (i % 3) * 0.1
            val = 0.85 + (i % 2) * 0.1
            r, g, b = colorsys.hsv_to_rgb(hue, min(sat, 1.0), min(val, 1.0))
            alpha = obj.color[3] if len(obj.color) > 3 else 1.0
            obj.color = (r, g, b, alpha)

        for a in context.screen.areas:
            if a.type == 'VIEW_3D':
                for space in a.spaces:
                    if space.type == 'VIEW_3D' and hasattr(space, 'shading'):
                        space.shading.color_type = 'OBJECT'
                        a.tag_redraw()

        self.report({'INFO'}, f"已为 {count} 个物体分配随机视口颜色")
        return {'FINISHED'}


class SHADING_OT_RandomizeCollectionColor(Operator):
    """按物体所属集合为物体分配独特的集合视口颜色"""
    bl_idname = "shading.randomize_collection_color"
    bl_label = "为物体集合添加随机颜色"
    bl_description = "按集合分组为所选物体赋予统一的视口颜色"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        objs = [o for o in context.selected_objects if o.type in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}]
        if not objs and context.object:
            objs = [context.object]

        if not objs:
            self.report({'WARNING'}, "未选择任何物体")
            return {'CANCELLED'}

        col_map = {}
        col_idx = 0
        for obj in objs:
            main_col = obj.users_collection[0] if obj.users_collection else None
            col_name = main_col.name if main_col else "Default"
            if col_name not in col_map:
                hue = ((col_idx * 0.618033988749895) + 0.28) % 1.0
                r, g, b = colorsys.hsv_to_rgb(hue, 0.8, 0.9)
                col_map[col_name] = (r, g, b)
                col_idx += 1

            r, g, b = col_map[col_name]
            alpha = obj.color[3] if len(obj.color) > 3 else 1.0
            obj.color = (r, g, b, alpha)

        for a in context.screen.areas:
            if a.type == 'VIEW_3D':
                for space in a.spaces:
                    if space.type == 'VIEW_3D' and hasattr(space, 'shading'):
                        space.shading.color_type = 'OBJECT'
                        a.tag_redraw()

        self.report({'INFO'}, f"已按 {len(col_map)} 个集合为物体赋予颜色")
        return {'FINISHED'}


class SHADING_OT_ResetObjectColor(Operator):
    """重置物体的视口颜色为默认白色"""
    bl_idname = "shading.reset_object_color"
    bl_label = "重置颜色"
    bl_description = "重置选中物体的视口颜色与透明度为初始值"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        objs = [o for o in context.selected_objects if o.type in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}]
        if not objs and context.object:
            objs = [context.object]
        for o in objs:
            o.color = (1.0, 1.0, 1.0, 1.0)
        for a in context.screen.areas:
            if a.type == 'VIEW_3D':
                a.tag_redraw()
        return {'FINISHED'}


class VIEW3D_PT_QuickShadingPopover(Panel):
    """快速着色弹出菜单：涵盖视图着色方式与物体着色全部核心设置"""
    bl_idname = "VIEW3D_PT_QuickShadingPopover"
    bl_label = "着色弹出菜单"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_ui_units_x = 14

    def draw(self, context):
        layout = self.layout
        space = context.space_data
        shading = getattr(space, 'shading', None)
        overlay = getattr(space, 'overlay', None)
        obj = context.active_object

        # 顶栏快捷 4 种着色模式切换
        if shading:
            header_row = layout.row(align=True)
            header_row.scale_y = 1.15
            header_row.prop(shading, "type", expand=True)

        # =========================================================================
        # 1. 视图着色方式 (Viewport Shading)
        # =========================================================================
        box_view = layout.box()
        view_title = box_view.row(align=True)
        view_title.label(text="视图着色方式", icon='SHADING_SOLID')

        if shading and shading.type == 'SOLID':
            # 光照
            box_view.label(text="光照", icon='LIGHT_SUN')
            box_view.prop(shading, "light", expand=True)

            if shading.light in {'STUDIO', 'MATCAP'}:
                box_view.template_icon_view(shading, "studio_light", show_labels=False)

            # 线框颜色
            if hasattr(shading, 'wireframe_color_type'):
                box_view.label(text="线框颜色")
                box_view.prop(shading, "wireframe_color_type", expand=True)

            # 颜色
            box_view.label(text="颜色")
            color_row1 = box_view.row(align=True)
            color_row1.prop_enum(shading, "color_type", 'MATERIAL')
            color_row1.prop_enum(shading, "color_type", 'RANDOM')
            color_row1.prop_enum(shading, "color_type", 'TEXTURE')

            color_row2 = box_view.row(align=True)
            color_row2.prop_enum(shading, "color_type", 'OBJECT')
            color_row2.prop_enum(shading, "color_type", 'VERTEX')
            color_row2.prop_enum(shading, "color_type", 'SINGLE')

            if shading.color_type == 'SINGLE':
                box_view.prop(shading, "single_color", text="")

            # 背景
            box_view.label(text="背景")
            box_view.prop(shading, "background_type", expand=True)
            if shading.background_type == 'VIEWPORT':
                box_view.prop(shading, "background_color", text="")

            # 附加选项
            opt_box = box_view.box()
            opt_row = opt_box.row(align=True)
            opt_row.prop(shading, "show_backface_culling", text="背面剔除")
            opt_row.prop(shading, "show_shadows", text="阴影")
            
            opt_row2 = opt_box.row(align=True)
            opt_row2.prop(shading, "show_cavity", text="空腔/曲率")
            opt_row2.prop(shading, "show_object_outline", text="轮廓线")
        elif shading:
            box_view.label(text=f"当前处于 {shading.type} 模式", icon='INFO')

        # =========================================================================
        # 2. 物体着色 (Object Shading & Display)
        # =========================================================================
        box_obj = layout.box()
        obj_title = box_obj.row(align=True)
        obj_title.label(text="物体着色", icon='OBJECT_DATA')

        if obj:
            # 2x2 常用快捷显示开关
            grid = box_obj.grid_flow(columns=2, align=True)
            grid.prop(obj, "show_wire", text="线框")
            grid.prop(obj, "show_in_front", text="在前面")
            
            if overlay and hasattr(overlay, 'show_retopology'):
                grid.prop(overlay, "show_retopology", text="重拓扑")
            elif hasattr(obj, 'show_all_edges'):
                grid.prop(obj, "show_all_edges", text="所有边")
            else:
                grid.label(text="")

            if hasattr(obj, 'is_shadow_catcher'):
                grid.prop(obj, "is_shadow_catcher", text="阴影捕捉")
            else:
                grid.label(text="")

            # 显示为
            box_obj.label(text="显示为")
            box_obj.prop(obj, "display_type", expand=True)

            # 视口调色板
            box_obj.label(text="视口颜色")
            palette_row = box_obj.row(align=True)
            palette_row.scale_y = 1.2
            for i in range(len(PRESET_COLORS)):
                icon_name = PRESET_ICONS[i] if i < len(PRESET_ICONS) else 'BLANK1'
                btn = palette_row.operator("shading.set_object_color_index", text="", icon=icon_name)
                btn.color_index = i

            # 随机化颜色工具
            tool_col = box_obj.column(align=True)
            tool_col.scale_y = 1.15
            tool_col.operator("shading.randomize_object_color", text="随机化颜色", icon='COLOR')
            tool_col.operator("shading.randomize_collection_color", text="为物体集合添加随机颜色", icon='GROUP')

            # 透明度与自定义颜色
            alpha_row = box_obj.row(align=True)
            alpha_row.prop(obj, "color", index=3, text="透明", slider=True)
            alpha_row.prop(obj, "color", text="")
            alpha_row.operator("shading.reset_object_color", text="", icon='X')
        else:
            box_obj.label(text="请选择网格物体以调整物体着色", icon='ERROR')


classes = (
    SHADING_OT_SetObjectColorIndex,
    SHADING_OT_RandomizeObjectColor,
    SHADING_OT_RandomizeCollectionColor,
    SHADING_OT_ResetObjectColor,
    VIEW3D_PT_QuickShadingPopover,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
