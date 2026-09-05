import os
import struct
import zlib
import colorsys
import bpy
import bpy.utils.previews
from bpy.types import Operator, Panel, PropertyGroup
from bpy.props import IntProperty, BoolProperty, PointerProperty

PRESET_COLORS = [
    (1.0, 0.0, 0.0),     # 纯红
    (1.0, 0.4, 0.0),     # 橙色
    (1.0, 0.7, 0.0),     # 琥珀黄
    (1.0, 1.0, 0.0),     # 柠檬黄
    (0.6, 1.0, 0.0),     # 嫩绿
    (0.0, 1.0, 0.0),     # 纯绿
    (0.0, 1.0, 1.0),     # 青色
    (0.0, 0.65, 1.0),    # 天蓝
    (0.0, 0.2, 1.0),     # 宝蓝
    (0.5, 0.0, 1.0),     # 紫色
    (1.0, 0.0, 0.7),     # 洋红
    (1.0, 1.0, 1.0),     # 纯白
]

preview_collections = {}

def make_solid_png(filepath, r, g, b, width=16, height=16):
    raw_data = bytearray()
    for _ in range(height):
        raw_data.append(0)
        for _ in range(width):
            raw_data.extend([int(r*255), int(g*255), int(b*255), 255])
    compressed = zlib.compress(raw_data)
    def chunk(tag, data):
        c = struct.pack('>I', len(data)) + tag + data
        return c + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
    header = b'\x89PNG\r\n\x1a\n'
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    with open(filepath, 'wb') as f:
        f.write(header)
        f.write(chunk(b'IHDR', ihdr))
        f.write(chunk(b'IDAT', compressed))
        f.write(chunk(b'IEND', b''))


def ensure_color_icons():
    addon_dir = os.path.dirname(__file__)
    icons_dir = os.path.join(addon_dir, 'icons')
    os.makedirs(icons_dir, exist_ok=True)
    for i, (r, g, b) in enumerate(PRESET_COLORS):
        fp = os.path.join(icons_dir, f'color_{i+1:02d}.png')
        if not os.path.exists(fp):
            make_solid_png(fp, r, g, b)


class ShadingUIProps(PropertyGroup):
    show_viewport_shading: BoolProperty(name="视图着色方式", default=True)
    show_shading_options: BoolProperty(name="选项", default=False)
    show_object_shading: BoolProperty(name="物体着色", default=True)
    show_stored_views: BoolProperty(name="存储视图", default=False)


class SHADING_OT_SetObjectColorIndex(Operator):
    bl_idname = "shading.set_object_color_index"
    bl_label = "设置视口颜色"
    bl_description = "将所选预设纯色应用到所有选中物体的视口颜色"
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
    bl_idname = "VIEW3D_PT_QuickShadingPopover"
    bl_label = "着色弹出菜单"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_ui_units_x = 13

    def draw(self, context):
        layout = self.layout
        space = context.space_data
        shading = getattr(space, 'shading', None)
        overlay = getattr(space, 'overlay', None)
        obj = context.active_object
        props = getattr(context.scene, 'um_shading_props', None)
        if not props:
            return

        # 顶栏快捷 4 种着色模式切换
        if shading:
            row_modes = layout.row(align=True)
            row_modes.scale_y = 1.15
            row_modes.prop(shading, "type", expand=True)

        # 渲染引擎切换
        row_eng = layout.row(align=True)
        engine_items = [e.identifier for e in context.scene.render.bl_rna.properties['engine'].enum_items]
        eevee_name = 'BLENDER_EEVEE_NEXT' if 'BLENDER_EEVEE_NEXT' in engine_items else 'BLENDER_EEVEE'
        row_eng.prop_enum(context.scene.render, "engine", eevee_name, text="Eevee", icon='SHADING_RENDERED')
        row_eng.prop_enum(context.scene.render, "engine", 'CYCLES', text="Cycles", icon='SCENE')
        row_eng.popover(panel="VIEW3D_PT_shading", text="", icon='PREFERENCES')

        # 显示网格与简化
        row_mesh = layout.row(align=True)
        if overlay:
            row_mesh.prop(overlay, "show_floor", text="显示网格", icon='GRID', toggle=True)
        row_mesh.prop(context.scene.render, "use_simplify", text="简化", icon='RNA', toggle=True)

        # =========================================================================
        # 1. 视图着色方式 (可折叠)
        # =========================================================================
        box_view = layout.box()
        h_view = box_view.row(align=True)
        icon_view = 'DOWNARROW_HLT' if props.show_viewport_shading else 'RIGHTARROW'
        h_view.prop(props, "show_viewport_shading", text="视图着色方式", icon=icon_view, emboss=False)

        if props.show_viewport_shading and shading and shading.type == 'SOLID':
            # 光照：横排三按钮平铺
            box_view.label(text="光照")
            row_light = box_view.row(align=True)
            row_light.prop_enum(shading, "light", 'STUDIO', text="棚灯")
            row_light.prop_enum(shading, "light", 'MATCAP', text="快照材质")
            row_light.prop_enum(shading, "light", 'FLAT', text="平面")

            if shading.light in {'STUDIO', 'MATCAP'}:
                box_view.template_icon_view(shading, "studio_light", show_labels=False)

            # 线框颜色：横排三按钮平铺
            if hasattr(shading, 'wireframe_color_type'):
                box_view.label(text="线框颜色")
                row_wire = box_view.row(align=True)
                row_wire.prop_enum(shading, "wireframe_color_type", 'THEME', text="主题")
                row_wire.prop_enum(shading, "wireframe_color_type", 'OBJECT', text="物体")
                row_wire.prop_enum(shading, "wireframe_color_type", 'RANDOM', text="随机")

            # 颜色：两行三列紧凑网格
            box_view.label(text="颜色")
            row_c1 = box_view.row(align=True)
            row_c1.prop_enum(shading, "color_type", 'MATERIAL', text="材质")
            row_c1.prop_enum(shading, "color_type", 'RANDOM', text="随机")
            row_c1.prop_enum(shading, "color_type", 'TEXTURE', text="纹理")

            row_c2 = box_view.row(align=True)
            row_c2.prop_enum(shading, "color_type", 'OBJECT', text="物体")
            row_c2.prop_enum(shading, "color_type", 'VERTEX', text="属性")
            row_c2.prop_enum(shading, "color_type", 'SINGLE', text="自定义")

            if shading.color_type == 'SINGLE':
                box_view.prop(shading, "single_color", text="")

            # 背景：横排三按钮平铺
            box_view.label(text="背景")
            row_bg = box_view.row(align=True)
            row_bg.prop_enum(shading, "background_type", 'THEME', text="主题")
            row_bg.prop_enum(shading, "background_type", 'WORLD', text="世界")
            row_bg.prop_enum(shading, "background_type", 'VIEWPORT', text="自定义")

            if shading.background_type == 'VIEWPORT':
                box_view.prop(shading, "background_color", text="")

            # 选项 (可折叠)
            box_opt = box_view.box()
            h_opt = box_opt.row(align=True)
            icon_opt = 'DOWNARROW_HLT' if props.show_shading_options else 'RIGHTARROW'
            h_opt.prop(props, "show_shading_options", text="选项", icon=icon_opt, emboss=False)

            if props.show_shading_options:
                g_opt = box_opt.grid_flow(columns=2, align=True)
                g_opt.prop(shading, "show_backface_culling", text="背面剔除", toggle=True)
                g_opt.prop(shading, "show_shadows", text="阴影", toggle=True)
                g_opt.prop(shading, "show_cavity", text="空腔/曲率", toggle=True)
                g_opt.prop(shading, "show_object_outline", text="轮廓线", toggle=True)

        # =========================================================================
        # 2. 物体着色 (可折叠)
        # =========================================================================
        box_obj = layout.box()
        h_obj = box_obj.row(align=True)
        icon_obj = 'DOWNARROW_HLT' if props.show_object_shading else 'RIGHTARROW'
        h_obj.prop(props, "show_object_shading", text="物体着色", icon=icon_obj, emboss=False)

        if props.show_object_shading and obj:
            # 四格扁平切换按钮
            grid_obj = box_obj.grid_flow(columns=2, align=True)
            grid_obj.prop(obj, "show_wire", text="线框", toggle=True)
            grid_obj.prop(obj, "show_in_front", text="在前面", toggle=True)

            if overlay and hasattr(overlay, 'show_retopology'):
                grid_obj.prop(overlay, "show_retopology", text="重拓扑", toggle=True)
            elif hasattr(obj, 'show_all_edges'):
                grid_obj.prop(obj, "show_all_edges", text="重拓扑", toggle=True)
            else:
                grid_obj.label(text="")

            if hasattr(obj, 'is_shadow_catcher'):
                grid_obj.prop(obj, "is_shadow_catcher", text="阴影捕捉", toggle=True)
            else:
                grid_obj.label(text="")

            # 显示为 (横排四按钮平铺)
            box_obj.label(text="显示为")
            row_disp = box_obj.row(align=True)
            row_disp.prop_enum(obj, "display_type", 'BOUNDS', text="边界范围")
            row_disp.prop_enum(obj, "display_type", 'WIRE', text="线框")
            row_disp.prop_enum(obj, "display_type", 'SOLID', text="实体")
            row_disp.prop_enum(obj, "display_type", 'TEXTURED', text="纹理")

            # 视口颜色 (12 色纯色方块调色板)
            box_obj.label(text="视口颜色")
            pcoll = preview_collections.get("main")
            pal_row = box_obj.row(align=True)
            pal_row.scale_y = 1.25
            for i in range(12):
                icon_id = pcoll[f"color_{i+1:02d}"].icon_id if pcoll and f"color_{i+1:02d}" in pcoll else 0
                btn = pal_row.operator("shading.set_object_color_index", text="", icon_value=icon_id)
                btn.color_index = i

            # 随机化颜色工具
            col_tools = box_obj.column(align=True)
            col_tools.scale_y = 1.15
            col_tools.operator("shading.randomize_object_color", text="随机化颜色", icon='COLOR')
            col_tools.operator("shading.randomize_collection_color", text="为物体集合添加随机颜色", icon='GROUP')

            # 透明度行
            alpha_row = box_obj.row(align=True)
            alpha_row.scale_y = 1.15
            alpha_row.prop(obj, "color", index=3, text="透明", slider=True)
            alpha_row.prop(obj, "color", text="")
            alpha_row.operator("shading.reset_object_color", text="", icon='X')

            # 存储视图 (可折叠)
            box_stored = box_obj.box()
            h_stored = box_stored.row(align=True)
            icon_stored = 'DOWNARROW_HLT' if props.show_stored_views else 'RIGHTARROW'
            h_stored.prop(props, "show_stored_views", text="存储视图", icon=icon_stored, emboss=False)


classes = (
    ShadingUIProps,
    SHADING_OT_SetObjectColorIndex,
    SHADING_OT_RandomizeObjectColor,
    SHADING_OT_RandomizeCollectionColor,
    SHADING_OT_ResetObjectColor,
    VIEW3D_PT_QuickShadingPopover,
)

def register():
    ensure_color_icons()
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.um_shading_props = PointerProperty(type=ShadingUIProps)

    pcoll = bpy.utils.previews.new()
    addon_dir = os.path.dirname(__file__)
    icons_dir = os.path.join(addon_dir, 'icons')
    for i in range(12):
        name = f'color_{i+1:02d}'
        fp = os.path.join(icons_dir, f'{name}.png')
        if os.path.exists(fp):
            pcoll.load(name, fp, 'IMAGE')
    preview_collections["main"] = pcoll


def unregister():
    for pcoll in preview_collections.values():
        bpy.utils.previews.remove(pcoll)
    preview_collections.clear()

    if hasattr(bpy.types.Scene, 'um_shading_props'):
        del bpy.types.Scene.um_shading_props

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
