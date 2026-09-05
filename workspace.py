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

CONFIG_FILENAME = "layout_config.json"
STATE_CONFIG_FILENAME = "layout_sidebar_states.json"

def get_config_filepath():
    addon_dir = os.path.dirname(__file__)
    return os.path.join(addon_dir, CONFIG_FILENAME)

def get_state_config_filepath():
    addon_dir = os.path.dirname(__file__)
    return os.path.join(addon_dir, STATE_CONFIG_FILENAME)

def serialize_sidebar_state_value(val):
    if isinstance(val, (bool, int, float, str)):
        return val
    elif isinstance(val, (bytes, bytearray)):
        try:
            return val.decode('utf-8', errors='ignore')
        except Exception:
            return str(val)
    elif val is None:
        return ""
    return str(val)

def load_sidebar_states_from_json():
    fp = get_state_config_filepath()
    if os.path.exists(fp):
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if content:
                    data = json.loads(content)
                    if isinstance(data, dict):
                        for k, v in data.items():
                            if isinstance(v, dict):
                                if k not in EDITOR_SIDEBAR_STATES:
                                    EDITOR_SIDEBAR_STATES[k] = {}
                                EDITOR_SIDEBAR_STATES[k].update(v)
        except Exception as e:
            print(f"[UV_Material_Manager] 加载侧边栏状态失败: {e}")

def save_sidebar_states_to_json():
    fp = get_state_config_filepath()
    tmp_fp = fp + ".tmp"
    try:
        clean_data = {}
        for k, state in EDITOR_SIDEBAR_STATES.items():
            if isinstance(state, dict):
                clean_data[str(k)] = {str(prop_k): serialize_sidebar_state_value(prop_v) for prop_k, prop_v in state.items()}
            else:
                clean_data[str(k)] = serialize_sidebar_state_value(state)
        with open(tmp_fp, 'w', encoding='utf-8') as f:
            json.dump(clean_data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(fp):
            os.replace(tmp_fp, fp)
        else:
            os.rename(tmp_fp, fp)
    except Exception as e:
        print(f"[UV_Material_Manager] 保存侧边栏状态失败: {e}")
        if os.path.exists(tmp_fp):
            try:
                os.remove(tmp_fp)
            except Exception:
                pass

def load_json_config():
    fp = get_config_filepath()
    if os.path.exists(fp):
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if content:
                    data = json.loads(content)
                    if isinstance(data, list):
                        return data
        except Exception as e:
            print(f"[UV_Material_Manager] 加载布局配置失败: {e}")
    return []

def save_json_config(items_data):
    fp = get_config_filepath()
    tmp_fp = fp + ".tmp"
    try:
        with open(tmp_fp, 'w', encoding='utf-8') as f:
            json.dump(items_data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(fp):
            os.replace(tmp_fp, fp)
        else:
            os.rename(tmp_fp, fp)
    except Exception as e:
        print(f"[UV_Material_Manager] 保存布局配置失败: {e}")
        if os.path.exists(tmp_fp):
            try:
                os.remove(tmp_fp)
            except Exception:
                pass

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


class UVMM_CustomLayoutItem(PropertyGroup):
    id: StringProperty(name="ID", default="")
    name: StringProperty(name="名称", default="新视图", update=on_property_updated)
    editor_type: EnumProperty(name="视图类型", items=EDITOR_ITEMS, default='UV', update=on_editor_type_updated)
    direction: EnumProperty(name="方位", items=DIRECTION_ITEMS, default='LEFT', update=on_property_updated)
    ratio: FloatProperty(name="占比", default=0.4, min=0.15, max=0.85, step=5, precision=2, subtype='FACTOR', update=on_property_updated)


def sync_layouts_to_scene(scene):
    load_sidebar_states_from_json()
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
        item.ratio = d.get('ratio', 0.4)


@persistent
def on_load_post_sync(dummy):
    try:
        load_sidebar_states_from_json()
        if hasattr(bpy, "data") and hasattr(bpy.data, "scenes"):
            for sc in bpy.data.scenes:
                sync_layouts_to_scene(sc)
    except Exception as e:
        print(f"[UV_Material_Manager] 场景加载同步失败: {e}")


def initial_sync_timer():
    try:
        load_sidebar_states_from_json()
        if hasattr(bpy, "data") and hasattr(bpy.data, "scenes"):
            for sc in bpy.data.scenes:
                sync_layouts_to_scene(sc)
    except Exception as e:
        print(f"[UV_Material_Manager] 初始同步失败: {e}")
    return None


def get_editor_icon(editor_type):
    for item in EDITOR_ITEMS:
        if item[0] == editor_type:
            return item[3]
    return 'WINDOW'


def get_safe_window_and_screen(context):
    window = getattr(context, "window", None)
    if not window and hasattr(context, "window_manager") and context.window_manager:
        if len(context.window_manager.windows) > 0:
            window = context.window_manager.windows[0]
    if not window and hasattr(bpy.data, "window_managers") and len(bpy.data.window_managers) > 0:
        if len(bpy.data.window_managers[0].windows) > 0:
            window = bpy.data.window_managers[0].windows[0]

    screen = getattr(context, "screen", None)
    if not screen and window:
        screen = window.screen
    if not screen and hasattr(bpy.data, "screens") and len(bpy.data.screens) > 0:
        screen = bpy.data.screens[0]

    return window, screen


def find_area_by_pointer(screen, area_ptr):
    if not screen or not area_ptr:
        return None
    try:
        for a in screen.areas:
            try:
                if hex(a.as_pointer()) == area_ptr:
                    return a
            except Exception:
                pass
    except Exception:
        pass
    return None

def get_or_detect_secondary_area(screen, secondary_ptr):
    if not screen:
        return None
    if secondary_ptr:
        a = find_area_by_pointer(screen, secondary_ptr)
        if a:
            return a
    # 智能查找屏幕中已存在的非3D工作区副屏（排除通用面板如属性、大纲、信息等）
    candidates = [
        a for a in screen.areas
        if a.type not in {'VIEW_3D', 'PROPERTIES', 'OUTLINER', 'INFO', 'TOPBAR', 'STATUSBAR', 'PREFERENCES'}
    ]
    if candidates:
        return candidates[0]
    return None

def close_secondary_area(context):
    global LAYOUT_OPERATION_GEN
    LAYOUT_OPERATION_GEN += 1

    wm = context.window_manager
    secondary_ptr = getattr(wm, "um_secondary_area_ptr", "")
    active_id = getattr(wm, "um_active_layout_id", "")
    wm.um_secondary_area_ptr = ""
    wm.um_active_layout_id = ""
    if hasattr(wm, "um_secondary_direction"):
        wm.um_secondary_direction = ""

    window, screen = get_safe_window_and_screen(context)
    if not screen:
        return False

    target_area = get_or_detect_secondary_area(screen, secondary_ptr)

    if target_area:
        if active_id:
            capture_area_sidebar_state(context, target_area, active_id)
            if hasattr(context, "scene") and hasattr(context.scene, "um_custom_layouts"):
                current_it = next((it for it in context.scene.um_custom_layouts if it.id == active_id), None)
                if current_it:
                    capture_area_sidebar_state(context, target_area, current_it.editor_type)
        else:
            capture_area_sidebar_state(context, target_area, target_area.type)

    if target_area and window:
        try:
            with context.temp_override(window=window, screen=screen, area=target_area):
                bpy.ops.screen.area_close()
        except Exception as e:
            print(f"[UV_Material_Manager] 关闭副屏提示: {e}")

    # 保护机制：无论 Blender area_close 后如何合并，确保当前屏幕存在 VIEW_3D 区域
    has_3d = any(a.type == 'VIEW_3D' for a in screen.areas)
    if not has_3d:
        candidates = [a for a in screen.areas if a.type not in {'PROPERTIES', 'OUTLINER', 'INFO'}]
        if candidates:
            largest = max(candidates, key=lambda a: a.width * a.height)
            largest.type = 'VIEW_3D'
        elif screen.areas:
            screen.areas[0].type = 'VIEW_3D'

    return True


LAYOUT_OPERATION_GEN = 0

EDITOR_SIDEBAR_STATES = {
    'UV': {
        'show_region_ui': True,
        'show_region_toolbar': False,
        'active_panel_category': 'UV Tools',
    },
    'SHADER': {
        'show_region_ui': True,
        'show_region_toolbar': False,
        'active_panel_category': 'Item',
    },
    'GEOMETRY': {
        'show_region_ui': True,
        'show_region_toolbar': False,
        'active_panel_category': 'Item',
    },
    'COMPOSITOR': {
        'show_region_ui': True,
        'show_region_toolbar': False,
        'active_panel_category': 'Item',
    },
    'ASSETS': {
        'show_region_ui': True,
        'show_region_toolbar': True,
        'browse_mode': 'ASSETS',
        'asset_library_reference': 'ALL',
        'catalog_id': '',
        'active_panel_category': '',
    },
    'FILES': {
        'show_region_ui': False,
        'show_region_toolbar': True,
        'browse_mode': 'FILES',
        'active_panel_category': '',
    },
    'TIMELINE': {
        'show_region_ui': True,
        'show_region_toolbar': False,
        'show_region_channels': True,
        'active_panel_category': 'View',
    },
    'GRAPH': {
        'show_region_ui': True,
        'show_region_toolbar': False,
        'show_region_channels': True,
        'active_panel_category': 'View',
    },
    'OUTLINER': {
        'show_region_ui': False,
        'show_region_toolbar': False,
        'display_mode': 'VIEW_LAYER',
    },
    'PROPERTIES': {
        'show_region_ui': False,
        'show_region_toolbar': False,
        'properties_context': 'MODIFIER',
    },
    'TEXT': {
        'show_region_ui': True,
        'show_region_toolbar': False,
        'active_panel_category': 'Text',
    },
}

def get_region_active_category(context, area, region):
    if not region or region.type != 'UI':
        return None
    window, screen = get_safe_window_and_screen(context)
    if window and screen and area:
        try:
            with context.temp_override(window=window, screen=screen, area=area, region=region):
                c = getattr(region, "active_panel_category", None)
                if c and c != 'UNSUPPORTED':
                    return c
        except Exception:
            pass
    if area:
        try:
            with context.temp_override(area=area, region=region):
                c = getattr(region, "active_panel_category", None)
                if c and c != 'UNSUPPORTED':
                    return c
        except Exception:
            pass
    try:
        c = getattr(region, "active_panel_category", None)
        if c and c != 'UNSUPPORTED':
            return c
    except Exception:
        pass
    return None

def capture_area_sidebar_state(context, area, key):
    if not area or not key:
        return
    try:
        state_dict = {}
        old_state = EDITOR_SIDEBAR_STATES.get(key, {})

        space = getattr(area, 'spaces', None)
        active_space = space.active if space else None
        if active_space:
            if hasattr(active_space, 'show_region_ui'):
                state_dict['show_region_ui'] = active_space.show_region_ui
            if hasattr(active_space, 'show_region_toolbar'):
                state_dict['show_region_toolbar'] = active_space.show_region_toolbar
            if hasattr(active_space, 'show_region_channels'):
                state_dict['show_region_channels'] = active_space.show_region_channels
            if hasattr(active_space, 'context'):
                state_dict['properties_context'] = active_space.context
            if hasattr(active_space, 'display_mode'):
                state_dict['display_mode'] = active_space.display_mode
            if hasattr(active_space, 'browse_mode'):
                state_dict['browse_mode'] = active_space.browse_mode
            if hasattr(active_space, 'params') and active_space.params:
                p = active_space.params
                if hasattr(p, 'asset_library_reference'):
                    try:
                        state_dict['asset_library_reference'] = p.asset_library_reference
                    except Exception:
                        pass
                if hasattr(p, 'catalog_id'):
                    try:
                        cid = p.catalog_id
                        old_cid = old_state.get('catalog_id')
                        if cid and cid != "00000000-0000-0000-0000-000000000000":
                            state_dict['catalog_id'] = cid
                        elif old_cid and old_cid != "00000000-0000-0000-0000-000000000000":
                            state_dict['catalog_id'] = old_cid
                        elif cid:
                            state_dict['catalog_id'] = cid
                    except Exception:
                        pass
                if hasattr(p, 'import_method'):
                    try:
                        state_dict['import_method'] = p.import_method
                    except Exception:
                        pass
                if hasattr(p, 'display_type'):
                    try:
                        state_dict['display_type'] = p.display_type
                    except Exception:
                        pass
                if hasattr(p, 'filter_search'):
                    try:
                        state_dict['filter_search'] = p.filter_search
                    except Exception:
                        pass
                if hasattr(p, 'directory'):
                    try:
                        state_dict['directory'] = p.directory
                    except Exception:
                        pass

        cat = None
        for r in area.regions:
            if r.type == 'UI':
                c = get_region_active_category(context, area, r)
                if c:
                    cat = c
                    break

        if cat:
            state_dict['active_panel_category'] = cat
        elif old_state.get('active_panel_category'):
            state_dict['active_panel_category'] = old_state.get('active_panel_category')

        merged_state = dict(old_state)
        merged_state.update(state_dict)
        EDITOR_SIDEBAR_STATES[key] = merged_state

        # 同步更新对应编辑器的通用类型状态（例如 ASSETS, UV, SHADER 等）
        type_key = None
        if area.type == 'FILE_BROWSER':
            browse_mode = getattr(active_space, 'browse_mode', 'ASSETS')
            type_key = 'ASSETS' if browse_mode == 'ASSETS' else 'FILES'
        elif area.type == 'IMAGE_EDITOR':
            type_key = 'UV'
        elif area.type == 'NODE_EDITOR':
            tree_type = getattr(active_space, 'tree_type', '')
            if tree_type == 'ShaderNodeTree':
                type_key = 'SHADER'
            elif tree_type == 'GeometryNodeTree':
                type_key = 'GEOMETRY'
            elif tree_type == 'CompositorNodeTree':
                type_key = 'COMPOSITOR'
        elif area.type == 'DOPESHEET_EDITOR':
            type_key = 'TIMELINE'
        elif area.type == 'GRAPH_EDITOR':
            type_key = 'GRAPH'
        elif area.type == 'OUTLINER':
            type_key = 'OUTLINER'
        elif area.type == 'PROPERTIES':
            type_key = 'PROPERTIES'
        elif area.type == 'TEXT_EDITOR':
            type_key = 'TEXT'

        if type_key and type_key != key:
            type_old_state = EDITOR_SIDEBAR_STATES.get(type_key, {})
            type_merged = dict(type_old_state)
            type_merged.update(state_dict)
            EDITOR_SIDEBAR_STATES[type_key] = type_merged

        save_sidebar_states_to_json()
    except Exception:
        pass

def try_set_category(context, area, region, category):
    if not category or not region or not area:
        return False
    window, screen = get_safe_window_and_screen(context)

    candidates = [category]
    translations = {
        "UV Tools": "UV 工具",
        "UV 工具": "UV Tools",
        "Item": "条目",
        "条目": "Item",
        "Tool": "工具",
        "工具": "Tool",
        "View": "视图",
        "视图": "View",
        "Node": "节点",
        "节点": "Node",
        "Modifiers": "修改器",
        "修改器": "Modifiers",
        "Edit": "编辑",
        "编辑": "Edit",
    }
    if category in translations and translations[category] not in candidates:
        candidates.append(translations[category])

    for cat in candidates:
        try:
            with context.temp_override(window=window, screen=screen, area=area, region=region):
                region.active_panel_category = cat
                return True
        except Exception:
            pass
        try:
            with context.temp_override(area=area, region=region):
                region.active_panel_category = cat
                return True
        except Exception:
            pass
        try:
            region.active_panel_category = cat
            return True
        except Exception:
            pass

    return False

def restore_area_sidebar_state(context, area, key, editor_type=None):
    global LAYOUT_OPERATION_GEN
    LAYOUT_OPERATION_GEN += 1
    current_gen = LAYOUT_OPERATION_GEN

    if not area:
        return

    # 获取当前配置状态，并自动使用类型默认配置做属性补全
    state = dict(EDITOR_SIDEBAR_STATES.get(key, {}))
    fallback_state = EDITOR_SIDEBAR_STATES.get(editor_type, {}) if editor_type else {}
    for k, v in fallback_state.items():
        if k not in state or state[k] is None or state[k] == "":
            state[k] = v

    target_show_ui = state.get('show_region_ui', True)
    target_show_toolbar = state.get('show_region_toolbar', None)
    target_show_channels = state.get('show_region_channels', None)
    target_props_context = state.get('properties_context', None)
    target_display_mode = state.get('display_mode', None)
    target_asset_lib = state.get('asset_library_reference', None)
    target_catalog_id = state.get('catalog_id', None)
    target_import_method = state.get('import_method', None)
    target_display_type = state.get('display_type', None)
    target_filter_search = state.get('filter_search', None)
    category = state.get('active_panel_category', None)

    is_asset_mode = (editor_type == 'ASSETS' or key == 'ASSETS' or target_catalog_id or target_asset_lib)

    area_ptr = hex(area.as_pointer()) if area else ""
    if not area_ptr:
        return

    attempts = 0
    def apply_cat():
        nonlocal attempts
        attempts += 1
        if current_gen != LAYOUT_OPERATION_GEN:
            return None

        ui_applied = False
        cat_applied = False
        asset_applied = not is_asset_mode

        try:
            ctx = bpy.context
            window, screen = get_safe_window_and_screen(ctx)
            valid_area = find_area_by_pointer(screen, area_ptr)
            if not valid_area:
                return None  # 该区域已被关闭或销毁，安全退出定时器！

            act_space = getattr(valid_area, 'spaces', None)
            if act_space and act_space.active:
                sp = act_space.active
                # 1. 恢复各区域显示状态 (N面板、T工具栏/目录树、通道栏等)
                if hasattr(sp, 'show_region_ui') and target_show_ui is not None:
                    if sp.show_region_ui != target_show_ui:
                        sp.show_region_ui = target_show_ui
                if hasattr(sp, 'show_region_toolbar') and target_show_toolbar is not None:
                    if sp.show_region_toolbar != target_show_toolbar:
                        sp.show_region_toolbar = target_show_toolbar
                if hasattr(sp, 'show_region_channels') and target_show_channels is not None:
                    if sp.show_region_channels != target_show_channels:
                        sp.show_region_channels = target_show_channels
                if hasattr(sp, 'context') and target_props_context:
                    try:
                        sp.context = target_props_context
                    except Exception:
                        pass
                if hasattr(sp, 'display_mode') and target_display_mode:
                    try:
                        sp.display_mode = target_display_mode
                    except Exception:
                        pass
                if hasattr(sp, 'browse_mode') and is_asset_mode:
                    if sp.browse_mode != 'ASSETS':
                        sp.browse_mode = 'ASSETS'

                # 处理资产浏览器参数
                if is_asset_mode:
                    if hasattr(sp, 'params') and sp.params:
                        p = sp.params
                        try:
                            p.use_library_browsing = True
                            p.use_filter_asset_only = True
                        except Exception:
                            pass

                        if target_asset_lib and hasattr(p, 'asset_library_reference'):
                            try:
                                p.asset_library_reference = target_asset_lib
                            except Exception:
                                pass

                        if target_catalog_id and hasattr(p, 'catalog_id'):
                            try:
                                p.catalog_id = target_catalog_id
                            except Exception:
                                pass

                        if target_import_method and hasattr(p, 'import_method'):
                            try:
                                p.import_method = target_import_method
                            except Exception:
                                pass

                        if target_display_type and hasattr(p, 'display_type'):
                            try:
                                p.display_type = target_display_type
                            except Exception:
                                pass

                        if target_filter_search is not None and hasattr(p, 'filter_search'):
                            try:
                                p.filter_search = target_filter_search
                            except Exception:
                                pass

                        # 验证 catalog_id 是否成功生效
                        if target_catalog_id and hasattr(p, 'catalog_id'):
                            if p.catalog_id == target_catalog_id:
                                asset_applied = True
                            else:
                                asset_applied = False
                        else:
                            asset_applied = True
                    else:
                        asset_applied = False
                else:
                    if hasattr(sp, 'params') and sp.params:
                        p = sp.params
                        if target_import_method and hasattr(p, 'import_method'):
                            try:
                                p.import_method = target_import_method
                            except Exception:
                                pass
                        if target_display_type and hasattr(p, 'display_type'):
                            try:
                                p.display_type = target_display_type
                            except Exception:
                                pass
                        if target_filter_search is not None and hasattr(p, 'filter_search'):
                            try:
                                p.filter_search = target_filter_search
                            except Exception:
                                pass

                ui_applied = True

            # 2. 如果展开侧边栏且指定了标签页，恢复激活标签页
            if target_show_ui and category:
                for r in valid_area.regions:
                    try:
                        if r.type == 'UI':
                            if try_set_category(ctx, valid_area, r, category):
                                cat_applied = True
                            r.tag_redraw()
                    except Exception:
                        pass
            else:
                cat_applied = True

            valid_area.tag_redraw()
        except Exception:
            return None

        if (ui_applied and cat_applied and asset_applied) or attempts >= 20:
            return None
        return 0.05

    try:
        bpy.app.timers.register(apply_cat, first_interval=0.03)
    except Exception:
        pass

def restore_view3d_sidebar_state(context, area, category, show_ui=None):
    global LAYOUT_OPERATION_GEN
    current_gen = LAYOUT_OPERATION_GEN

    if not area:
        return

    area_ptr = hex(area.as_pointer()) if area else ""
    if not area_ptr:
        return

    attempts = 0
    def apply_cat():
        nonlocal attempts
        attempts += 1
        if current_gen != LAYOUT_OPERATION_GEN:
            return None

        ui_applied = False
        cat_applied = False
        try:
            ctx = bpy.context
            window, screen = get_safe_window_and_screen(ctx)
            valid_area = find_area_by_pointer(screen, area_ptr)
            if not valid_area:
                return None

            if show_ui is not None:
                act_space = getattr(valid_area, 'spaces', None)
                if act_space and act_space.active and hasattr(act_space.active, 'show_region_ui'):
                    if act_space.active.show_region_ui != show_ui:
                        act_space.active.show_region_ui = show_ui
                    ui_applied = True
            else:
                ui_applied = True

            if category:
                for r in valid_area.regions:
                    try:
                        if r.type == 'UI':
                            if try_set_category(ctx, valid_area, r, category):
                                cat_applied = True
                            r.tag_redraw()
                    except Exception:
                        pass
            else:
                cat_applied = True

            valid_area.tag_redraw()
        except Exception:
            return None

        if (ui_applied and cat_applied) or attempts >= 8:
            return None
        return 0.05

    try:
        bpy.app.timers.register(apply_cat, first_interval=0.03)
    except Exception:
        pass


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


def execute_toggle_layout(self_op, context, item_id="", item_name="", editor_type="", index=-1):
    scene = context.scene
    wm = context.window_manager
    window, screen = get_safe_window_and_screen(context)

    if not screen:
        if self_op:
            self_op.report({'ERROR'}, "未找到可用窗口")
        return {'CANCELLED'}

    custom_layouts = getattr(scene, "um_custom_layouts", [])
    if len(custom_layouts) == 0:
        if self_op:
            self_op.report({'WARNING'}, "未配置任何分屏视图")
        return {'CANCELLED'}

    target_item = None
    # 1. 优先按 item_id 查找
    if item_id:
        for it in custom_layouts:
            if it.id == item_id:
                target_item = it
                break

    # 2. 按索引序号查找
    if not target_item and 0 <= index < len(custom_layouts):
        target_item = custom_layouts[index]

    # 3. 按名称查找
    if not target_item and item_name:
        for it in custom_layouts:
            if it.name == item_name:
                target_item = it
                break

    # 4. 按类型查找 (例如 UV, SHADER, ASSETS)
    if not target_item and editor_type:
        for it in custom_layouts:
            if it.editor_type == editor_type:
                target_item = it
                break

    # 5. 若无匹配且 index == -1，默认取第 1 个视图
    if not target_item and index == -1 and len(custom_layouts) > 0:
        target_item = custom_layouts[0]

    if not target_item:
        if self_op:
            self_op.report({'WARNING'}, "未找到该视图配置")
        return {'CANCELLED'}

    active_id = getattr(wm, "um_active_layout_id", "")
    secondary_ptr = getattr(wm, "um_secondary_area_ptr", "")

    # 智能查找副屏（按指针查找，或智能检测当前屏幕已存在的非3D工作区副屏）
    existing_secondary = get_or_detect_secondary_area(screen, secondary_ptr)

    if existing_secondary:
        wm.um_secondary_area_ptr = hex(existing_secondary.as_pointer())
        if active_id:
            capture_area_sidebar_state(context, existing_secondary, active_id)
            current_it = next((it for it in custom_layouts if it.id == active_id), None)
            if current_it:
                capture_area_sidebar_state(context, existing_secondary, current_it.editor_type)
        else:
            capture_area_sidebar_state(context, existing_secondary, existing_secondary.type)

    # 情况 1: 如果当前点击的正是已开启的该视图 -> 关闭副屏并恢复单 3D 视图
    is_same_view = (active_id == target_item.id) or (not active_id and existing_secondary and existing_secondary.type == target_item.editor_type)
    if is_same_view and existing_secondary:
        close_secondary_area(context)
        if self_op:
            self_op.report({'INFO'}, f"已关闭 {target_item.name}")
        return {'FINISHED'}

    # 情况 2: 副屏已经存在，且用户点击了其他视图 -> 直接在已有副屏中切换模式！
    if existing_secondary:
        curr_direction = getattr(wm, "um_secondary_direction", "")
        if not curr_direction or curr_direction == target_item.direction:
            # 相同方位：直接无缝切换视图空间类型，零延迟零报错
            configure_area_space(existing_secondary, target_item.editor_type)
            restore_area_sidebar_state(context, existing_secondary, target_item.id, target_item.editor_type)
            wm.um_active_layout_id = target_item.id
            wm.um_secondary_direction = target_item.direction
            existing_secondary.tag_redraw()
            if self_op:
                self_op.report({'INFO'}, f"已切换至: {target_item.name}")
            return {'FINISHED'}
        else:
            # 不同方位：先关闭当前副屏，再在新方位分屏
            close_secondary_area(context)

    # 情况 3: 副屏不存在（首次打开或不同方位重新打开）
    orig_3d_area = context.area if context.area and context.area.type == 'VIEW_3D' else None
    if not orig_3d_area:
        view3d_areas = [a for a in screen.areas if a.type == 'VIEW_3D']
        if view3d_areas:
            orig_3d_area = max(view3d_areas, key=lambda a: a.width * a.height)

    if not orig_3d_area:
        if self_op:
            self_op.report({'ERROR'}, "未找到 3D 视图以执行分屏操作")
        return {'CANCELLED'}

    main_3d_cat = None
    main_3d_show_ui = True
    try:
        if orig_3d_area.spaces and orig_3d_area.spaces.active:
            main_3d_show_ui = getattr(orig_3d_area.spaces.active, 'show_region_ui', True)
        for r in orig_3d_area.regions:
            try:
                if r.type == 'UI':
                    c = getattr(r, "active_panel_category", None)
                    if c and c != 'UNSUPPORTED':
                        main_3d_cat = c
                    break
            except Exception:
                pass
    except Exception:
        pass

    direction = target_item.direction
    ratio = max(0.15, min(0.85, target_item.ratio))

    if direction in {'LEFT', 'RIGHT'}:
        split_dir = 'VERTICAL'
        factor = ratio if direction == 'LEFT' else (1.0 - ratio)
    else:
        split_dir = 'HORIZONTAL'
        factor = ratio if direction == 'BOTTOM' else (1.0 - ratio)

    try:
        areas_before = list(screen.areas)
        with context.temp_override(
            window=window,
            screen=screen,
            area=orig_3d_area,
        ):
            bpy.ops.screen.area_split(direction=split_dir, factor=factor)

        new_area = None
        for a in screen.areas:
            if a not in areas_before:
                new_area = a
                break
        if not new_area:
            new_area = screen.areas[-1]

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
        restore_area_sidebar_state(context, secondary_area, target_item.id, target_item.editor_type)
        if main_3d_cat or main_3d_show_ui is not None:
            restore_view3d_sidebar_state(context, main_3d_area, main_3d_cat, main_3d_show_ui)

        wm.um_active_layout_id = target_item.id
        wm.um_secondary_area_ptr = hex(secondary_area.as_pointer())
        wm.um_secondary_direction = direction

        secondary_area.tag_redraw()
        main_3d_area.tag_redraw()

        if self_op:
            self_op.report({'INFO'}, f"{target_item.name} 已打开")
        return {'FINISHED'}

    except Exception as e:
        if self_op:
            self_op.report({'ERROR'}, f"分屏失败: {str(e)}")
        return {'CANCELLED'}


class WORKSPACE_OT_ToggleCustomLayout(Operator):
    bl_idname = "view3d.toggle_custom_layout"
    bl_label = "切换分屏视图"
    bl_description = "智能切换该分屏视图（智能单副屏互斥，切到其他视图自动替换，再次点击恢复全屏3D视图）"
    bl_options = {'REGISTER', 'UNDO'}

    item_id: StringProperty(name="视图ID", default="")
    item_name: StringProperty(name="视图名称", default="")
    editor_type: StringProperty(name="视图类型", default="")
    index: IntProperty(name="视图索引", default=-1)

    def execute(self, context):
        return execute_toggle_layout(
            self,
            context,
            item_id=self.item_id,
            item_name=self.item_name,
            editor_type=self.editor_type,
            index=self.index
        )


def make_slot_operator(slot_idx):
    slot_num = slot_idx + 1
    class WORKSPACE_OT_ToggleSlotLayout(Operator):
        bl_idname = f"view3d.toggle_custom_layout_{slot_num}"
        bl_label = f"切换分屏视图项 {slot_num}"
        bl_description = f"切换第 {slot_num} 个自定义分屏视图"
        bl_options = {'REGISTER', 'UNDO'}

        def execute(self, context):
            return execute_toggle_layout(self, context, index=slot_idx)

    WORKSPACE_OT_ToggleSlotLayout.__name__ = f"WORKSPACE_OT_ToggleCustomLayoutSlot_{slot_num}"
    return WORKSPACE_OT_ToggleSlotLayout

SLOT_OPERATORS = [make_slot_operator(i) for i in range(10)]


def make_type_operator(editor_type, type_name):
    class WORKSPACE_OT_ToggleTypeLayout(Operator):
        bl_idname = f"view3d.toggle_split_{editor_type.lower()}"
        bl_label = f"切换 {type_name} 分屏"
        bl_description = f"智能切换 {type_name} 分屏视图"
        bl_options = {'REGISTER', 'UNDO'}

        def execute(self, context):
            return execute_toggle_layout(self, context, editor_type=editor_type)

    WORKSPACE_OT_ToggleTypeLayout.__name__ = f"WORKSPACE_OT_ToggleSplit_{editor_type}"
    return WORKSPACE_OT_ToggleTypeLayout

TYPE_OPERATORS = [make_type_operator(item[0], item[1]) for item in EDITOR_ITEMS]


class WORKSPACE_OT_AddCustomLayoutItem(Operator):
    bl_idname = "workspace.add_custom_layout"
    bl_label = "添加分屏视图"
    bl_description = "添加一个新的自定义分屏视图项"
    bl_options = {'REGISTER', 'UNDO'}

    editor_type: EnumProperty(
        name="视图类型",
        items=EDITOR_ITEMS,
        default='UV',
    )

    def execute(self, context):
        scene = context.scene
        new_item = scene.um_custom_layouts.add()
        new_item.id = str(int(time.time() * 1000))
        new_item.editor_type = self.editor_type
        new_item.name = EDITOR_DEFAULT_NAMES.get(self.editor_type, "新视图")
        new_item.direction = 'LEFT'
        new_item.ratio = 0.4

        save_scene_layouts_to_json(scene)
        scene.um_show_layout_settings = True
        self.report({'INFO'}, f"已添加: {new_item.name}")
        return {'FINISHED'}


class WORKSPACE_MT_AddCustomLayoutMenu(bpy.types.Menu):
    bl_idname = "WORKSPACE_MT_add_custom_layout_menu"
    bl_label = "选择添加的视图类型"

    def draw(self, context):
        layout = self.layout
        for identifier, name, desc, icon, idx in EDITOR_ITEMS:
            op = layout.operator("workspace.add_custom_layout", text=name, icon=icon)
            op.editor_type = identifier


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
    UVMM_CustomLayoutItem,
    WORKSPACE_MT_AddCustomLayoutMenu,
    WORKSPACE_OT_ToggleCustomLayout,
    *SLOT_OPERATORS,
    *TYPE_OPERATORS,
    WORKSPACE_OT_AddCustomLayoutItem,
    WORKSPACE_OT_RemoveCustomLayoutItem,
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
    except Exception as e:
        print(f"[UV_Material_Manager] register {cls} warning: {e}")

def safe_unregister_class(cls):
    try:
        bpy.utils.unregister_class(cls)
    except Exception:
        pass

def register():
    load_sidebar_states_from_json()
    for cls in classes:
        safe_register_class(cls)

    bpy.types.Scene.um_custom_layouts = CollectionProperty(type=UVMM_CustomLayoutItem)
    bpy.types.Scene.um_show_layout_settings = BoolProperty(name="显示视图配置", default=False)

    bpy.types.WindowManager.um_active_layout_id = StringProperty(name="活动视图ID", default="")
    bpy.types.WindowManager.um_secondary_area_ptr = StringProperty(name="副屏区域指针", default="")
    bpy.types.WindowManager.um_secondary_direction = StringProperty(name="副屏方位", default="LEFT")

    if on_load_post_sync not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(on_load_post_sync)

    try:
        bpy.app.timers.register(initial_sync_timer, first_interval=0.01)
    except Exception:
        pass


def unregister():
    if on_load_post_sync in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(on_load_post_sync)

    if hasattr(bpy.types.WindowManager, 'um_secondary_direction'):
        del bpy.types.WindowManager.um_secondary_direction
    if hasattr(bpy.types.WindowManager, 'um_secondary_area_ptr'):
        del bpy.types.WindowManager.um_secondary_area_ptr
    if hasattr(bpy.types.WindowManager, 'um_active_layout_id'):
        del bpy.types.WindowManager.um_active_layout_id

    if hasattr(bpy.types.Scene, 'um_show_layout_settings'):
        del bpy.types.Scene.um_show_layout_settings
    if hasattr(bpy.types.Scene, 'um_custom_layouts'):
        del bpy.types.Scene.um_custom_layouts

    for cls in reversed(classes):
        safe_unregister_class(cls)