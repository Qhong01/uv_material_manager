import os
import struct
import zlib
import colorsys
import bpy
import bpy.utils.previews
from bpy.types import Operator, Panel, PropertyGroup
from bpy.props import IntProperty, BoolProperty, FloatProperty, FloatVectorProperty, EnumProperty, PointerProperty

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


DISPLAY_TYPE_ITEMS = [
    ('BOUNDS', '边界范围', '仅显示边界范围'),
    ('WIRE', '线框', '显示为线框'),
    ('SOLID', '实体', '显示为实体'),
    ('TEXTURED', '纹理', '显示为纹理')
]

def _get_target_objs(context):
    objs = [o for o in context.selected_objects if o.type in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}]
    if not objs and context.object and context.object.type in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}:
        objs = [context.object]
    return objs

def get_disp_type(self):
    obj = bpy.context.active_object
    if obj:
        for idx, item in enumerate(DISPLAY_TYPE_ITEMS):
            if item[0] == obj.display_type:
                return idx
    return 3

def set_disp_type(self, value):
    key = DISPLAY_TYPE_ITEMS[value][0]
    for o in _get_target_objs(bpy.context):
        o.display_type = key

def get_show_wire(self):
    obj = bpy.context.active_object
    return obj.show_wire if obj else False

def set_show_wire(self, value):
    for o in _get_target_objs(bpy.context):
        o.show_wire = value

def get_show_retopology(self):
    obj = bpy.context.active_object
    return getattr(obj, 'show_all_edges', False) if obj else False

def set_show_retopology(self, value):
    for o in _get_target_objs(bpy.context):
        if hasattr(o, 'show_all_edges'):
            o.show_all_edges = value
    view = bpy.context.space_data
    if hasattr(view, 'overlay') and hasattr(view.overlay, 'show_retopology'):
        view.overlay.show_retopology = value

def get_show_in_front(self):
    obj = bpy.context.active_object
    return obj.show_in_front if obj else False

def set_show_in_front(self, value):
    for o in _get_target_objs(bpy.context):
        o.show_in_front = value

def get_shadow_catcher(self):
    obj = bpy.context.active_object
    return getattr(obj, 'is_shadow_catcher', False) if obj else False

def set_shadow_catcher(self, value):
    for o in _get_target_objs(bpy.context):
        if hasattr(o, 'is_shadow_catcher'):
            o.is_shadow_catcher = value

def get_object_alpha(self):
    obj = bpy.context.active_object
    if obj and hasattr(obj, 'color') and len(obj.color) > 3:
        return obj.color[3]
    return 1.0

def set_object_alpha(self, value):
    for o in _get_target_objs(bpy.context):
        if hasattr(o, 'color'):
            c = list(o.color)
            if len(c) < 4:
                c = c + [1.0] * (4 - len(c))
            c[3] = value
            o.color = tuple(c)
    for a in bpy.context.screen.areas:
        if a.type == 'VIEW_3D':
            a.tag_redraw()

def get_object_color(self):
    obj = bpy.context.active_object
    if obj and hasattr(obj, 'color'):
        return obj.color
    return (1.0, 1.0, 1.0, 1.0)

def set_object_color(self, value):
    for o in _get_target_objs(bpy.context):
        if hasattr(o, 'color'):
            o.color = tuple(value)
    for a in bpy.context.screen.areas:
        if a.type == 'VIEW_3D':
            for space in a.spaces:
                if space.type == 'VIEW_3D' and hasattr(space, 'shading'):
                    space.shading.color_type = 'OBJECT'
            a.tag_redraw()


class ShadingUIProps(PropertyGroup):
    show_viewport_shading: BoolProperty(name="视图着色方式", default=True)
    show_object_shading: BoolProperty(name="物体着色", default=True)

    matcap_icon_scale: FloatProperty(
        name="图标缩放",
        description="调节点击快照材质/棚灯时弹出的球体图标缩放比例",
        default=2.8,
        min=1.5,
        max=4.5,
        precision=1
    )

    object_display_type: EnumProperty(name="显示为", items=DISPLAY_TYPE_ITEMS, get=get_disp_type, set=set_disp_type)
    object_show_wire: BoolProperty(name="线框", get=get_show_wire, set=set_show_wire)
    object_show_retopology: BoolProperty(name="重拓扑", get=get_show_retopology, set=set_show_retopology)
    object_show_in_front: BoolProperty(name="在前面", get=get_show_in_front, set=set_show_in_front)
    object_is_shadow_catcher: BoolProperty(name="阴影捕捉", get=get_shadow_catcher, set=set_shadow_catcher)

    object_alpha: FloatProperty(name="透明", min=0.0, max=1.0, precision=3, step=1, get=get_object_alpha, set=set_object_alpha)
    object_color: FloatVectorProperty(name="视口颜色", subtype='COLOR', size=4, min=0.0, max=1.0, get=get_object_color, set=set_object_color)


class SHADING_OT_SetObjectColorIndex(Operator):
    bl_idname = "shading.set_object_color_index"
    bl_label = "设置视口颜色"
    bl_description = "将所选预设纯色应用到所有选中物体的视口颜色"
    bl_options = {'REGISTER', 'UNDO'}

    color_index: IntProperty(default=0)

    def execute(self, context):
        if 0 <= self.color_index < len(PRESET_COLORS):
            r, g, b = PRESET_COLORS[self.color_index]
            objs = _get_target_objs(context)

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
        objs = _get_target_objs(context)

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
        objs = _get_target_objs(context)

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
        objs = _get_target_objs(context)
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
    bl_ui_units_x = 12

    def draw(self, context):
        layout = self.layout
        space = context.space_data
        shading = getattr(space, 'shading', None)
        obj = context.active_object
        props = getattr(context.scene, 'um_shading_props', None)
        if not props:
            return

        # =========================================================================
        # 1. 视图着色方式 (可折叠)
        # =========================================================================
        box_view = layout.box()
        h_view = box_view.row(align=True)
        icon_view = 'DOWNARROW_HLT' if props.show_viewport_shading else 'RIGHTARROW'
        h_view.prop(props, "show_viewport_shading", text="视图着色方式", icon=icon_view, emboss=False)

        if props.show_viewport_shading and shading:
            # 光照：横排三按钮平铺
            row_light = box_view.row(align=True)
            row_light.prop_enum(shading, "light", 'STUDIO', text="棚灯")
            row_light.prop_enum(shading, "light", 'MATCAP', text="快照材质")
            row_light.prop_enum(shading, "light", 'FLAT', text="平面")

            if shading.light in {'STUDIO', 'MATCAP'}:
                sub = box_view.row(align=True)
                sub.scale_y = 0.65
                sub.template_icon_view(shading, "studio_light", scale_popup=props.matcap_icon_scale)

            # 线框颜色：横排三按钮平铺
            if hasattr(shading, 'wireframe_color_type'):
                row_wire = box_view.row(align=True)
                row_wire.prop_enum(shading, "wireframe_color_type", 'THEME', text="主题")
                row_wire.prop_enum(shading, "wireframe_color_type", 'OBJECT', text="物体")
                row_wire.prop_enum(shading, "wireframe_color_type", 'RANDOM', text="随机")

            # 颜色：两行三列紧凑网格
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
            row_bg = box_view.row(align=True)
            row_bg.prop_enum(shading, "background_type", 'THEME', text="主题")
            row_bg.prop_enum(shading, "background_type", 'WORLD', text="世界")
            row_bg.prop_enum(shading, "background_type", 'VIEWPORT', text="自定义")

            if shading.background_type == 'VIEWPORT':
                box_view.prop(shading, "background_color", text="")

            # 选项：一整排平铺四个选项，无需下拉折叠框
            row_opts = box_view.row(align=True)
            row_opts.prop(shading, "show_backface_culling", text="背面剔除", toggle=True)
            row_opts.prop(shading, "show_shadows", text="阴影", toggle=True)
            row_opts.prop(shading, "show_cavity", text="空腔/曲率", toggle=True)
            row_opts.prop(shading, "show_object_outline", text="轮廓线", toggle=True)

        # =========================================================================
        # 2. 物体着色 (可折叠)
        # =========================================================================
        box_obj = layout.box()
        h_obj = box_obj.row(align=True)
        icon_obj = 'DOWNARROW_HLT' if props.show_object_shading else 'RIGHTARROW'
        h_obj.prop(props, "show_object_shading", text="物体着色", icon=icon_obj, emboss=False)

        if props.show_object_shading and obj:
            # 单排四按钮：线框、重拓扑、在前面、阴影捕捉 (同时控制全部选中物体)
            row_four = box_obj.row(align=True)
            row_four.prop(props, "object_show_wire", text="线框", toggle=True)
            row_four.prop(props, "object_show_retopology", text="重拓扑", toggle=True)
            row_four.prop(props, "object_show_in_front", text="在前面", toggle=True)
            row_four.prop(props, "object_is_shadow_catcher", text="阴影捕捉", toggle=True)

            # 显示为 (横排四按钮平铺，同时设置全部选中物体)
            row_disp = box_obj.row(align=True)
            row_disp.prop(props, "object_display_type", expand=True)

            # 视口颜色 (12 色纯色方块调色板)
            pcoll = preview_collections.get("main")
            pal_row = box_obj.row(align=True)
            pal_row.scale_y = 0.95
            for i in range(12):
                icon_id = pcoll[f"color_{i+1:02d}"].icon_id if pcoll and f"color_{i+1:02d}" in pcoll else 0
                btn = pal_row.operator("shading.set_object_color_index", text="", icon_value=icon_id)
                btn.color_index = i

            # 随机化颜色工具
            col_tools = box_obj.column(align=True)
            col_tools.operator("shading.randomize_object_color", text="随机化颜色", icon='COLOR')
            col_tools.operator("shading.randomize_collection_color", text="为物体集合添加随机颜色", icon='GROUP')

            # 透明度行 (同时控制所有选中物体)
            alpha_row = box_obj.row(align=True)
            alpha_row.prop(props, "object_alpha", text="透明", slider=True)
            alpha_row.prop(props, "object_color", text="")
            alpha_row.operator("shading.reset_object_color", text="", icon='X')


classes = (
    ShadingUIProps,
    SHADING_OT_SetObjectColorIndex,
    SHADING_OT_RandomizeObjectColor,
    SHADING_OT_RandomizeCollectionColor,
    SHADING_OT_ResetObjectColor,
    VIEW3D_PT_QuickShadingPopover,
)

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

def register():
    ensure_color_icons()
    for cls in classes:
        safe_register_class(cls)
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
        safe_unregister_class(cls)