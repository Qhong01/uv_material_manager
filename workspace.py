import os
import json
import time
import bpy
from bpy.types import Operator, PropertyGroup
from bpy.props import (
    StringProperty,
    EnumProperty,
    FloatProperty,
    CollectionProperty,
    BoolProperty,
    IntProperty,
)
from bpy.app.handlers import persistent

CONFIG_FILENAME = "layout_config.json"

EDITOR_ITEMS = [
    ('UV', 'UV 编辑器', 'UV 编辑器', 'UV', 0),
    ('SHADER', '着色节点', '着色节点编辑器', 'NODETREE', 1),
    ('GEOMETRY', '几何节点', '几何节点编辑器', 'GEOMETRY_NODES', 2),
    ('ASSETS', '资产浏览器', '资产库与资产浏览器', 'ASSET_MANAGER', 3),
    ('FILES', '文件浏览器', '系统文件浏览器', 'FILE_FOLDER', 4),
    ('TIMELINE', '时间轴/摄影表', '时间轴与摄影表', 'TIME', 5),
    ('GRAPH', '曲线编辑器', '曲线图编辑器', 'GRAPH', 6),
    ('OUTLINER', '大纲视图', '大纲视图', 'OUTLINER', 7),
    ('PROPERTIES', '属性视图', '属性面板视图', 'PROPERTIES', 8),
    ('TEXT', '文本编辑器', 'Python与文本编辑器', 'TEXT', 9),
    ('COMPOSITOR', '合成节点', '合成器节点编辑器', 'NODE_COMPOSITING', 10),
]

DIRECTION_ITEMS = [
    ('LEFT', '左侧', '在左侧切出副屏', 'BACK', 0),
    ('RIGHT', '右侧', '在右侧切出副屏', 'FORWARD', 1),
    ('TOP', '顶部', '在顶部切出副屏', 'TRIA_UP', 2),
    ('BOTTOM', '底部', '在底部切出副屏', 'TRIA_DOWN', 3),
]

EDITOR_DEFAULT_NAMES = {
    'UV': '切换UV',
    'SHADER': '切换材质',
    'GEOMETRY': '几何节点',
    'ASSETS': '资产库',
    'FILES': '文件浏览',
    'TIMELINE': '时间轴',
    'GRAPH': '曲线编辑',
    'OUTLINER': '大纲视图',
    'PROPERTIES': '属性面板',
    'TEXT': '文本代码',
    'COMPOSITOR': '合成节点',
}

def get_config_filepath():
    addon_dir = os.path.dirname(__file__)
    return os.path.join(addon_dir, CONFIG_FILENAME)

def load_json_config():
    fp = get_config_filepath()
    if os.path.exists(fp):
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception as e:
            print(f"[UV_Material_Manager] 加载布局配置失败: {e}")
    return []

def save_json_config(items_data):
    fp = get_config_filepath()
    try:
        with open(fp, 'w', encoding='utf-8') as f:
            json.dump(items_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[UV_Material_Manager] 保存布局配置失败: {e}")

def save_scene_layouts_to_json(scene):
    if not scene or not hasattr(scene, 'um_custom_layouts'):
        return
    items_data = []
    for item in scene.um_custom_layouts:
        items_data.append({
            'id': item.id,
            'name': item.name,
            'editor_type': item.editor_type,
            'direction': item.direction,
            'ratio': item.ratio,
        })
    save_json_config(items_data)

def on_property_updated(self, context):
    if context and hasattr(context, 'scene') and context.scene:
        save_scene_layouts_to_json(context.scene)

def on_editor_type_updated(self, context):
    if not self.name or self.name == "新视图" or any(self.name == v for v in EDITOR_DEFAULT_NAMES.values()):
        self.name = EDITOR_DEFAULT_NAMES.get(self.editor_type, "新视图")
    if context and hasattr(context, 'scene') and context.scene:
        save_scene_layouts_to_json(context.scene)


class CustomLayoutItem(PropertyGroup):
    id: StringProperty(name="ID", default="")
    name: StringProperty(name="名称", default="新视图", update=on_property_updated)
    editor_type: EnumProperty(name="视图类型", items=EDITOR_ITEMS, default='UV', update=on_editor_type_updated)
    direction: EnumProperty(name="方位", items=DIRECTION_ITEMS, default='LEFT', update=on_property_updated)
    ratio: FloatProperty(name="占比", default=0.5, min=0.15, max=0.85, step=5, precision=2, subtype='FACTOR', update=on_property_updated)


def sync_layouts_to_scene(scene):
    if not scene or not hasattr(scene, 'um_custom_layouts'):
        return
    scene.um_custom_layouts.clear()
    data = load_json_config()
    for d in data:
        item = scene.um_custom_layouts.add()
        item.id = d.get('id', str(int(time.time() * 1000)))
        item.name = d.get('name', '新视图')
        item.editor_type = d.get('editor_type', 'UV')
        item.direction = d.get('direction', 'LEFT')
        item.ratio = d.get('ratio', 0.5)


@persistent
def on_load_post_sync(dummy):
    try:
        if bpy.context.scene:
            sync_layouts_to_scene(bpy.context.scene)
    except Exception as e:
        print(f"[UV_Material_Manager] 场景加载同步失败: {e}")


def get_editor_icon(editor_type):
    for item in EDITOR_ITEMS:
        if item[0] == editor_type:
            return item[3]
    return 'WINDOW'


def close_secondary_area(context):
    wm = context.window_manager
    secondary_ptr = getattr(wm, "um_secondary_area_ptr", "")
    wm.um_secondary_area_ptr = ""
    wm.um_active_layout_id = ""

    screen = context.screen
    if not screen:
        return False

    target_area = None
    if secondary_ptr:
        for a in screen.areas:
            if hex(a.as_pointer()) == secondary_ptr:
                target_area = a
                break

    if target_area:
        try:
            with context.temp_override(window=context.window, area=target_area):
                bpy.ops.screen.area_close()
            return True
        except Exception as e:
            print(f"[UV_Material_Manager] 关闭副屏失败: {e}")

    return False


def configure_area_space(area, editor_type):
    if editor_type == 'UV':
        area.type = 'IMAGE_EDITOR'
        space = area.spaces.active
        if hasattr(space, 'mode'):
            space.mode = 'UV'
    elif editor_type == 'SHADER':
        area.type = 'NODE_EDITOR'
        space = area.spaces.active
        if hasattr(space, 'tree_type'):
            space.tree_type = 'ShaderNodeTree'
    elif editor_type == 'GEOMETRY':
        area.type = 'NODE_EDITOR'
        space = area.spaces.active
        if hasattr(space, 'tree_type'):
            space.tree_type = 'GeometryNodeTree'
    elif editor_type == 'COMPOSITOR':
        area.type = 'NODE_EDITOR'
        space = area.spaces.active
        if hasattr(space, 'tree_type'):
            space.tree_type = 'CompositorNodeTree'
    elif editor_type == 'ASSETS':
        area.type = 'FILE_BROWSER'
        space = area.spaces.active
        if hasattr(space, 'browse_mode'):
            space.browse_mode = 'ASSETS'
    elif editor_type == 'FILES':
        area.type = 'FILE_BROWSER'
        space = area.spaces.active
        if hasattr(space, 'browse_mode'):
            space.browse_mode = 'FILES'
    elif editor_type == 'TIMELINE':
        area.type = 'DOPESHEET_EDITOR'
    elif editor_type == 'GRAPH':
        area.type = 'GRAPH_EDITOR'
    elif editor_type == 'OUTLINER':
        area.type = 'OUTLINER'
    elif editor_type == 'PROPERTIES':
        area.type = 'PROPERTIES'
    elif editor_type == 'TEXT':
        area.type = 'TEXT_EDITOR'


class WORKSPACE_OT_ToggleCustomLayout(Operator):
    bl_idname = "workspace.toggle_custom_layout"
    bl_label = "切换视图"
    bl_description = "智能切换该分屏视图（智能单副屏互斥，切到其他视图自动替换，再次点击恢复全屏3D视图）"
    bl_options = {'REGISTER'}

    item_id: StringProperty(default="")

    def execute(self, context):
        scene = context.scene
        wm = context.window_manager

        target_item = None
        for it in scene.um_custom_layouts:
            if it.id == self.item_id:
                target_item = it
                break

        if not target_item:
            self.report({'WARNING'}, "未找到该视图配置")
            return {'CANCELLED'}

        active_id = getattr(wm, "um_active_layout_id", "")
        secondary_ptr = getattr(wm, "um_secondary_area_ptr", "")

        # 情况 1: 如果当前点击的正是已开启的该视图 -> 关闭副屏并恢复单 3D 视图
        if active_id == self.item_id and secondary_ptr:
            close_secondary_area(context)
            self.report({'INFO'}, f"已关闭 {target_item.name}")
            return {'FINISHED'}

        # 情况 2: 如果当前已经有其他副屏打开 -> 先关闭旧副屏
        if secondary_ptr:
            close_secondary_area(context)

        # 确保当前操作的主区域是 3D 视图
        orig_3d_area = context.area if context.area and context.area.type == 'VIEW_3D' else None
        if not orig_3d_area:
            for a in context.screen.areas:
                if a.type == 'VIEW_3D':
                    orig_3d_area = a
                    break

        if not orig_3d_area:
            self.report({'ERROR'}, "未找到 3D 视图以执行分屏操作")
            return {'CANCELLED'}

        direction = target_item.direction
        ratio = max(0.15, min(0.85, target_item.ratio))

        if direction in {'LEFT', 'RIGHT'}:
            split_dir = 'VERTICAL'
            factor = ratio if direction == 'LEFT' else (1.0 - ratio)
        else:
            split_dir = 'HORIZONTAL'
            factor = ratio if direction == 'BOTTOM' else (1.0 - ratio)

        try:
            with context.temp_override(
                window=context.window,
                area=orig_3d_area,
            ):
                bpy.ops.screen.area_split(direction=split_dir, factor=factor)

            new_area = context.screen.areas[-1]

            if split_dir == 'VERTICAL':
                left_a = orig_3d_area if orig_3d_area.x < new_area.x else new_area
                right_a = new_area if orig_3d_area.x < new_area.x else orig_3d_area
                secondary_area = left_a if direction == 'LEFT' else right_a
                main_3d_area = right_a if direction == 'LEFT' else left_a
            else:
                bottom_a = orig_3d_area if orig_3d_area.y < new_area.y else new_area
                top_a = new_area if orig_3d_area.y < new_area.y else orig_3d_area
                secondary_area = bottom_a if direction == 'BOTTOM' else top_a
                main_3d_area = top_a if direction == 'BOTTOM' else bottom_a

            main_3d_area.type = 'VIEW_3D'
            configure_area_space(secondary_area, target_item.editor_type)

            wm.um_active_layout_id = target_item.id
            wm.um_secondary_area_ptr = hex(secondary_area.as_pointer())

            secondary_area.tag_redraw()
            main_3d_area.tag_redraw()

            self.report({'INFO'}, f"{target_item.name} 已打开")
            return {'FINISHED'}

        except Exception as e:
            self.report({'ERROR'}, f"分屏失败: {str(e)}")
            return {'CANCELLED'}


class WORKSPACE_OT_AddCustomLayoutItem(Operator):
    bl_idname = "workspace.add_custom_layout"
    bl_label = "添加分屏视图"
    bl_description = "添加一个新的自定义分屏视图项"
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        new_item = scene.um_custom_layouts.add()
        new_item.id = str(int(time.time() * 1000))
        new_item.editor_type = 'UV'
        new_item.name = EDITOR_DEFAULT_NAMES.get('UV', '新视图')
        new_item.direction = 'LEFT'
        new_item.ratio = 0.5

        save_scene_layouts_to_json(scene)
        scene.um_show_layout_settings = True
        self.report({'INFO'}, "已添加新视图，可在下方卡片中配置")
        return {'FINISHED'}


class WORKSPACE_OT_RemoveCustomLayoutItem(Operator):
    bl_idname = "workspace.remove_custom_layout"
    bl_label = "删除分屏视图"
    bl_description = "删除该自定义分屏视图项"
    bl_options = {'REGISTER'}

    index: IntProperty(default=0)

    def execute(self, context):
        scene = context.scene
        wm = context.window_manager

        if 0 <= self.index < len(scene.um_custom_layouts):
            item = scene.um_custom_layouts[self.index]
            if getattr(wm, "um_active_layout_id", "") == item.id:
                close_secondary_area(context)
            scene.um_custom_layouts.remove(self.index)
            save_scene_layouts_to_json(scene)
            self.report({'INFO'}, "已删除视图")
            return {'FINISHED'}

        return {'CANCELLED'}


classes = (
    CustomLayoutItem,
    WORKSPACE_OT_ToggleCustomLayout,
    WORKSPACE_OT_AddCustomLayoutItem,
    WORKSPACE_OT_RemoveCustomLayoutItem,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.um_custom_layouts = CollectionProperty(type=CustomLayoutItem)
    bpy.types.Scene.um_show_layout_settings = BoolProperty(name="显示视图配置", default=False)

    bpy.types.WindowManager.um_active_layout_id = StringProperty(name="活动视图ID", default="")
    bpy.types.WindowManager.um_secondary_area_ptr = StringProperty(name="副屏区域指针", default="")

    if on_load_post_sync not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(on_load_post_sync)

    if bpy.context.scene:
        sync_layouts_to_scene(bpy.context.scene)


def unregister():
    if on_load_post_sync in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(on_load_post_sync)

    if hasattr(bpy.types.WindowManager, 'um_secondary_area_ptr'):
        del bpy.types.WindowManager.um_secondary_area_ptr
    if hasattr(bpy.types.WindowManager, 'um_active_layout_id'):
        del bpy.types.WindowManager.um_active_layout_id

    if hasattr(bpy.types.Scene, 'um_show_layout_settings'):
        del bpy.types.Scene.um_show_layout_settings
    if hasattr(bpy.types.Scene, 'um_custom_layouts'):
        del bpy.types.Scene.um_custom_layouts

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)