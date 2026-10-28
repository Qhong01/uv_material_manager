"""Grouped editor for the add-on's actual Blender keymap items.

Rows mirror Blender's own keymap editor (``rna_keymap_ui.draw_kmi``) so the
page shows the same columns as *Preferences > Keymap*: an expansion arrow, an
enable checkbox, the shortcut name, the keymap-type dropdown ("键盘"), the key
binding and a remove button.
"""

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy.types import Operator

from . import workspace


# (label, keymap, operator, key, modifiers, operator properties)
GROUPS = {
    'VIEW': (
        ('大纲视图', '3D View', 'view3d.toggle_outliner', 'Z', {'alt': True}, {}),
        ('着色', '3D View', 'wm.call_panel', 'Q',
         {'ctrl': True, 'shift': True, 'alt': True},
         {'name': 'VIEW3D_PT_QuickShadingPopover'}),
    ),
    'MODEL': (
        ('智能环选', 'Mesh', 'mesh.smart_loop_select', 'LEFTMOUSE', {'alt': True}, {}),
        ('90°旋转', '3D View', 'view3d.open_rotate_90_menu', 'A',
         {'ctrl': True, 'alt': True}, {}),
        ('圈内选面', 'Mesh', 'mesh.select_face_ring_region', 'THREE', {}, {}),
        ('吸材质（编辑）', 'Mesh', 'material.pick_material_for_selected_faces', 'RIGHTMOUSE',
         {'ctrl': True, 'shift': True, 'alt': True}, {}),
        ('吸材质（物体）', 'Object Mode', 'material.pick_material_for_selected_faces', 'RIGHTMOUSE',
         {'ctrl': True, 'shift': True, 'alt': True}, {}),
        ('滑动旋转', 'Mesh', 'mesh.edge_rail_rotate', 'R', {'alt': True}, {}),
        ('快速镜像', '3D View', 'object.quick_mirror_gesture', 'X',
         {'alt': True, 'shift': True}, {}),
        ('智能顶点', 'Mesh', 'mesh.uvmm_smart_vertex', 'ONE', {}, {}),
        ('智能边', 'Mesh', 'mesh.uvmm_smart_edge', 'TWO', {}, {}),
        ('切换标记锐边', 'Mesh', 'mesh.uvmm_toggle_sharp', 'TWO',
         {'shift': True}, {}),
    ),
    'FOCUS': (
        ('聚焦', 'Object Mode', 'view3d.uvmm_focus_selected', 'F', {}, {}),
        ('隐藏未选（物体）', 'Object Mode', 'view3d.uvmm_hide_unselected', 'F',
         {'ctrl': True}, {}),
        ('隐藏所选／回退（物体）', 'Object Mode', 'view3d.uvmm_restore_hidden_level',
         'F', {'ctrl': True, 'alt': True}, {}),
        ('隐藏未选（编辑）', 'Mesh', 'view3d.uvmm_hide_unselected', 'F',
         {'ctrl': True}, {}),
        ('隐藏所选／回退（编辑）', 'Mesh', 'view3d.uvmm_restore_hidden_level',
         'F', {'ctrl': True, 'alt': True}, {}),
    ),
    'OTHER': (
        ('拍平面', '3D View', 'mesh.flatten_face_by_three_vertices', 'NONE', {}, {}),
        ('合并材质', 'Object Mode', 'object.ultra_material_combine', 'NONE', {}, {}),
        ('填充孔洞', 'Mesh', 'mesh.zhineng_fill_holes', 'NONE', {}, {}),
        ('清理法线', 'Object Mode', 'custom.clear_normals', 'NONE', {}, {}),
    ),
}

SPLIT_DEFAULTS = {
    'UV': ('T', {'shift': True}),
    'SHADER': ('Y', {'shift': True}),
    'ASSETS': ('SPACE', {'ctrl': True, 'shift': True}),
}

EXISTING_IDS = {
    'mesh.select_face_ring_region',
    'material.pick_material_for_selected_faces',
    'mesh.uvmm_toggle_sharp',
    'view3d.uvmm_focus_selected',
    'view3d.uvmm_hide_unselected',
    'view3d.uvmm_restore_hidden_level',
}

# Display name / icon for the keymap contexts used above.
KEYMAP_INFO = {
    '3D View': ('3D 视图', 'VIEW3D'),
    'Mesh': ('网格', 'MESH_DATA'),
    'Object Mode': ('物体模式', 'OBJECT_DATA'),
    'Screen': ('屏幕', 'WINDOW'),
}

# Indentation in pixels, matching the native keymap editor's 16 px steps.
_INDENT_PX = 18.0


def _keymap_label(name):
    return KEYMAP_INFO.get(name, (name, 'NONE'))[0]


def _keymap_icon(name):
    return KEYMAP_INFO.get(name, (name, 'NONE'))[1]


def _indented(layout, context, level):
    """Return a column indented by ``level`` steps, like the native editor."""
    region = getattr(context, 'region', None)
    width = float(getattr(region, 'width', 0) or 0.0)
    factor = (_INDENT_PX * level / width) if width > 1.0 else 0.0
    if factor <= 0.0:
        return layout.column()
    split = layout.split(factor=min(factor, 0.4))
    split.column()
    return split.column()


def _keymap(config, name):
    return config.keymaps.get(name) if config else None


def _matching_item(config, name, operator_id, properties):
    km = _keymap(config, name)
    if km is None:
        return None, None
    for kmi in km.keymap_items:
        if (kmi.idname == operator_id
                and all(getattr(kmi.properties, key, None) == value
                        for key, value in properties.items())):
            return km, kmi
    return km, None


def _preferences(context=None):
    preferences = getattr(context, 'preferences', None) if context else None
    if preferences is None:
        preferences = getattr(bpy.context, 'preferences', None)
    addon = preferences.addons.get(__package__) if preferences else None
    return addon.preferences if addon else None


def _spec_key(spec):
    _, name, operator_id, _, _, properties = spec
    return '|'.join((name, operator_id,
                     properties.get('item_id', ''),
                     properties.get('name', '')))


def _removed_keys(context=None):
    preferences = _preferences(context)
    raw = getattr(preferences, 'removed_shortcuts', '') if preferences else ''
    return {line for line in raw.split('\n') if line}


def _write_removed_keys(context, keys):
    preferences = _preferences(context)
    if preferences is None:
        return
    preferences.removed_shortcuts = '\n'.join(sorted(keys))


def _set_removed(context, spec, removed):
    keys = _removed_keys(context)
    key = _spec_key(spec)
    if removed == (key in keys):
        return
    if removed:
        keys.add(key)
    else:
        keys.discard(key)
    _write_removed_keys(context, keys)


def _ensure_item(window_manager, spec, allow_unassigned=False,
                 use_user_keymap=False, context=None):
    _, name, operator_id, key, modifiers, properties = spec
    if not key or key == 'NONE':
        if not allow_unassigned:
            return
        key = 'NONE'
    if _spec_key(spec) in _removed_keys(context):
        return
    user = window_manager.keyconfigs.user
    addon = window_manager.keyconfigs.addon
    config = user if use_user_keymap else addon
    if config is None:
        return
    if _matching_item(config, name, operator_id, properties)[1] is not None:
        return
    if not use_user_keymap and user is not None:
        if _matching_item(user, name, operator_id, properties)[1] is not None:
            return
    space_type = 'VIEW_3D' if name == '3D View' else 'EMPTY'
    km = config.keymaps.new(name=name, space_type=space_type, region_type='WINDOW')
    kmi = km.keymap_items.new(operator_id, key, 'PRESS', head=True, **modifiers)
    for prop, value in properties.items():
        setattr(kmi.properties, prop, value)


def _layout_field(item, name):
    return item.get(name, '') if isinstance(item, dict) else getattr(item, name)


def _layout_spec(item, index, key='NONE', modifiers=None):
    if index < 10:
        operator_id = f'screen.toggle_custom_layout_{index + 1}'
        properties = {}
    else:
        operator_id = 'screen.toggle_custom_layout'
        properties = {'item_id': _layout_field(item, 'id')}
    return (_layout_field(item, 'name'), 'Screen', operator_id,
            key, modifiers or {}, properties)


def _ensure_layout_items(wm, items):
    seen_types = set()
    for index, item in enumerate(items):
        editor_type = _layout_field(item, 'editor_type')
        key, modifiers = SPLIT_DEFAULTS.get(editor_type, ('NONE', {}))
        if editor_type in seen_types:
            key, modifiers = 'NONE', {}
        seen_types.add(editor_type)
        _ensure_item(wm, _layout_spec(item, index, key, modifiers))


def ensure_layout_shortcuts(scene):
    """Add missing layout bindings without changing any existing user bindings."""
    wm = bpy.context.window_manager
    if wm is None:
        return
    _ensure_layout_items(wm, scene.um_custom_layouts)
    wm.keyconfigs.update()


def _draw_keymap_header(layout, context, name):
    """Small section title that mirrors the native "屏幕 (全局)" child header."""
    row = _indented(layout, context, 0.8).row(align=True)
    row.label(text=_keymap_label(name), icon=_keymap_icon(name))


def _draw_shortcut(layout, context, spec, level=2.0):
    label, name, operator_id, key, modifiers, properties = spec
    removed = _spec_key(spec) in _removed_keys(context)
    km, kmi = None, None
    if not removed:
        configs = context.window_manager.keyconfigs
        km, kmi = _matching_item(configs.user, name, operator_id, properties)
        if kmi is None:
            km, kmi = _matching_item(configs.addon, name, operator_id, properties)

    col = _indented(layout, context, level)

    if kmi is None:
        row = col.row(align=True)
        row.separator(factor=2.2)
        row.label(text=label)
        head = row.row()
        head.alignment = 'RIGHT'
        op = head.operator('uvmm.add_shortcut', text='添加快捷键', icon='ADD')
        op.keymap_name = name
        op.operator_id = operator_id
        op.item_id = properties.get('item_id', '')
        op.panel_name = properties.get('name', '')
        op.key = key
        op.ctrl = bool(modifiers.get('ctrl'))
        op.shift = bool(modifiers.get('shift'))
        op.alt = bool(modifiers.get('alt'))
        op.oskey = bool(modifiers.get('oskey'))
        return

    if kmi.show_expanded:
        split = col.box().split()
    else:
        split = col.split()

    # Left half: expansion arrow, enable checkbox, name - the native widgets.
    head = split.row(align=True)
    head.prop(kmi, 'show_expanded', text='', emboss=False)
    head.prop(kmi, 'active', text='', emboss=False)
    head.label(text=label)

    # Right half: keymap type ("键盘"), key binding, remove button.
    right = split.row()
    right.prop(kmi, 'map_type', text='')
    map_type = kmi.map_type
    if map_type in {'KEYBOARD', 'MOUSE', 'NDOF'}:
        right.prop(kmi, 'type', text='', full_event=True)
    elif map_type == 'TWEAK':
        right.prop(kmi, 'type', text='')
        right.prop(kmi, 'value', text='')
    elif map_type == 'TIMER':
        right.prop(kmi, 'type', text='')
    else:
        right.label()
    remove = right.operator('uvmm.remove_shortcut', text='', icon='X')
    remove.keymap_name = name
    remove.operator_id = operator_id
    remove.item_id = properties.get('item_id', '')
    remove.panel_name = properties.get('name', '')
    right.separator(factor=0.4)

    if not kmi.show_expanded:
        return

    # Expanded event settings, laid out like the native keymap item box.
    detail = col.box()
    split = detail.split(factor=0.42)
    info = split.row()
    info.label(text=kmi.idname)
    body = split.column()
    row = body.row(align=True)
    row.prop(kmi, 'type', text='', event=True)
    row.prop(kmi, 'value', text='')
    repeat = row.row(align=True)
    repeat.active = kmi.value in {'ANY', 'PRESS'}
    repeat.prop(kmi, 'repeat', text='重复')
    row = body.row(align=True)
    row.scale_x = 0.75
    row.prop(kmi, 'any', toggle=True)
    row.prop(kmi, 'shift_ui', toggle=True)
    row.prop(kmi, 'ctrl_ui', toggle=True)
    row.prop(kmi, 'alt_ui', toggle=True)
    row.prop(kmi, 'oskey_ui', text='Win', toggle=True)
    row.prop(kmi, 'key_modifier', text='', event=True)
    detail.template_keymap_item_properties(kmi)


def _keymap_buckets(specs):
    """Group specs by keymap context while preserving their original order."""
    buckets = []
    lookup = {}
    for spec in specs:
        name = spec[1]
        index = lookup.get(name)
        if index is None:
            lookup[name] = len(buckets)
            buckets.append((name, [spec]))
        else:
            buckets[index][1].append(spec)
    return buckets


def _draw_group(layout, context, specs):
    buckets = _keymap_buckets(specs)
    # One indent level per keymap section, exactly like the native editor's
    # "Global" child section. Rows always use the same level so the "键盘",
    # key and close columns line up across every group.
    with_headers = len(buckets) > 1
    for name, group_specs in buckets:
        if with_headers:
            _draw_keymap_header(layout, context, name)
        for spec in group_specs:
            _draw_shortcut(layout, context, spec, 2.0)


class UVMM_OT_AddShortcut(Operator):
    bl_idname = 'uvmm.add_shortcut'
    bl_label = '添加快捷键'
    bl_options = {'REGISTER', 'UNDO'}

    keymap_name: StringProperty()
    operator_id: StringProperty()
    item_id: StringProperty()
    panel_name: StringProperty()
    key: StringProperty(default='NONE')
    ctrl: BoolProperty()
    shift: BoolProperty()
    alt: BoolProperty()
    oskey: BoolProperty()

    def execute(self, context):
        properties = {}
        if self.item_id:
            properties['item_id'] = self.item_id
        if self.panel_name:
            properties['name'] = self.panel_name
        modifiers = {name: True for name in ('ctrl', 'shift', 'alt', 'oskey')
                     if getattr(self, name)}
        spec = ('', self.keymap_name, self.operator_id,
                self.key or 'NONE', modifiers, properties)
        _set_removed(context, spec, False)
        _ensure_item(context.window_manager, spec, allow_unassigned=True,
                     use_user_keymap=True, context=context)
        context.window_manager.keyconfigs.update()
        return {'FINISHED'}


class UVMM_OT_RemoveShortcut(Operator):
    bl_idname = 'uvmm.remove_shortcut'
    bl_label = '移除快捷键'
    bl_options = {'REGISTER', 'UNDO'}

    keymap_name: StringProperty()
    operator_id: StringProperty()
    item_id: StringProperty()
    panel_name: StringProperty()

    def execute(self, context):
        properties = {}
        if self.item_id:
            properties['item_id'] = self.item_id
        if self.panel_name:
            properties['name'] = self.panel_name
        # Only touch the user keyconfig: modifying the add-on keyconfig at
        # runtime makes Blender drop the whole merged keymap on the next sync.
        km, kmi = _matching_item(context.window_manager.keyconfigs.user,
                                 self.keymap_name, self.operator_id, properties)
        if kmi is None:
            return {'CANCELLED'}
        km.keymap_items.remove(kmi)
        _set_removed(context, ('', self.keymap_name, self.operator_id,
                               'NONE', {}, properties), True)
        preferences = getattr(context, 'preferences', None)
        if preferences is not None:
            preferences.is_dirty = True
        return {'FINISHED'}


def draw_shortcut_preferences(layout, context):
    layout.label(text='快捷键配置')
    layout.label(text='不要删除热键，而是用勾选禁用它们（删除后可用「添加快捷键」恢复）。',
                 icon='INFO')

    groups = (
        ('uvmm_shortcuts_view', '视图与着色', GROUPS['VIEW']),
        ('uvmm_shortcuts_model', '建模与选择', GROUPS['MODEL']),
        ('uvmm_shortcuts_focus', '聚焦与隐藏', GROUPS['FOCUS']),
        ('uvmm_shortcuts_split', '分屏', None),
        ('uvmm_shortcuts_other', '其他工具（未设默认）', GROUPS['OTHER']),
    )
    for panel_id, title, specs in groups:
        header, body = layout.panel(panel_id, default_closed=True)
        header.label(text=title)
        if body is None:
            continue
        if specs is not None:
            _draw_group(body, context, specs)
            continue
        scene = context.scene
        items = getattr(scene, 'um_custom_layouts', None) if scene else None
        if items is None:
            items = workspace.load_json_config()
        if not items:
            body.label(text='尚未添加分屏项目', icon='INFO')
            continue
        _draw_group(body, context,
                    [_layout_spec(item, index)
                     for index, item in enumerate(items)])


CLASSES = (
    UVMM_OT_AddShortcut,
    UVMM_OT_RemoveShortcut,
)


def register():
    for cls in CLASSES:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            bpy.utils.unregister_class(cls)
            bpy.utils.register_class(cls)
    wm = bpy.context.window_manager
    if wm is None:
        return
    # Settle the addon/user merge first: otherwise keys left over from a
    # previous load make _ensure_item skip defaults that are about to be
    # dropped by the update below.
    wm.keyconfigs.update()
    for group in GROUPS.values():
        for spec in group:
            if spec[2] not in EXISTING_IDS:
                _ensure_item(wm, spec)
    _ensure_layout_items(wm, workspace.load_json_config())
    wm.keyconfigs.update()


def unregister():
    wm = bpy.context.window_manager
    addon = wm.keyconfigs.addon if wm else None
    if addon is not None:
        owned = {(spec[1], spec[2]) for group in GROUPS.values()
                 for spec in group if spec[2] not in EXISTING_IDS}
        owned.update(('Screen', f'screen.toggle_custom_layout_{index + 1}')
                     for index in range(10))
        owned.add(('Screen', 'screen.toggle_custom_layout'))
        for name, operator_id in owned:
            km = addon.keymaps.get(name)
            if km is not None:
                for kmi in list(km.keymap_items):
                    if kmi.idname == operator_id:
                        km.keymap_items.remove(kmi)
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
