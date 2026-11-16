import bpy
import json
from bpy.types import Operator, Menu, Panel, PropertyGroup
from bpy.props import StringProperty, BoolProperty, IntProperty, PointerProperty, CollectionProperty
from bpy.app.handlers import persistent
from . import modifier_layouts

# 修改器官方矢量图标映射
MODIFIER_ICONS = {
    'DATA_TRANSFER': 'MOD_DATA_TRANSFER',
    'MESH_CACHE': 'MOD_MESHDEFORM',
    'MESH_SEQUENCE_CACHE': 'MOD_MESHDEFORM',
    'NORMAL_EDIT': 'MOD_NORMALEDIT',
    'WEIGHTED_NORMAL': 'MOD_NORMALEDIT',
    'UV_PROJECT': 'MOD_UVPROJECT',
    'UV_WARP': 'MOD_UVPROJECT',
    'VERTEX_WEIGHT_EDIT': 'MOD_VERTEX_WEIGHT',
    'VERTEX_WEIGHT_MIX': 'MOD_VERTEX_WEIGHT',
    'VERTEX_WEIGHT_PROXIMITY': 'MOD_VERTEX_WEIGHT',
    'ARRAY': 'MOD_ARRAY',
    'BEVEL': 'MOD_BEVEL',
    'BOOLEAN': 'MOD_BOOLEAN',
    'BUILD': 'MOD_BUILD',
    'DECIMATE': 'MOD_DECIM',
    'EDGE_SPLIT': 'MOD_EDGESPLIT',
    'NODES': 'GEOMETRY_NODES',
    'MASK': 'MOD_MASK',
    'MIRROR': 'MOD_MIRROR',
    'MULTIRES': 'MOD_MULTIRES',
    'REMESH': 'MOD_REMESH',
    'SCREW': 'MOD_SCREW',
    'SKIN': 'MOD_SKIN',
    'SOLIDIFY': 'MOD_SOLIDIFY',
    'SUBSURF': 'MOD_SUBSURF',
    'TRIANGULATE': 'MOD_TRIANGULATE',
    'VOLUME_TO_MESH': 'VOLUME_DATA',
    'MESH_TO_VOLUME': 'VOLUME_DATA',
    'WELD': 'AUTOMERGE_OFF',
    'WIREFRAME': 'MOD_WIREFRAME',
    'ARMATURE': 'MOD_ARMATURE',
    'CAST': 'MOD_CAST',
    'CURVE': 'MOD_CURVE',
    'DISPLACE': 'MOD_DISPLACE',
    'HOOK': 'HOOK',
    'LAPLACIANDEFORM': 'MOD_MESHDEFORM',
    'LAPLACIANSMOOTH': 'MOD_SMOOTH',
    'LATTICE': 'MOD_LATTICE',
    'MESH_DEFORM': 'MOD_MESHDEFORM',
    'SHRINKWRAP': 'MOD_SHRINKWRAP',
    'SIMPLE_DEFORM': 'MOD_SIMPLEDEFORM',
    'SMOOTH': 'MOD_SMOOTH',
    'CORRECTIVE_SMOOTH': 'MOD_SMOOTH',
    'SURFACE_DEFORM': 'MOD_MESHDEFORM',
    'WARP': 'MOD_WARP',
    'WAVE': 'MOD_WAVE',
    'CLOTH': 'MOD_CLOTH',
    'COLLISION': 'MOD_PHYSICS',
    'DYNAMIC_PAINT': 'MOD_DYNAMICPAINT',
    'EXPLODE': 'MOD_EXPLODE',
    'FLUID': 'MOD_FLUID',
    'OCEAN': 'MOD_OCEAN',
    'PARTICLE_INSTANCE': 'MOD_PARTICLES',
    'PARTICLE_SYSTEM': 'MOD_PARTICLES',
    'SOFT_BODY': 'MOD_SOFT',
    'SURFACE': 'MOD_PHYSICS',
}

def get_modifier_icon(mod_type):
    return MODIFIER_ICONS.get(mod_type, 'MODIFIER')


# =========================================================================
# 实时自动同步引擎 (选中模型中有参数实际变化时同步，切换选中时不触发)
# =========================================================================

class ModifierSyncSnapshot(PropertyGroup):
    object_name: StringProperty()
    modifier_name: StringProperty()
    modifier_uid: IntProperty()
    modifier_type: StringProperty()
    data: StringProperty()


class ModifierSyncChange(PropertyGroup):
    data: StringProperty()


class ModifierSyncRuntimeState(PropertyGroup):
    snapshots: CollectionProperty(type=ModifierSyncSnapshot)
    selection_signature: StringProperty()
    is_syncing: BoolProperty(default=False)
    warning_message: StringProperty()
    status_message: StringProperty()


class ModifierSyncHistory(PropertyGroup):
    # Session-only journal: Scene RNA is itself restored by native undo.
    scene_name: StringProperty()
    changes: CollectionProperty(type=ModifierSyncChange)
    change_cursor: IntProperty(default=0)
    undo_baseline: StringProperty()
    undo_participants: StringProperty()
    is_restoring: BoolProperty(default=False)
    pending_operation: StringProperty()
    last_operation_token: StringProperty()


def _modifier_history(scene, create=False):
    wm = getattr(bpy.context, 'window_manager', None)
    if wm is None:
        return None
    for history in wm.um_modifier_sync_histories:
        if history.scene_name == scene.name:
            return history
    if create:
        history = wm.um_modifier_sync_histories.add()
        history.scene_name = scene.name
        return history
    return None


def _is_user_modifier(mod):
    if mod.type != 'NODES':
        return True
    if mod.node_group is not None:
        return not mod.node_group.get('uvmm_native_preview', False)
    return not mod.name.startswith('UV 检查 · 材质预览（临时）')


class UMM_OT_StatusTooltip(Operator):
    bl_idname = "wm.umm_status_tooltip"
    bl_label = "操作提示"
    bl_description = "悬停查看完整操作提示"
    bl_options = {'INTERNAL'}

    message: StringProperty(options={'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        return properties.message or cls.bl_description

    def execute(self, context):
        return {'FINISHED'}


def draw_status_tooltip(layout, message, icon='INFO', alert=False):
    status_row = layout.row(align=True)
    status_row.alert = alert
    button = status_row.operator(
        UMM_OT_StatusTooltip.bl_idname,
        text=message,
        icon=icon,
        emboss=False
    )
    button.message = message


class ModifierAutoSync:
    ALWAYS_IGNORED_PROPERTIES = {
        'name', 'type', 'show_expanded', 'is_active', 'execution_time',
        'is_override_data', 'use_pin_to_last'
    }
    IGNORED_PROPERTIES = {
        'show_viewport', 'show_render', 'show_in_editmode', 'show_on_cage',
        'use_pin_to_last'
    }

    @classmethod
    def serialize_value(cls, val):
        if val is None:
            return None
        if isinstance(val, set):
            return sorted(val)
        if isinstance(val, (int, float, str, bool)):
            return val
        if isinstance(val, bpy.types.bpy_struct):
            return getattr(val, 'name', str(val))
        try:
            if not isinstance(val, (str, bytes)):
                return [cls.serialize_value(x) for x in val]
        except TypeError:
            pass
        return str(val)

    @classmethod
    def get_snapshot(cls, mod, include_display=True):
        snap = {}
        for p in mod.bl_rna.properties:
            ignored = cls.ALWAYS_IGNORED_PROPERTIES
            if not include_display:
                ignored = ignored | cls.IGNORED_PROPERTIES
            if not p.is_readonly and p.identifier not in ignored:
                try:
                    val = getattr(mod, p.identifier)
                    snap[p.identifier] = cls.serialize_value(val)
                except Exception:
                    pass
        for category, sockets in _modifier_node_properties(mod).items():
            for identifier, fields in sockets.items():
                for name in fields:
                    key = f'@/{category}/{identifier}/{name}'
                    snap[key] = cls.serialize_value(_get_modifier_parameter(mod, key))
        for key, value in _modifier_nested_parameters(mod).items():
            snap[key] = cls.serialize_value(value)
        return snap

    @classmethod
    def get_state(cls, scene):
        return getattr(scene, "um_modifier_sync_state", None)

    @classmethod
    def find_snapshot(cls, scene, obj, mod):
        state = cls.get_state(scene)
        if not state:
            return None
        return next((entry for entry in state.snapshots
                     if entry.object_name == obj.name and entry.modifier_uid == mod.persistent_uid), None)

    @classmethod
    def set_snapshot(cls, scene, obj, mod, snapshot=None):
        state = cls.get_state(scene)
        if not state:
            return
        entry = cls.find_snapshot(scene, obj, mod)
        if entry is None:
            entry = state.snapshots.add()
            entry.object_name = obj.name
        entry.modifier_name = mod.name
        entry.modifier_uid = mod.persistent_uid
        entry.modifier_type = mod.type
        entry.data = json.dumps(snapshot if snapshot is not None else cls.get_snapshot(mod), sort_keys=True)

    @classmethod
    def remove_snapshot(cls, scene, obj, modifier_name):
        state = cls.get_state(scene)
        if not state:
            return
        for index in range(len(state.snapshots) - 1, -1, -1):
            entry = state.snapshots[index]
            if entry.object_name == obj.name and entry.modifier_name == modifier_name:
                state.snapshots.remove(index)

    @classmethod
    def ensure_snapshots(cls, scene, objects):
        """Rebuild the baseline for the selected meshes and their modifiers."""
        state = cls.get_state(scene)
        if not state:
            return
        state.snapshots.clear()
        for obj in objects:
            if obj and obj.type == 'MESH':
                for mod in obj.modifiers:
                    if _is_user_modifier(mod):
                        cls.set_snapshot(scene, obj, mod)

    @classmethod
    def reset_all_states(cls, *_args):
        for scene in bpy.data.scenes:
            state = cls.get_state(scene)
            if state:
                state.snapshots.clear()
                state.selection_signature = ""
                state.is_syncing = False
                state.warning_message = ""

    @classmethod
    def update_handler(cls, scene, depsgraph):
        state = cls.get_state(scene)
        history = _modifier_history(scene)
        if not state or state.is_syncing or (history and history.is_restoring):
            return

        if hasattr(scene, "um_auto_sync_modifiers") and not scene.um_auto_sync_modifiers:
            state.snapshots.clear()
            state.selection_signature = ""
            state.warning_message = ""
            return

        context = bpy.context
        active_obj = context.view_layer.objects.active
        if not active_obj or active_obj.type != 'MESH':
            return

        selected_objs = [o for o in context.selected_objects if o.type == 'MESH']
        if not selected_objs:
            return

        curr_selection = (active_obj.name, tuple(sorted(o.name for o in selected_objs)))
        curr_selection_signature = json.dumps(curr_selection)

        # 如果用户刚在视图中切换了选择，仅更新基准快照，绝不触发参数同步
        if curr_selection_signature != state.selection_signature:
            state.selection_signature = curr_selection_signature
            state.warning_message = ""
            cls.ensure_snapshots(scene, selected_objs)
            return

        # 只检查本次确实发生几何更新的选中物体，活动物体和非活动选中物体都可作为同步源。
        updated_geometry_ids = set()
        for update in depsgraph.updates:
            original_id = getattr(update.id, "original", None)
            if original_id is not None and (update.is_updated_geometry
                                             or isinstance(original_id, bpy.types.Object)):
                updated_geometry_ids.add(original_id.as_pointer())
        updated_objects = [
            obj for obj in selected_objs
            if (obj.as_pointer() in updated_geometry_ids
                or (obj.data and obj.data.as_pointer() in updated_geometry_ids))
        ]
        if not updated_objects:
            return

        state.is_syncing = True
        try:
            state.warning_message = ""
            changes = []
            added_count = 0
            skipped_objects = set()
            if active_obj in updated_objects:
                changes, added_count, skipped_objects = _sync_added_modifiers(
                    scene, active_obj, selected_objs)
                removed_changes, removed_count, removal_skips = _sync_removed_modifiers(
                    scene, active_obj, selected_objs, depsgraph)
                changes.extend(removed_changes)
                skipped_objects.update(removal_skips)
            else:
                removed_count = 0
            changed_values = {}
            for source_obj in updated_objects:
                for mod in source_obj.modifiers:
                    if not _is_user_modifier(mod):
                        continue
                    snapshot_entry = cls.find_snapshot(scene, source_obj, mod)
                    prev_snap = json.loads(snapshot_entry.data) if snapshot_entry and snapshot_entry.data else None
                    curr_snap = cls.get_snapshot(mod)
                    if prev_snap is None:
                        continue
                    for prop_id, curr_val in curr_snap.items():
                        if prop_id in prev_snap and prev_snap[prop_id] != curr_val:
                            key = (mod.name, mod.type, prop_id)
                            changed_values.setdefault(key, []).append((source_obj, mod, curr_val))

            synced_counts = {}
            if changed_values:
                state.warning_message = ""
                state.status_message = ""
            for (modifier_name, modifier_type, prop_id), sources in changed_values.items():
                distinct_values = {
                    json.dumps(source[2], sort_keys=True)
                    for source in sources
                }
                if len(distinct_values) > 1:
                    changes.extend(_parameter_change(scene, obj, mod, prop_id, value)
                                   for obj, mod, value in sources)
                    continue

                source_obj, source_mod, _serialized_value = sources[0]
                raw_value = _get_modifier_parameter(source_mod, prop_id)
                change = _parameter_change(scene, source_obj, source_mod, prop_id, _serialized_value)
                for target_obj in selected_objs:
                    if any(target_obj == source[0] for source in sources):
                        continue
                    target_mod = target_obj.modifiers.get(modifier_name)
                    if target_mod and target_mod.type == modifier_type:
                        try:
                            previous_value = _modifier_history_value(_get_modifier_parameter(target_mod, prop_id))
                            if isinstance(raw_value, (tuple, list)):
                                _set_modifier_parameter(target_mod, prop_id, list(raw_value))
                            else:
                                _set_modifier_parameter(target_mod, prop_id, raw_value)
                            key = (modifier_name, prop_id)
                            synced_counts[key] = synced_counts.get(key, 0) + 1
                            change['targets'].append({
                                'object': target_obj.name, 'before': previous_value,
                                'after': _modifier_history_value(_get_modifier_parameter(target_mod, prop_id)),
                            })
                        except (AttributeError, TypeError, ValueError, RuntimeError):
                            skipped_objects.add(target_obj.name)
                    else:
                        skipped_objects.add(target_obj.name)
                changes.append(change)
                changes.extend(_parameter_change(scene, obj, mod, prop_id, value)
                               for obj, mod, value in sources[1:])

            if changes:
                history = _modifier_history(scene, create=True)
                while len(history.changes) > history.change_cursor:
                    history.changes.remove(len(history.changes) - 1)
                entry = history.changes.add()
                entry.data = json.dumps(changes)
                limit = max(64, bpy.context.preferences.edit.undo_steps * 128)
                while len(history.changes) > limit:
                    history.changes.remove(0)
                history.change_cursor = len(history.changes)

            # 同步之后统一刷新所选物体的基线，防止后续视图更新重复触发。
            cls.ensure_snapshots(scene, selected_objs)
            if synced_counts:
                (modifier_name, prop_id), count = next(iter(synced_counts.items()))
                label_mod = active_obj.modifiers.get(modifier_name)
                label = _modifier_parameter_label(label_mod, prop_id)
                state.status_message = f"已同步 {modifier_name} · {label}，{count} 个物体"
            elif added_count:
                state.status_message = f"已同步新增修改器到 {added_count} 个物体"
            elif removed_count:
                state.status_message = f"已同步修改器操作到 {removed_count} 个物体"
            if skipped_objects:
                state.warning_message = f"已跳过 {len(skipped_objects)} 个缺少匹配修改器或不可写的物体"
        finally:
            state.is_syncing = False


def set_modifier_status(context, message):
    state = ModifierAutoSync.get_state(context.scene) if context.scene else None
    if state:
        state.warning_message = ""
        state.status_message = message


@persistent
def depsgraph_update_handler(scene, depsgraph):
    ModifierAutoSync.update_handler(scene, depsgraph)


@persistent
def modifier_sync_reset_handler(*args):
    ModifierAutoSync.reset_all_states(*args)
    scene = bpy.context.scene
    if scene is not None:
        update_modifier_sync_enabled(scene, bpy.context)


@persistent
def modifier_sync_load_post(*args):
    bpy.context.window_manager.um_modifier_sync_histories.clear()
    modifier_sync_reset_handler(*args)


def _modifier_history_value(value):
    if isinstance(value, bpy.types.ID):
        return {'id_type': value.bl_rna.identifier, 'name': value.name_full,
                'library': value.library.filepath if value.library else ''}
    return ModifierAutoSync.serialize_value(value)


def _parameter_change(scene, obj, mod, identifier, value):
    entry = ModifierAutoSync.find_snapshot(scene, obj, mod)
    return {'source': obj.name, 'source_uid': mod.persistent_uid,
            'modifier': mod.name, 'type': mod.type, 'property': identifier,
            'before': json.loads(entry.data)[identifier], 'after': value, 'targets': []}


def _modifier_node_properties(mod):
    node_properties = {}
    properties = getattr(mod, 'properties', None) if mod.type == 'NODES' else None
    if properties is not None and mod.node_group is not None:
        for socket in mod.node_group.interface.items_tree:
            if socket.item_type != 'SOCKET':
                continue
            category = 'inputs' if socket.in_out == 'INPUT' else 'outputs'
            value = getattr(getattr(properties, category), socket.identifier, None)
            if value is not None:
                node_properties.setdefault(category, {})[socket.identifier] = {
                    name: _modifier_history_value(getattr(value, name))
                    for name in ('type', 'value', 'attribute_name')
                    if hasattr(value, name) and not value.is_property_readonly(name)
                }
    return node_properties


def _modifier_parameter_owner(mod, identifier):
    if identifier.startswith('@/'):
        path = identifier[2:].split('/')
        if path[0] in {'inputs', 'outputs'}:
            path.insert(0, 'properties')
        owner = mod
        for field in path[:-1]:
            owner = owner[int(field)] if field.isdecimal() else getattr(owner, field)
        return owner, path[-1]
    return mod, identifier


def _modifier_nested_parameters(mod):
    values, visited = {}, set()

    def collect(value, path):
        if value is None or value.as_pointer() in visited:
            return
        visited.add(value.as_pointer())
        for prop in value.bl_rna.properties:
            if prop.identifier in {'rna_type', 'id_data'}:
                continue
            child = getattr(value, prop.identifier)
            child_path = path + [prop.identifier]
            if prop.is_readonly:
                if prop.type == 'POINTER' and child is not None and not isinstance(child, bpy.types.ID):
                    collect(child, child_path)
                elif prop.type == 'COLLECTION':
                    for index, item in enumerate(child):
                        if not isinstance(item, bpy.types.ID):
                            collect(item, child_path + [str(index)])
            elif prop.type != 'COLLECTION':
                values['@/' + '/'.join(child_path)] = child

    for prop in mod.bl_rna.properties:
        if (not prop.is_readonly or prop.type not in {'POINTER', 'COLLECTION'}
                or prop.identifier in {'rna_type', 'id_data', 'properties', 'particle_system',
                                       'bakes', 'node_warnings', 'panels'}):
            continue
        child = getattr(mod, prop.identifier)
        if prop.type == 'POINTER' and child is not None and not isinstance(child, bpy.types.ID):
            collect(child, [prop.identifier])
        elif prop.type == 'COLLECTION':
            for index, item in enumerate(child):
                if not isinstance(item, bpy.types.ID):
                    collect(item, [prop.identifier, str(index)])
    return values


def _get_modifier_parameter(mod, identifier):
    owner, name = _modifier_parameter_owner(mod, identifier)
    return getattr(owner, name)


def _modifier_parameter_label(mod, identifier):
    if mod is None:
        return "参数"
    if identifier.startswith('@/inputs/') or identifier.startswith('@/outputs/'):
        if mod.node_group is None:
            return "参数"
        socket_id = identifier.split('/')[2]
        socket = next((item for item in mod.node_group.interface.items_tree
                       if item.item_type == 'SOCKET' and item.identifier == socket_id), None)
        if socket is not None:
            return socket.name
    try:
        owner, name = _modifier_parameter_owner(mod, identifier)
        return bpy.app.translations.pgettext_iface(owner.bl_rna.properties[name].name)
    except (AttributeError, KeyError):
        return "参数"


def _set_modifier_parameter(mod, identifier, value):
    owner, name = _modifier_parameter_owner(mod, identifier)
    prop = owner.bl_rna.properties[name]
    if prop.type == 'ENUM' and prop.is_enum_flag:
        value = set(value)
    setattr(owner, name, value)


def _modifier_description(mod):
    try:
        inputs = mod.items() if mod.type == 'NODES' else ()
    except TypeError:
        # Nodes modifiers without allocated input overrides have no IDProperties.
        inputs = ()
    return {
        'name': mod.name, 'type': mod.type,
        'node_properties': _modifier_node_properties(mod),
        'nested_properties': {key: _modifier_history_value(value)
                              for key, value in _modifier_nested_parameters(mod).items()},
        'properties': {name: _modifier_history_value(getattr(mod, name))
                       for name in ModifierAutoSync.get_snapshot(mod, include_display=True)
                       if name in mod.bl_rna.properties and mod.bl_rna.properties[name].type != 'COLLECTION'},
        'inputs': {name: _modifier_history_value(value) for name, value in inputs
                   if not name.startswith('_')},
    }


def _create_synced_modifier(obj, description):
    mod = obj.modifiers.new(name=description['name'], type=description['type'])
    try:
        for name, value in description['properties'].items():
            if _modifier_history_value(_get_modifier_parameter(mod, name)) == value:
                continue
            if isinstance(value, dict):
                value = _modifier_history_id(value)
            _set_modifier_parameter(mod, name, value)
        for name, value in description['inputs'].items():
            mod[name] = _modifier_history_id(value) if isinstance(value, dict) else value
        for name, value in description.get('nested_properties', {}).items():
            if _modifier_history_value(_get_modifier_parameter(mod, name)) != value:
                _set_modifier_parameter(mod, name,
                                        _modifier_history_id(value) if isinstance(value, dict) else value)
        for category, sockets in description.get('node_properties', {}).items():
            for identifier, fields in sockets.items():
                target = getattr(getattr(mod.properties, category), identifier)
                for name, value in fields.items():
                    setattr(target, name, _modifier_history_id(value) if isinstance(value, dict) else value)
    except (AttributeError, TypeError, ValueError, RuntimeError):
        obj.modifiers.remove(mod)
        raise
    return mod


def _sync_added_modifiers(scene, source_obj, objects, queue_native=True):
    operator = bpy.context.active_operator
    if (queue_native and not bpy.app.background and operator is not None
            and operator.bl_idname in {'OBJECT_OT_modifier_add', 'OBJECT_OT_modifier_add_node_group'}):
        added = [mod for mod in source_obj.modifiers if _is_user_modifier(mod)
                 and ModifierAutoSync.find_snapshot(scene, source_obj, mod) is None]
        if added:
            mod = added[-1]
            _queue_modifier_operation(scene, source_obj, objects, mod.name, mod.type, operator)
            return [], 0, set()
    changes, added_count, skipped = [], 0, set()
    for mod in source_obj.modifiers:
        if not _is_user_modifier(mod) or ModifierAutoSync.find_snapshot(scene, source_obj, mod) is not None:
            continue
        targets = [obj for obj in objects if obj != source_obj]
        # Existing modifiers must keep their settings. Reserve one common new
        # name so subsequent parameter edits still use name/type matching.
        conflicts = any(
            (existing := obj.modifiers.get(mod.name)) is not None
            and (ModifierAutoSync.find_snapshot(scene, obj, existing) is not None
                 or existing.type != mod.type)
            for obj in targets
        )
        if conflicts:
            base, separator, suffix = mod.name.rpartition('.')
            base = base if separator and suffix.isdigit() else mod.name
            index = 1
            while any(f'{base}.{index:03}' in obj.modifiers for obj in objects):
                index += 1
            mod.name = f'{base}.{index:03}'
        description = _modifier_description(mod)
        change = {'kind': 'ADD', 'source': source_obj.name,
                  'source_uid': mod.persistent_uid, 'modifier': mod.name,
                  'type': mod.type, 'property': '__presence__',
                  'before': False, 'after': True, 'description': description,
                  'targets': []}
        for obj in targets:
            if obj.modifiers.get(mod.name) is not None:
                continue
            try:
                new_mod = _create_synced_modifier(obj, description)
                change['targets'].append({'object': obj.name, 'uid': new_mod.persistent_uid})
                added_count += 1
            except (AttributeError, TypeError, ValueError, RuntimeError, OSError):
                skipped.add(obj.name)
        if change['targets']:
            changes.append(change)
    return changes, added_count, skipped


def _sync_removed_modifiers(scene, source_obj, objects, depsgraph):
    state = ModifierAutoSync.get_state(scene)
    current = {mod.persistent_uid for mod in source_obj.modifiers}
    removed = [(entry.modifier_name, entry.modifier_uid, entry.modifier_type)
               for entry in state.snapshots
               if entry.object_name == source_obj.name and entry.modifier_uid not in current]
    operator = bpy.context.active_operator
    native_name = (operator.properties.modifier if operator is not None
                   and operator.bl_idname in {'OBJECT_OT_modifier_apply', 'OBJECT_OT_modifier_remove'} else None)
    mesh_changed = any(update.id.original == source_obj.data and update.is_updated_geometry
                       for update in depsgraph.updates)
    changes, count, skipped = [], 0, set()
    for name, uid, type_name in removed:
        applying = mesh_changed and name == native_name and operator.bl_idname == 'OBJECT_OT_modifier_apply'
        native_remove = name == native_name and operator is not None and operator.bl_idname == 'OBJECT_OT_modifier_remove'
        if applying or native_remove:
            _queue_modifier_operation(scene, source_obj, objects, name, type_name, operator)
            continue
        change = {'kind': 'REMOVE', 'source': source_obj.name, 'source_uid': uid,
                  'modifier': name, 'type': type_name, 'property': '__presence__',
                  'before': True, 'after': False, 'targets': []}
        for obj in objects:
            if obj == source_obj:
                continue
            mod = obj.modifiers.get(name)
            if mod is None or mod.type != type_name:
                continue
            try:
                change['targets'].append({'object': obj.name, 'uid': mod.persistent_uid,
                                          'description': _modifier_description(mod)})
                obj.modifiers.remove(mod)
                count += 1
            except (AttributeError, TypeError, ValueError, RuntimeError, OSError):
                skipped.add(obj.name)
        if change['targets']:
            changes.append(change)
    return changes, count, skipped


def _queue_modifier_operation(scene, source, objects, name, type_name, operator):
    history = _modifier_history(scene, create=True)
    token = str(operator.as_pointer())
    if history.last_operation_token == token:
        return
    history.last_operation_token = token
    while len(history.changes) > history.change_cursor:
        history.changes.remove(len(history.changes) - 1)
    addition = operator.bl_idname in {'OBJECT_OT_modifier_add', 'OBJECT_OT_modifier_add_node_group'}
    if operator.properties.use_selected_objects and not addition:
        return
    targets = [obj.name for obj in objects if obj != source
               and (addition or ((mod := obj.modifiers.get(name)) is not None and mod.type == type_name))]
    if not targets:
        return
    history.pending_operation = json.dumps({
        'scene': scene.name, 'source': source.name, 'targets': targets,
        'modifier': name, 'type': type_name, 'token': token,
        'window': str(bpy.context.window.as_pointer()) if bpy.context.window else '',
        'undo': bpy.context.preferences.edit.use_global_undo,
        'addition': addition,
        'operator': {'OBJECT_OT_modifier_apply': 'modifier_apply',
                     'OBJECT_OT_modifier_remove': 'modifier_remove',
                     'OBJECT_OT_modifier_add': 'modifier_add',
                     'OBJECT_OT_modifier_add_node_group': 'modifier_add_node_group'}[operator.bl_idname],
        'options': {key: getattr(operator.properties, key)
                    for key in ('merge_customdata', 'single_user', 'all_keyframes', 'type',
                                'asset_library_type', 'asset_library_identifier',
                                'relative_asset_identifier', 'session_uid')
                    if hasattr(operator.properties, key)},
    })
    if not bpy.app.timers.is_registered(_finish_modifier_operation):
        bpy.app.timers.register(_finish_modifier_operation, first_interval=0.0)


def _modifier_request_context(request):
    window = next((win for win in bpy.context.window_manager.windows
                   if str(win.as_pointer()) == request['window']), None)
    if window is None:
        return None
    area = next((area for area in window.screen.areas
                 if area.type in {'PROPERTIES', 'VIEW_3D'}), None)
    if area is None:
        return None
    region = next(region for region in area.regions if region.type == 'WINDOW')
    return {'window': window, 'area': area, 'region': region}


def _finish_modifier_operation():
    wm = bpy.context.window_manager
    requests = []
    for history in wm.um_modifier_sync_histories:
        if history.pending_operation:
            requests.append(json.loads(history.pending_operation))
            history.pending_operation = ''
    for request in requests:
        label = '新增' if request['addition'] else ('应用' if request['operator'] == 'modifier_apply' else '删除')
        scene = bpy.data.scenes.get(request['scene'])
        override = _modifier_request_context(request)
        if scene is None or not scene.um_auto_sync_modifiers or override is None:
            continue
        state = ModifierAutoSync.get_state(scene)
        if not wm.operators or str(wm.operators[-1].as_pointer()) != request['token']:
            state.warning_message = f"操作已改变，本次{label}未扩展到其他物体"
            continue
        # Replace the just-finished single-object step with Blender's own
        # selected-object operation, preserving geometry and modifier data.
        if request['undo']:
            try:
                with bpy.context.temp_override(**override):
                    result = bpy.ops.ed.undo()
            except RuntimeError:
                result = {'CANCELLED'}
            if 'FINISHED' not in result:
                state.warning_message = f"无法准备多物体{label}，请检查撤销状态"
                continue
        scene = bpy.data.scenes.get(request['scene'])
        history = _modifier_history(scene, create=True)
        names = ([request['source']] if request['undo'] or request['addition'] else []) + request['targets']
        objects = [obj for name in names if (obj := bpy.data.objects.get(name)) is not None
                   and obj.is_editable and (request['addition'] or
                       ((mod := obj.modifiers.get(request['modifier'])) is not None and mod.type == request['type']))]
        if not objects:
            continue
        override = _modifier_request_context(request)
        if override is None:
            continue
        history.is_restoring = True
        try:
            before = {
                obj.name: {identifier: _modifier_history_value(_get_modifier_parameter(mod, identifier))
                           for identifier in ModifierAutoSync.get_snapshot(mod)}
                for obj in objects if not request['addition']
                for mod in (obj.modifiers[request['modifier']],)
            }
            before_uids = {obj.name: {mod.persistent_uid for mod in obj.modifiers} for obj in objects}
            with bpy.context.temp_override(**override, object=objects[0], active_object=objects[0],
                                           selected_objects=objects, selected_editable_objects=objects):
                if request['addition']:
                    result = bpy.ops.object.uvmm_add_synced_native(
                        'EXEC_DEFAULT', True, native_operator=request['operator'],
                        options=json.dumps(request['options']),
                        existing_modifier='' if request['undo'] else request['modifier'])
                else:
                    native_operator = getattr(bpy.ops.object, request['operator'])
                    result = native_operator(
                        'EXEC_DEFAULT', True, modifier=request['modifier'],
                        use_selected_objects=True, **request['options'])
                bpy.context.view_layer.update()
            if not request['addition'] and 'FINISHED' in result:
                change = {'kind': 'NATIVE_STACK', 'source': objects[0].name,
                          'modifier': request['modifier'], 'type': request['type'],
                          'property': '__presence__', 'before': True, 'after': False,
                          'values': before, 'targets': []}
                entry = history.changes.add()
                entry.data = json.dumps([change])
                while len(history.changes) > max(64, bpy.context.preferences.edit.undo_steps * 128):
                    history.changes.remove(0)
                history.change_cursor = len(history.changes)
            modifier_sync_reset_handler()
            state = ModifierAutoSync.get_state(scene)
            applied = sum(any(mod.persistent_uid not in before_uids[obj.name] for mod in obj.modifiers)
                          for obj in objects) if request['addition'] else sum(
                request['modifier'] not in obj.modifiers for obj in objects)
            state.status_message = f"已{label} {applied} 个物体的修改器"
            skipped = len(names) - applied
            if skipped:
                state.warning_message = f"{skipped} 个物体未通过原生{label}检查"
        finally:
            history.is_restoring = False
            history.last_operation_token = ''
    return None


def _modifier_history_id(value):
    id_type = getattr(bpy.types, value['id_type'])
    for prop in bpy.data.bl_rna.properties:
        if prop.type != 'COLLECTION':
            continue
        collection = getattr(bpy.data, prop.identifier)
        for candidate in collection:
            if (isinstance(candidate, id_type) and candidate.name_full == value['name']
                    and (candidate.library.filepath if candidate.library else '') == value['library']):
                return candidate
    raise ValueError('Referenced datablock no longer exists')


def _history_modifier(change, object_name):
    obj = bpy.data.objects.get(object_name)
    if obj is None:
        return None
    if object_name == change['source'] and 'source_uid' in change:
        mod = next((mod for mod in obj.modifiers
                    if mod.persistent_uid == change['source_uid']), None)
        if mod is None:
            mod = obj.modifiers.get(change['modifier'])
    else:
        mod = obj.modifiers.get(change['modifier'])
    return mod if mod is not None and mod.type == change['type'] else None


def _history_key(change):
    return json.dumps([change['source'], change['modifier'], change['type'], change['property']])


def _participant_key(change, object_name):
    return json.dumps([object_name, change['modifier'], change['type'], change['property']])


@persistent
def modifier_sync_undo_pre(*_args):
    # Native RNA edits push their undo step before depsgraph callbacks write
    # recipients. Keep those recipient edits paired with the source edit.
    for history in bpy.context.window_manager.um_modifier_sync_histories:
        baseline, participants = {}, {}
        for entry in history.changes:
            for change in json.loads(entry.data):
                mod = _history_modifier(change, change['source'])
                if change.get('kind') in {'ADD', 'REMOVE', 'NATIVE_STACK'}:
                    baseline[_history_key(change)] = mod is not None
                elif mod is not None:
                    try:
                        baseline[_history_key(change)] = ModifierAutoSync.serialize_value(
                            _get_modifier_parameter(mod, change['property']))
                    except AttributeError:
                        continue
                if change.get('kind') in {'ADD', 'REMOVE', 'NATIVE_STACK'}:
                    continue
                names = [change['source']] + [target['object'] for target in change['targets']]
                for object_name in names:
                    participant_mod = _history_modifier(change, object_name)
                    if participant_mod is None:
                        continue
                    try:
                        value = _modifier_history_value(_get_modifier_parameter(participant_mod, change['property']))
                    except AttributeError:
                        continue
                    participants[_participant_key(change, object_name)] = {
                        'source': object_name, 'source_uid': participant_mod.persistent_uid,
                        'modifier': change['modifier'], 'type': change['type'],
                        'property': change['property'], 'value': value,
                    }
        history.undo_baseline = json.dumps(baseline)
        history.undo_participants = json.dumps(participants)
        history.is_restoring = True


def _restore_modifier_history(history, direction):
    expected = json.loads(history.undo_baseline or '{}')
    indices = (range(history.change_cursor - 1, -1, -1) if direction < 0
               else range(history.change_cursor, len(history.changes)))
    planned, records, valid_keys = [], {}, set()
    old, new = ('after', 'before') if direction < 0 else ('before', 'after')
    for index in indices:
        changes = json.loads(history.changes[index].data)
        records[index] = changes
        for change in reversed(changes) if direction < 0 else changes:
            key = _history_key(change)
            mod = _history_modifier(change, change['source'])
            if key not in expected:
                continue
            is_stack_change = change.get('kind') in {'ADD', 'REMOVE', 'NATIVE_STACK'}
            if mod is None and not is_stack_change:
                continue
            try:
                actual = (mod is not None if is_stack_change else
                          ModifierAutoSync.serialize_value(_get_modifier_parameter(mod, change['property'])))
            except AttributeError:
                continue
            if actual == expected[key] or expected[key] != change[old]:
                continue
            planned.append((index, change))
            expected[key] = change[new]
            if expected[key] == actual:
                valid_keys.add(key)
    # An unrelated native edit can invalidate a redo branch. Replay only
    # complete journal paths which explain the source's actual restored value.
    protected = set()
    for _, change in planned:
        if _history_key(change) in valid_keys:
            protected.add(_participant_key(change, change['source']))
            protected.update(_participant_key(change, target['object']) for target in change['targets'])
    for key, participant in json.loads(history.undo_participants or '{}').items():
        if key in protected:
            continue
        mod = _history_modifier(participant, participant['source'])
        if mod is None:
            continue
        value = participant['value']
        try:
            if _modifier_history_value(_get_modifier_parameter(mod, participant['property'])) != value:
                _set_modifier_parameter(mod, participant['property'],
                                        _modifier_history_id(value) if isinstance(value, dict) else value)
        except (AttributeError, TypeError, ValueError, RuntimeError):
            continue
    for index, change in planned:
        if _history_key(change) not in valid_keys:
            continue
        if change.get('kind') in {'ADD', 'REMOVE', 'NATIVE_STACK'}:
            _restore_added_modifier(history, change, direction)
        else:
            for target in change['targets']:
                target_mod = _history_modifier(change, target['object'])
                if target_mod is None:
                    continue
                value = target[new]
                try:
                    if isinstance(value, dict):
                        value = _modifier_history_id(value)
                    _set_modifier_parameter(target_mod, change['property'], value)
                except (AttributeError, TypeError, ValueError, RuntimeError):
                    continue
        history.changes[index].data = json.dumps(records[index])
        history.change_cursor = index if direction < 0 else index + 1


def _restore_added_modifier(history, change, direction):
    should_exist = change['after'] if direction > 0 else change['before']
    if change['kind'] == 'NATIVE_STACK':
        # Blender restores opaque modifier/mesh data; repair only the RNA
        # values written after an earlier native parameter undo snapshot.
        if should_exist:
            for object_name, values in change['values'].items():
                mod = _history_modifier(change, object_name)
                if mod is None:
                    continue
                for identifier, value in values.items():
                    try:
                        _set_modifier_parameter(mod, identifier,
                                                _modifier_history_id(value) if isinstance(value, dict) else value)
                    except (AttributeError, TypeError, ValueError, RuntimeError):
                        continue
        return
    if change['kind'] == 'ADD' and direction > 0:
        source = _history_modifier(change, change['source'])
        if source is not None:
            source.name = change['modifier']
    for target in change['targets']:
        obj = bpy.data.objects.get(target['object'])
        if obj is None:
            continue
        mod = next((mod for mod in obj.modifiers if mod.persistent_uid == target['uid']), None)
        if mod is None:
            mod = obj.modifiers.get(change['modifier'])
        if not should_exist:
            if mod is not None and mod.type == change['type']:
                obj.modifiers.remove(mod)
        elif mod is None:
            try:
                description = target.get('description', change.get('description'))
                mod = _create_synced_modifier(obj, description)
                target['uid'] = mod.persistent_uid
            except (AttributeError, TypeError, ValueError, RuntimeError):
                continue


def _restore_modifier_sync_history(direction):
    histories = bpy.context.window_manager.um_modifier_sync_histories
    try:
        for history in histories:
            scene = bpy.data.scenes.get(history.scene_name)
            if scene is not None and scene.um_auto_sync_modifiers:
                _restore_modifier_history(history, direction)
        modifier_sync_reset_handler()
    finally:
        for history in histories:
            history.is_restoring = False


@persistent
def modifier_sync_undo_post(*_args):
    _restore_modifier_sync_history(-1)


@persistent
def modifier_sync_redo_post(*_args):
    _restore_modifier_sync_history(1)


def update_modifier_sync_enabled(scene, context):
    state = ModifierAutoSync.get_state(scene)
    if state is None:
        return
    state.snapshots.clear()
    state.selection_signature = ""
    state.status_message = ""
    state.warning_message = ""
    if not scene.um_auto_sync_modifiers:
        return
    active = context.view_layer.objects.active
    objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
    if active is not None and active.type == 'MESH' and objects:
        state.selection_signature = json.dumps(
            (active.name, tuple(sorted(obj.name for obj in objects)))
        )
        ModifierAutoSync.ensure_snapshots(scene, objects)


def on_modifier_sync_toggle(scene, context):
    history = _modifier_history(scene)
    if history is not None:
        while len(history.changes) > history.change_cursor:
            history.changes.remove(len(history.changes) - 1)
    update_modifier_sync_enabled(scene, context)


def draw_modifier_sync_controls(layout, context):
    enabled = context.scene.um_auto_sync_modifiers
    objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
    if not objects and context.object and context.object.type == 'MESH':
        objects = [context.object]
    count = len(objects)
    text = "同步已开启" if enabled else "同步已关闭"
    split = layout.split(factor=0.4, align=True)
    toggle_row = split.row(align=True)
    toggle_row.alert = enabled
    toggle_row.prop(context.scene, "um_auto_sync_modifiers",
                    text=text, icon='LINKED' if enabled else 'UNLINKED', toggle=True)
    state = ModifierAutoSync.get_state(context.scene)
    messages = [message for message in (state.status_message, state.warning_message) if message] if state else []
    draw_status_tooltip(split.row(align=True), '；'.join(messages) if messages else f"已选 {count} 个物体")


class DATA_PT_UVMMModifierSync(Panel):
    bl_label = "修改器同步"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'modifier'
    bl_options = {'HIDE_HEADER'}
    bl_order = -1000

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.type == 'MESH'

    def draw(self, context):
        layout = self.layout
        draw_modifier_sync_controls(layout, context)


# =========================================================================
# 操作符定义
# =========================================================================

class MODIFIER_OT_AddSyncedNative(Operator):
    bl_idname = "object.uvmm_add_synced_native"
    bl_label = "同步新增修改器"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    native_operator: StringProperty(default='modifier_add', options={'HIDDEN'})
    options: StringProperty(default='{"type":"SUBSURF"}', options={'HIDDEN'})
    existing_modifier: StringProperty(options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.type == 'MESH'

    def execute(self, context):
        scene = context.scene
        objects = [obj for obj in context.selected_objects if obj.type == 'MESH' and obj.is_editable]
        history = _modifier_history(scene, create=True)
        previous = history.is_restoring
        history.is_restoring = True
        try:
            ModifierAutoSync.ensure_snapshots(scene, objects)
            if self.existing_modifier:
                ModifierAutoSync.remove_snapshot(scene, context.object, self.existing_modifier)
            else:
                if self.native_operator not in {'modifier_add', 'modifier_add_node_group'}:
                    return {'CANCELLED'}
                result = getattr(bpy.ops.object, self.native_operator)(
                    'EXEC_DEFAULT', False, use_selected_objects=False, **json.loads(self.options))
                if 'FINISHED' not in result:
                    return result
            _, count, skipped = _sync_added_modifiers(scene, context.object, objects, queue_native=False)
            context.view_layer.update()
            ModifierAutoSync.ensure_snapshots(scene, objects)
            if skipped:
                self.report({'WARNING'}, f"已跳过 {len(skipped)} 个无法新增修改器的物体")
            return {'FINISHED'}
        finally:
            history.is_restoring = previous


class MODIFIER_OT_SelectByModifier(Operator):
    """在场景中选中所有使用该修改器的物体"""
    bl_idname = "object.select_by_modifier"
    bl_label = "选择使用此修改器的物体"
    bl_options = {'REGISTER', 'UNDO'}

    modifier_name: StringProperty(name="修改器名称")
    modifier_type: StringProperty(name="修改器类型")

    def execute(self, context):
        matched = []
        for obj in context.view_layer.objects:
            if obj.type == 'MESH':
                if self.modifier_name in obj.modifiers:
                    matched.append(obj)
                elif self.modifier_type and any(m.type == self.modifier_type for m in obj.modifiers):
                    matched.append(obj)

        if not matched:
            self.report({'WARNING'}, f"场景中没有使用修改器 {self.modifier_name} 的网格物体")
            return {'CANCELLED'}

        bpy.ops.object.select_all(action='DESELECT')
        for obj in matched:
            obj.select_set(True)
        context.view_layer.objects.active = matched[0]
        self.report({'INFO'}, f"已在场景中选中 {len(matched)} 个使用修改器 '{self.modifier_name}' 的物体")
        set_modifier_status(context, f"已选择 {len(matched)} 个使用 {self.modifier_name} 的物体")
        return {'FINISHED'}


class MODIFIER_OT_ToggleExpanded(Operator):
    """展开或折叠修改器"""
    bl_idname = "object.custom_modifier_toggle_expanded"
    bl_label = "展开/折叠修改器"
    bl_options = {'INTERNAL'}

    modifier_name: StringProperty(name="修改器名称")
    target_object_name: StringProperty(name="目标物体名称", default="")

    def execute(self, context):
        obj = None
        if self.target_object_name:
            obj = bpy.data.objects.get(self.target_object_name)
        if not obj:
            obj = context.active_object

        if obj and self.modifier_name in obj.modifiers:
            mod = obj.modifiers[self.modifier_name]
            mod.show_expanded = not mod.show_expanded
        return {'FINISHED'}


class MODIFIER_OT_AddCustom(Operator):
    """为选中的所有网格物体添加修改器"""
    bl_idname = "object.add_custom_modifier"
    bl_label = "添加修改器"
    bl_options = {'REGISTER', 'UNDO'}

    modifier_type: StringProperty(name="修改器类型")

    def execute(self, context):
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not selected_objs and context.active_object and context.active_object.type == 'MESH':
            selected_objs = [context.active_object]

        if not selected_objs:
            self.report({'WARNING'}, "未选择任何网格物体")
            return {'CANCELLED'}

        added_count = 0
        for obj in selected_objs:
            try:
                mod = obj.modifiers.new(
                    name=self.modifier_type.capitalize().replace('_', ' '),
                    type=self.modifier_type
                )
                ModifierAutoSync.set_snapshot(context.scene, obj, mod)
                added_count += 1
            except Exception as e:
                self.report({'WARNING'}, f"在物体 {obj.name} 上添加修改器失败: {str(e)}")

        self.report({'INFO'}, f"已为 {added_count} 个物体添加修改器")
        set_modifier_status(context, f"已添加修改器到 {added_count} 个物体")
        return {'FINISHED'}


class MODIFIER_OT_ApplyToAll(Operator):
    """应用该修改器到所有拥有它的选中物体"""
    bl_idname = "object.apply_modifier_to_all"
    bl_label = "应用修改器到全部"
    bl_options = {'REGISTER', 'UNDO'}

    modifier_name: StringProperty(name="修改器名称")

    def execute(self, context):
        processed = 0
        errors = []
        original_active = context.view_layer.objects.active

        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue

            mod = obj.modifiers.get(self.modifier_name)
            if not mod:
                continue

            try:
                context.view_layer.objects.active = obj
                bpy.ops.object.modifier_apply(modifier=mod.name)
                ModifierAutoSync.remove_snapshot(context.scene, obj, mod.name)
                processed += 1
            except Exception as e:
                errors.append(f"{obj.name}: {str(e)}")

        context.view_layer.objects.active = original_active

        if errors:
            self.report({'WARNING'}, f"{processed}个成功，失败{len(errors)}个：{', '.join(errors)}")
            set_modifier_status(context, f"应用修改器：成功 {processed} 个，失败 {len(errors)} 个")
        else:
            self.report({'INFO'}, f"成功应用到 {processed} 个物体")
            set_modifier_status(context, f"已应用修改器到 {processed} 个物体")
        return {'FINISHED'}


class MODIFIER_OT_RemoveFromSelected(Operator):
    """从选中的物体中移除此修改器"""
    bl_idname = "object.remove_modifier_from_selected"
    bl_label = "批量移除修改器"
    bl_options = {'REGISTER', 'UNDO'}

    modifier_name: StringProperty(name="修改器名称")

    def execute(self, context):
        processed = 0
        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue
            if mod := obj.modifiers.get(self.modifier_name):
                ModifierAutoSync.remove_snapshot(context.scene, obj, mod.name)
                obj.modifiers.remove(mod)
                processed += 1

        self.report({'INFO'}, f"已从 {processed} 个物体移除修改器")
        set_modifier_status(context, f"已从 {processed} 个物体移除修改器")
        return {'FINISHED'}


class MODIFIER_OT_SyncToSelected(Operator):
    """将当前活动物体的此修改器所有参数立即覆盖同步到选中的其他物体"""
    bl_idname = "object.sync_modifier_to_selected"
    bl_label = "同步参数到选中物体"
    bl_description = "将当前物体的此修改器所有参数立即复制覆盖到所有选中的其他物体"
    bl_options = {'REGISTER', 'UNDO'}

    modifier_name: StringProperty(name="修改器名称")

    def execute(self, context):
        active_obj = context.active_object
        if not active_obj or self.modifier_name not in active_obj.modifiers:
            self.report({'WARNING'}, "未找到活动物体的修改器")
            return {'CANCELLED'}

        src_mod = active_obj.modifiers[self.modifier_name]
        curr_snap = ModifierAutoSync.get_snapshot(src_mod, include_display=True)
        selected_objs = [o for o in context.selected_objects if o.type == 'MESH' and o != active_obj]

        if not selected_objs:
            self.report({'INFO'}, "没有其他选中的网格物体需要同步")
            return {'FINISHED'}

        synced_count = 0
        for target_obj in selected_objs:
            target_mod = target_obj.modifiers.get(src_mod.name)
            if not target_mod:
                for m in target_obj.modifiers:
                    if m.type == src_mod.type:
                        target_mod = m
                        break
            if not target_mod:
                try:
                    target_mod = target_obj.modifiers.new(name=src_mod.name, type=src_mod.type)
                except Exception:
                    continue

            if target_mod:
                for prop_id, curr_val in curr_snap.items():
                    if hasattr(target_mod, prop_id):
                        try:
                            raw_val = getattr(src_mod, prop_id)
                            if isinstance(curr_val, (tuple, list)):
                                setattr(target_mod, prop_id, list(raw_val))
                            else:
                                setattr(target_mod, prop_id, raw_val)
                        except Exception:
                            pass
                ModifierAutoSync.set_snapshot(context.scene, target_obj, target_mod)
                synced_count += 1

        self.report({'INFO'}, f"✨ 已成功将修改器 '{src_mod.name}' 同步到 {synced_count} 个物体")
        set_modifier_status(context, f"已手动同步 {src_mod.name} 到 {synced_count} 个物体")
        return {'FINISHED'}


# =========================================================================
# 添加修改器分类菜单（与 Blender 原生一致）
# =========================================================================

class OBJECT_MT_CustomModifierAdd(Menu):
    bl_idname = "OBJECT_MT_custom_modifier_add"
    bl_label = "添加修改器"

    def draw(self, context):
        layout = self.layout
        layout.menu("OBJECT_MT_custom_modifier_add_modify", text="修改 (Edit/Modify)", icon='MOD_DATA_TRANSFER')
        layout.menu("OBJECT_MT_custom_modifier_add_generate", text="生成 (Generate)", icon='MOD_BEVEL')
        layout.menu("OBJECT_MT_custom_modifier_add_deform", text="变形 (Deform)", icon='MOD_SIMPLEDEFORM')
        layout.menu("OBJECT_MT_custom_modifier_add_physics", text="物理 (Physics)", icon='MOD_PHYSICS')


class OBJECT_MT_CustomModifierAdd_Modify(Menu):
    bl_idname = "OBJECT_MT_custom_modifier_add_modify"
    bl_label = "修改 (Modify)"

    def draw(self, context):
        layout = self.layout
        items = [
            ("DATA_TRANSFER", "数据传递 (Data Transfer)", 'MOD_DATA_TRANSFER'),
            ("NORMAL_EDIT", "法向编辑 (Normal Edit)", 'MOD_NORMALEDIT'),
            ("WEIGHTED_NORMAL", "加权法向 (Weighted Normal)", 'MOD_NORMALEDIT'),
            ("UV_PROJECT", "UV 投射 (UV Project)", 'MOD_UVPROJECT'),
            ("UV_WARP", "UV 扭曲 (UV Warp)", 'MOD_UVPROJECT'),
            ("VERTEX_WEIGHT_EDIT", "顶点权重编辑 (Vertex Weight Edit)", 'MOD_VERTEX_WEIGHT'),
            ("VERTEX_WEIGHT_MIX", "顶点权重混合 (Vertex Weight Mix)", 'MOD_VERTEX_WEIGHT'),
            ("VERTEX_WEIGHT_PROXIMITY", "顶点权重邻近 (Vertex Weight Proximity)", 'MOD_VERTEX_WEIGHT'),
        ]
        for type_id, name, icon in items:
            op = layout.operator("object.add_custom_modifier", text=name, icon=icon)
            op.modifier_type = type_id


class OBJECT_MT_CustomModifierAdd_Generate(Menu):
    bl_idname = "OBJECT_MT_custom_modifier_add_generate"
    bl_label = "生成 (Generate)"

    def draw(self, context):
        layout = self.layout
        items = [
            ("ARRAY", "阵列 (Array)", 'MOD_ARRAY'),
            ("BEVEL", "倒角 (Bevel)", 'MOD_BEVEL'),
            ("BOOLEAN", "布尔 (Boolean)", 'MOD_BOOLEAN'),
            ("BUILD", "建形 (Build)", 'MOD_BUILD'),
            ("DECIMATE", "精简/减面 (Decimate)", 'MOD_DECIM'),
            ("EDGE_SPLIT", "边缘分拆 (Edge Split)", 'MOD_EDGESPLIT'),
            ("NODES", "几何节点 (Geometry Nodes)", 'GEOMETRY_NODES'),
            ("MASK", "遮罩 (Mask)", 'MOD_MASK'),
            ("MIRROR", "镜像 (Mirror)", 'MOD_MIRROR'),
            ("MULTIRES", "多级分辨率 (Multiresolution)", 'MOD_MULTIRES'),
            ("REMESH", "重构网格 (Remesh)", 'MOD_REMESH'),
            ("SCREW", "螺旋 (Screw)", 'MOD_SCREW'),
            ("SKIN", "蒙皮/皮肤 (Skin)", 'MOD_SKIN'),
            ("SOLIDIFY", "实体化 (Solidify)", 'MOD_SOLIDIFY'),
            ("SUBSURF", "细分曲面 (Subdivision Surface)", 'MOD_SUBSURF'),
            ("TRIANGULATE", "三角化 (Triangulate)", 'MOD_TRIANGULATE'),
            ("WELD", "焊接 (Weld)", 'AUTOMERGE_OFF'),
            ("WIREFRAME", "线框 (Wireframe)", 'MOD_WIREFRAME'),
        ]
        for type_id, name, icon in items:
            op = layout.operator("object.add_custom_modifier", text=name, icon=icon)
            op.modifier_type = type_id


class OBJECT_MT_CustomModifierAdd_Deform(Menu):
    bl_idname = "OBJECT_MT_custom_modifier_add_deform"
    bl_label = "变形 (Deform)"

    def draw(self, context):
        layout = self.layout
        items = [
            ("ARMATURE", "骨架 (Armature)", 'MOD_ARMATURE'),
            ("CAST", "铸型 (Cast)", 'MOD_CAST'),
            ("CURVE", "曲线 (Curve)", 'MOD_CURVE'),
            ("DISPLACE", "置换 (Displace)", 'MOD_DISPLACE'),
            ("HOOK", "钩挂 (Hook)", 'HOOK'),
            ("LAPLACIANDEFORM", "拉普拉斯变形 (Laplacian Deform)", 'MOD_MESHDEFORM'),
            ("LAPLACIANSMOOTH", "拉普拉斯平滑 (Laplacian Smooth)", 'MOD_SMOOTH'),
            ("LATTICE", "晶格 (Lattice)", 'MOD_LATTICE'),
            ("MESH_DEFORM", "网格变形 (Mesh Deform)", 'MOD_MESHDEFORM'),
            ("SHRINKWRAP", "收缩包裹 (Shrinkwrap)", 'MOD_SHRINKWRAP'),
            ("SIMPLE_DEFORM", "简易形变 (Simple Deform)", 'MOD_SIMPLEDEFORM'),
            ("SMOOTH", "平滑 (Smooth)", 'MOD_SMOOTH'),
            ("CORRECTIVE_SMOOTH", "平滑修正 (Corrective Smooth)", 'MOD_SMOOTH'),
            ("SURFACE_DEFORM", "表面变形 (Surface Deform)", 'MOD_MESHDEFORM'),
            ("WARP", "弯折/扭曲 (Warp)", 'MOD_WARP'),
            ("WAVE", "波浪 (Wave)", 'MOD_WAVE'),
        ]
        for type_id, name, icon in items:
            op = layout.operator("object.add_custom_modifier", text=name, icon=icon)
            op.modifier_type = type_id


class OBJECT_MT_CustomModifierAdd_Physics(Menu):
    bl_idname = "OBJECT_MT_custom_modifier_add_physics"
    bl_label = "物理 (Physics)"

    def draw(self, context):
        layout = self.layout
        items = [
            ("CLOTH", "布料 (Cloth)", 'MOD_CLOTH'),
            ("COLLISION", "碰撞 (Collision)", 'MOD_PHYSICS'),
            ("DYNAMIC_PAINT", "动态画笔 (Dynamic Paint)", 'MOD_DYNAMICPAINT'),
            ("FLUID", "流体 (Fluid)", 'MOD_FLUID'),
            ("OCEAN", "海洋 (Ocean)", 'MOD_OCEAN'),
            ("PARTICLE_SYSTEM", "粒子系统 (Particle System)", 'MOD_PARTICLES'),
            ("SOFT_BODY", "柔体 (Soft Body)", 'MOD_SOFT'),
        ]
        for type_id, name, icon in items:
            op = layout.operator("object.add_custom_modifier", text=name, icon=icon)
            op.modifier_type = type_id


# =========================================================================
# 原生修改器参数排版（100% 紧凑对齐 Blender 官方 5.2 原生）
# =========================================================================

def draw_modifier_body(layout, ob, md):
    """根据修改器类型以 Blender 官方原生布局排版参数"""
    mtype = md.type
    layout.use_property_split = True
    layout.use_property_decorate = False

    if mtype == 'NORMAL_EDIT':
        draw_normal_edit_layout(layout, ob, md)
    elif mtype == 'BOOLEAN':
        draw_boolean_layout(layout, ob, md)
    elif mtype == 'MIRROR':
        draw_mirror_layout(layout, ob, md)
    elif mtype == 'ARRAY':
        draw_array_layout(layout, ob, md)
    elif mtype == 'BEVEL':
        draw_bevel_layout(layout, ob, md)
    elif mtype == 'SUBSURF':
        draw_subsurf_layout(layout, ob, md)
    elif mtype == 'SOLIDIFY':
        draw_solidify_layout(layout, ob, md)
    elif mtype == 'DECIMATE':
        draw_decimate_layout(layout, ob, md)
    elif mtype == 'WEIGHTED_NORMAL':
        draw_weighted_normal_layout(layout, ob, md)
    elif mtype == 'WELD':
        draw_weld_layout(layout, ob, md)
    elif mtype == 'REMESH':
        draw_remesh_layout(layout, ob, md)
    elif mtype == 'SIMPLE_DEFORM':
        draw_simple_deform_layout(layout, ob, md)
    elif mtype == 'EDGE_SPLIT':
        draw_edge_split_layout(layout, ob, md)
    elif mtype == 'TRIANGULATE':
        draw_triangulate_layout(layout, ob, md)
    elif mtype == 'WIREFRAME':
        draw_wireframe_layout(layout, ob, md)
    elif mtype == 'SHRINKWRAP':
        draw_shrinkwrap_layout(layout, ob, md)
    elif mtype == 'DISPLACE':
        draw_displace_layout(layout, ob, md)
    elif mtype == 'SMOOTH':
        draw_smooth_layout(layout, ob, md)
    elif mtype == 'SCREW':
        draw_screw_layout(layout, ob, md)
    elif mtype == 'CURVE':
        draw_curve_layout(layout, ob, md)
    elif mtype == 'LATTICE':
        draw_lattice_layout(layout, ob, md)
    elif mtype == 'CAST':
        draw_cast_layout(layout, ob, md)
    elif mtype == 'NODES':
        draw_nodes_layout(layout, ob, md)
    else:
        dp = modifier_layouts.DATA_PT_modifiers(bpy.context)
        func = getattr(dp, mtype, None)
        if func:
            try:
                func(layout, ob, md)
                return
            except Exception:
                pass
        draw_generic_layout(layout, ob, md)


# --- 几何节点修改器 (Geometry Nodes / 按角度平滑) ---
def draw_nodes_layout(layout, ob, md):
    """绘制几何节点修改器（如 按角度平滑 / Smooth by Angle 等），100% 对齐官方原生布局"""
    if not md.node_group:
        layout.template_ID(md, "node_group", new="node.new_geometry_node_group_assign")
        return

    ng = md.node_group
    layout.template_ID(md, "node_group", new="node.new_geometry_node_group_assign")
    layout.separator(factor=0.5)

    # 绘制节点组输入参数
    if hasattr(md, "properties") and hasattr(md.properties, "inputs"):
        inputs_obj = md.properties.inputs
        if hasattr(ng, "interface") and hasattr(ng.interface, "items_tree"):
            for item in ng.interface.items_tree:
                if getattr(item, "item_type", None) == 'SOCKET' and getattr(item, "in_out", None) == 'INPUT':
                    if getattr(item, "socket_type", None) == 'NodeSocketGeometry':
                        continue
                    ident = getattr(item, "identifier", None)
                    if not ident:
                        continue
                    inp = getattr(inputs_obj, ident, None)
                    if not inp:
                        continue

                    row = layout.row(align=True)
                    if hasattr(inp, "value"):
                        row.prop(inp, "value", text=item.name)
                    elif hasattr(inp, "default_value"):
                        row.prop(inp, "default_value", text=item.name)


# --- 法线编辑 (Normal Edit) ---
def draw_normal_edit_layout(layout, ob, md):
    layout.row().prop(md, "mode", expand=True)
    layout.prop(md, "target", text="目标")
    layout.prop(md, "use_direction_parallel", text="平行法向")

    header, panel = layout.panel("normal_edit_mix", default_closed=False)
    header.label(text="混合")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        panel.prop(md, "mix_mode", text="混合模式")
        panel.prop(md, "mix_factor", text="混合系数")

        row_v = panel.row(heading="顶点组", align=True)
        row_v.prop_search(md, "vertex_group", ob, "vertex_groups", text="")
        sub_v = row_v.row(align=True)
        sub_v.active = bool(md.vertex_group)
        sub_v.prop(md, "invert_vertex_group", text="", icon='ARROW_LEFTRIGHT')

        panel.prop(md, "mix_limit", text="最大角度")

    header, panel = layout.panel("normal_edit_offset", default_closed=True)
    header.label(text="偏移")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        panel.prop(md, "offset", index=0, text="偏移 X")
        panel.prop(md, "offset", index=1, text="Y")
        panel.prop(md, "offset", index=2, text="Z")


# --- 布尔 (Boolean) ---
def draw_boolean_layout(layout, ob, md):
    layout.row().prop(md, "operation", expand=True)
    layout.prop(md, "operand_type", text="运算对象类型")
    if md.operand_type == 'OBJECT':
        layout.prop(md, "object", text="物体")
    else:
        layout.prop(md, "collection", text="集合")

    row_s = layout.row(heading="解算器", align=True)
    row_s.prop(md, "solver", expand=True)

    header, panel = layout.panel("boolean_solver_options", default_closed=True)
    header.label(text="解算器选项")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        if hasattr(md, "double_threshold"):
            panel.prop(md, "double_threshold", text="重合阈值")
        if hasattr(md, "use_self"):
            panel.prop(md, "use_self", text="自相交")
        if hasattr(md, "use_hole_tolerant"):
            panel.prop(md, "use_hole_tolerant", text="容孔")


# --- 镜像 (Mirror) ---
def draw_mirror_layout(layout, ob, md):
    row = layout.row(heading="轴向", align=True)
    row.prop(md, "use_axis", index=0, text="X", toggle=True)
    row.prop(md, "use_axis", index=1, text="Y", toggle=True)
    row.prop(md, "use_axis", index=2, text="Z", toggle=True)

    row_b = layout.row(heading="切分", align=True)
    row_b.prop(md, "use_bisect_axis", index=0, text="X", toggle=True)
    row_b.prop(md, "use_bisect_axis", index=1, text="Y", toggle=True)
    row_b.prop(md, "use_bisect_axis", index=2, text="Z", toggle=True)

    row_f = layout.row(heading="翻转", align=True)
    row_f.prop(md, "use_bisect_flip_axis", index=0, text="X", toggle=True)
    row_f.prop(md, "use_bisect_flip_axis", index=1, text="Y", toggle=True)
    row_f.prop(md, "use_bisect_flip_axis", index=2, text="Z", toggle=True)

    layout.prop(md, "mirror_object", text="镜像物体")
    layout.prop(md, "use_clip", text="范围限制")

    row_m = layout.row(heading="合并", align=True)
    row_m.prop(md, "use_mirror_merge", text="")
    sub_m = row_m.row(align=True)
    sub_m.active = md.use_mirror_merge
    sub_m.prop(md, "merge_threshold", text="")

    layout.prop(md, "bisect_threshold", text="切分距离")

    # 数据子面板（可折叠）
    header, panel = layout.panel("mirror_data", default_closed=False)
    header.label(text="数据")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        col = panel.column()
        row_u = col.row(heading="镜像 U", align=True)
        row_u.prop(md, "use_mirror_u", text="")
        sub_u = row_u.row(align=True)
        sub_u.active = md.use_mirror_u
        sub_u.prop(md, "mirror_offset_u", text="")

        row_v = col.row(heading="V", align=True)
        row_v.prop(md, "use_mirror_v", text="")
        sub_v = row_v.row(align=True)
        sub_v.active = md.use_mirror_v
        sub_v.prop(md, "mirror_offset_v", text="")

        col.prop(md, "offset_u", text="偏移 U")

        col.prop(md, "offset_v", text="V")

        col.prop(md, "use_mirror_vertex_groups", text="顶点组")

        col.prop(md, "use_mirror_udim", text="翻转UDIM")


# --- 阵列 (Array) ---
def draw_array_layout(layout, ob, md):
    layout.prop(md, "fit_type", text="适配类型")
    if md.fit_type == 'FIXED_COUNT':
        layout.prop(md, "count", text="数量")
    elif md.fit_type == 'FIT_LENGTH':
        layout.prop(md, "fit_length", text="长度")
    elif md.fit_type == 'FIT_CURVE':
        layout.prop(md, "curve", text="曲线")

    # 相对偏移（原生可折叠子面板）
    header, panel = layout.panel("array_relative_offset", default_closed=not md.use_relative_offset)
    header.prop(md, "use_relative_offset", text="相对偏移")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        col = panel.column(align=True)
        col.active = md.use_relative_offset
        col.prop(md, "relative_offset_displace", index=0, text="系数 X")
        col.prop(md, "relative_offset_displace", index=1, text="Y")
        col.prop(md, "relative_offset_displace", index=2, text="Z")

    # 恒定偏移（原生可折叠子面板）
    header, panel = layout.panel("array_constant_offset", default_closed=not md.use_constant_offset)
    header.prop(md, "use_constant_offset", text="恒定偏移")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        col = panel.column(align=True)
        col.active = md.use_constant_offset
        col.prop(md, "constant_offset_displace", index=0, text="距离 X")
        col.prop(md, "constant_offset_displace", index=1, text="Y")
        col.prop(md, "constant_offset_displace", index=2, text="Z")

    # 物体偏移（原生可折叠子面板）
    header, panel = layout.panel("array_object_offset", default_closed=not md.use_object_offset)
    header.prop(md, "use_object_offset", text="物体偏移")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        col = panel.column()
        col.active = md.use_object_offset
        col.prop(md, "offset_object", text="目标物体")

    # 合并（原生可折叠子面板）
    header, panel = layout.panel("array_merge", default_closed=not md.use_merge_vertices)
    header.prop(md, "use_merge_vertices", text="合并")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        col = panel.column()
        col.active = md.use_merge_vertices
        col.prop(md, "merge_threshold", text="合并距离")
        if hasattr(md, "use_merge_vertices_cap"):
            col.prop(md, "use_merge_vertices_cap", text="首末相连")

    # UV（原生可折叠子面板）
    header, panel = layout.panel("array_uv", default_closed=True)
    header.label(text="UV")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        panel.prop(md, "offset_u", text="偏移 U")
        panel.prop(md, "offset_v", text="V")

    # 端盖样式（原生可折叠子面板）
    header, panel = layout.panel("array_caps", default_closed=True)
    header.label(text="端盖样式")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        panel.prop(md, "start_cap", text="起始端")
        panel.prop(md, "end_cap", text="末端")


# --- 倒角 (Bevel) ---
def draw_bevel_layout(layout, ob, md):
    layout.row().prop(md, "affect", expand=True)

    layout.prop(md, "offset_type", text="宽度类型")
    if md.offset_type == 'PERCENT':
        layout.prop(md, "width_pct", text="宽度百分比")
    else:
        layout.prop(md, "width", text="数量")

    layout.prop(md, "segments", text="分段")
    layout.prop(md, "limit_method", text="限定方式")
    if md.limit_method == 'ANGLE':
        layout.prop(md, "angle_limit", text="角度")
    elif md.limit_method == 'WEIGHT':
        if hasattr(md, "edge_weight"):
            layout.prop(md, "edge_weight", text="边权重")
        elif hasattr(md, "vertex_weight"):
            layout.prop(md, "vertex_weight", text="顶点权重")
    elif md.limit_method == 'VGROUP':
        row_v = layout.row(heading="顶点组", align=True)
        row_v.prop_search(md, "vertex_group", ob, "vertex_groups", text="")
        sub_v = row_v.row(align=True)
        sub_v.active = bool(md.vertex_group)
        sub_v.prop(md, "invert_vertex_group", text="", icon='ARROW_LEFTRIGHT')

    header, panel = layout.panel("bevel_profile", default_closed=False)
    header.label(text="轮廓")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        panel.prop(md, "profile_type", text="轮廓类型")
        if md.profile_type == 'SUPERELLIPSE':
            panel.prop(md, "profile", text="轮廓形状")
        panel.prop(md, "material", text="材质编号")

    header, panel = layout.panel("bevel_geometry", default_closed=True)
    header.label(text="几何数据")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        panel.prop(md, "use_clamp_overlap", text="钳制重叠")
        panel.prop(md, "loop_slide", text="环切线滑移")
        panel.prop(md, "mark_seam", text="标记为缝合边")
        panel.prop(md, "mark_sharp", text="标记锐边")
        panel.prop(md, "miter_outer", text="外斜接")
        panel.prop(md, "miter_inner", text="内斜接")

    header, panel = layout.panel("bevel_shading", default_closed=True)
    header.label(text="着色方式")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        panel.prop(md, "harden_normals", text="硬化法向")
        panel.prop(md, "face_strength_mode", text="面强度")


# --- 细分曲面 (Subsurf) ---
def draw_subsurf_layout(layout, ob, md):
    layout.row().prop(md, "subdivision_type", expand=True)
    layout.prop(md, "levels", text="视图级别")
    layout.prop(md, "render_levels", text="渲染级别")
    layout.prop(md, "quality", text="质量")

    header, panel = layout.panel("subsurf_advanced", default_closed=True)
    header.label(text="高级")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        panel.prop(md, "uv_smooth", text="UV 平滑")
        panel.prop(md, "boundary_smooth", text="边界平滑")
        panel.prop(md, "use_creases", text="使用折痕")
        panel.prop(md, "use_custom_normals", text="使用自定义法线")


# --- 实体化 (Solidify) ---
def draw_solidify_layout(layout, ob, md):
    layout.row().prop(md, "solidify_mode", expand=True)
    layout.prop(md, "thickness", text="厚度")
    layout.prop(md, "offset", text="偏移量")
    layout.prop(md, "use_even_offset", text="均一厚度")
    layout.prop(md, "use_rim", text="填充边缘")
    layout.prop(md, "use_rim_only", text="仅边缘")
    layout.prop(md, "use_flip_normals", text="翻转法向")

    header, panel = layout.panel("solidify_materials", default_closed=True)
    header.label(text="材质与顶点组")
    if panel:
        panel.use_property_split = True
        panel.use_property_decorate = False
        panel.prop(md, "material_offset", text="材质偏移")
        panel.prop(md, "material_offset_rim", text="边缘材质")
        row_v = panel.row(heading="顶点组", align=True)
        row_v.prop_search(md, "vertex_group", ob, "vertex_groups", text="")
        sub_v = row_v.row(align=True)
        sub_v.active = bool(md.vertex_group)
        sub_v.prop(md, "invert_vertex_group", text="", icon='ARROW_LEFTRIGHT')


# --- 减面/精简 (Decimate) ---
def draw_decimate_layout(layout, ob, md):
    layout.row().prop(md, "decimate_type", expand=True)
    if md.decimate_type == 'COLLAPSE':
        layout.prop(md, "ratio", text="比率")
        layout.prop(md, "use_symmetry", text="对称")
        layout.prop(md, "use_collapse_triangulate", text="三角化")
    elif md.decimate_type == 'UNSUBDIV':
        layout.prop(md, "iterations", text="迭代次数")
    elif md.decimate_type == 'DISSOLVE':
        layout.prop(md, "angle_limit", text="角度限制")
        layout.prop(md, "delimit", text="限定")

    row_v = layout.row(heading="顶点组", align=True)
    row_v.prop_search(md, "vertex_group", ob, "vertex_groups", text="")
    sub_v = row_v.row(align=True)
    sub_v.active = bool(md.vertex_group)
    sub_v.prop(md, "invert_vertex_group", text="", icon='ARROW_LEFTRIGHT')


# --- 加权法向 (Weighted Normal) ---
def draw_weighted_normal_layout(layout, ob, md):
    layout.prop(md, "weight", text="权重")
    layout.prop(md, "mode", text="模式")
    layout.prop(md, "thresh", text="阈值")
    layout.prop(md, "keep_sharp", text="保持锐边")
    layout.prop(md, "use_face_influence", text="面影响")

    row_v = layout.row(heading="顶点组", align=True)
    row_v.prop_search(md, "vertex_group", ob, "vertex_groups", text="")
    sub_v = row_v.row(align=True)
    sub_v.active = bool(md.vertex_group)
    sub_v.prop(md, "invert_vertex_group", text="", icon='ARROW_LEFTRIGHT')


# --- 焊接 (Weld) ---
def draw_weld_layout(layout, ob, md):
    layout.prop(md, "mode", text="模式")
    layout.prop(md, "merge_threshold", text="距离阈值")
    row_v = layout.row(heading="顶点组", align=True)
    row_v.prop_search(md, "vertex_group", ob, "vertex_groups", text="")
    sub_v = row_v.row(align=True)
    sub_v.active = bool(md.vertex_group)
    sub_v.prop(md, "invert_vertex_group", text="", icon='ARROW_LEFTRIGHT')


# --- 重构网格 (Remesh) ---
def draw_remesh_layout(layout, ob, md):
    layout.row().prop(md, "mode", expand=True)
    if md.mode == 'VOXEL':
        layout.prop(md, "voxel_size", text="体素大小")
        layout.prop(md, "adaptivity", text="自适应")
        layout.prop(md, "use_smooth_shade", text="平滑着色")
    elif md.mode == 'SMOOTH':
        layout.prop(md, "octree_depth", text="八叉树深度")
        layout.prop(md, "scale", text="缩放")
        layout.prop(md, "use_smooth_shade", text="平滑着色")
    elif md.mode == 'BLOCKS':
        layout.prop(md, "octree_depth", text="八叉树深度")
        layout.prop(md, "scale", text="缩放")


# --- 简易形变 (Simple Deform) ---
def draw_simple_deform_layout(layout, ob, md):
    layout.row().prop(md, "deform_method", expand=True)
    if md.deform_method in {'TWIST', 'BEND'}:
        layout.prop(md, "angle", text="角度")
    else:
        layout.prop(md, "factor", text="系数")
    layout.prop(md, "origin", text="原点")
    row_a = layout.row(heading="形变轴", align=True)
    row_a.prop(md, "deform_axis", expand=True)
    layout.prop(md, "limits", text="限制范围")
    row_v = layout.row(heading="顶点组", align=True)
    row_v.prop_search(md, "vertex_group", ob, "vertex_groups", text="")
    sub_v = row_v.row(align=True)
    sub_v.active = bool(md.vertex_group)
    sub_v.prop(md, "invert_vertex_group", text="", icon='ARROW_LEFTRIGHT')


# --- 边缘分拆 (Edge Split) ---
def draw_edge_split_layout(layout, ob, md):
    layout.prop(md, "use_edge_angle", text="从边角度")
    if md.use_edge_angle:
        layout.prop(md, "split_angle", text="分拆角度")
    layout.prop(md, "use_edge_sharp", text="从锐边")


# --- 三角化 (Triangulate) ---
def draw_triangulate_layout(layout, ob, md):
    layout.prop(md, "quad_method", text="四边形方法")
    layout.prop(md, "ngon_method", text="多边形方法")
    layout.prop(md, "min_vertices", text="最小顶点数")
    layout.prop(md, "keep_custom_normals", text="保留自定义法线")


# --- 线框 (Wireframe) ---
def draw_wireframe_layout(layout, ob, md):
    layout.prop(md, "thickness", text="厚度")
    layout.prop(md, "offset", text="偏移")
    layout.prop(md, "use_boundary", text="边界")
    layout.prop(md, "use_replace", text="替换原网格")
    layout.prop(md, "use_even_offset", text="均一厚度")
    layout.prop(md, "material_offset", text="材质编号")


# --- 收缩包裹 (Shrinkwrap) ---
def draw_shrinkwrap_layout(layout, ob, md):
    layout.prop(md, "wrap_method", text="包裹方法")
    layout.prop(md, "wrap_mode", text="包裹模式")
    layout.prop(md, "target", text="目标")
    layout.prop(md, "offset", text="偏移量")
    row_v = layout.row(heading="顶点组", align=True)
    row_v.prop_search(md, "vertex_group", ob, "vertex_groups", text="")
    sub_v = row_v.row(align=True)
    sub_v.active = bool(md.vertex_group)
    sub_v.prop(md, "invert_vertex_group", text="", icon='ARROW_LEFTRIGHT')


# --- 置换 (Displace) ---
def draw_displace_layout(layout, ob, md):
    layout.prop(md, "texture", text="纹理")
    layout.prop(md, "direction", text="方向")
    layout.prop(md, "texture_coords", text="纹理坐标")
    layout.prop(md, "strength", text="强度")
    layout.prop(md, "mid_level", text="中间电平")
    row_v = layout.row(heading="顶点组", align=True)
    row_v.prop_search(md, "vertex_group", ob, "vertex_groups", text="")
    sub_v = row_v.row(align=True)
    sub_v.active = bool(md.vertex_group)
    sub_v.prop(md, "invert_vertex_group", text="", icon='ARROW_LEFTRIGHT')


# --- 平滑 (Smooth) ---
def draw_smooth_layout(layout, ob, md):
    row = layout.row(heading="轴向", align=True)
    row.prop(md, "use_x", text="X", toggle=True)
    row.prop(md, "use_y", text="Y", toggle=True)
    row.prop(md, "use_z", text="Z", toggle=True)
    layout.prop(md, "factor", text="系数")
    layout.prop(md, "iterations", text="重复")
    row_v = layout.row(heading="顶点组", align=True)
    row_v.prop_search(md, "vertex_group", ob, "vertex_groups", text="")
    sub_v = row_v.row(align=True)
    sub_v.active = bool(md.vertex_group)
    sub_v.prop(md, "invert_vertex_group", text="", icon='ARROW_LEFTRIGHT')


# --- 螺旋 (Screw) ---
def draw_screw_layout(layout, ob, md):
    row_a = layout.row(heading="轴向", align=True)
    row_a.prop(md, "axis", expand=True)
    layout.prop(md, "screw_offset", text="螺旋")
    layout.prop(md, "iterations", text="圈数")
    layout.prop(md, "angle", text="角度")
    layout.prop(md, "steps", text="轴向步数")
    layout.prop(md, "render_steps", text="渲染步数")
    layout.prop(md, "use_smooth_shade", text="平滑着色")
    layout.prop(md, "use_merge_vertices", text="合并顶点")


# --- 曲线 (Curve) ---
def draw_curve_layout(layout, ob, md):
    layout.prop(md, "object", text="曲线物体")
    row_a = layout.row(heading="形变轴", align=True)
    row_a.prop(md, "deform_axis", expand=True)
    row_v = layout.row(heading="顶点组", align=True)
    row_v.prop_search(md, "vertex_group", ob, "vertex_groups", text="")
    sub_v = row_v.row(align=True)
    sub_v.active = bool(md.vertex_group)
    sub_v.prop(md, "invert_vertex_group", text="", icon='ARROW_LEFTRIGHT')


# --- 晶格 (Lattice) ---
def draw_lattice_layout(layout, ob, md):
    layout.prop(md, "object", text="晶格物体")
    layout.prop(md, "strength", text="强度")
    row_v = layout.row(heading="顶点组", align=True)
    row_v.prop_search(md, "vertex_group", ob, "vertex_groups", text="")
    sub_v = row_v.row(align=True)
    sub_v.active = bool(md.vertex_group)
    sub_v.prop(md, "invert_vertex_group", text="", icon='ARROW_LEFTRIGHT')


# --- 铸型 (Cast) ---
def draw_cast_layout(layout, ob, md):
    layout.row().prop(md, "cast_type", expand=True)
    layout.prop(md, "factor", text="系数")
    layout.prop(md, "radius", text="半径")
    layout.prop(md, "size", text="大小")
    layout.prop(md, "object", text="控制物体")


# --- 通用排版回退 ---
def draw_generic_layout(layout, ob, md):
    ignored = {
        "name", "type", "show_expanded", "show_viewport", "show_render",
        "show_in_editmode", "show_on_cage", "is_active", "use_pin_to_last",
        "use_apply_on_spline", "is_override_data", "execution_time"
    }
    for prop in md.bl_rna.properties:
        if not prop.is_readonly and prop.identifier not in ignored:
            layout.prop(md, prop.identifier)


# =========================================================================
# 修改器主面板渲染 (去重聚合显示所有选中物体的修改器)
# =========================================================================

def draw_modifiers_section(layout, context):
    """绘制原生风格的修改器堆栈卡片列表（显示所有选中物体的修改器）"""
    selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']
    active_obj = context.active_object if context.active_object and context.active_object.type == 'MESH' else None

    # 如果没有活动网格物体，但在多选中存在网格物体，选取第一个网格物体
    if not active_obj and selected_objs:
        active_obj = selected_objs[0]

    # 顶部标题与添加按钮
    header_row = layout.row(align=True)
    header_row.menu("OBJECT_MT_custom_modifier_add", text="新建修改器", icon='ADD')

    modifier_box = layout.box()
    sync_state = ModifierAutoSync.get_state(context.scene)

    def draw_status():
        if not sync_state:
            return
        message = sync_state.status_message
        if message:
            draw_status_tooltip(layout, message)

    if not selected_objs and not active_obj:
        modifier_box.label(text="请选择网格物体", icon='INFO')
        draw_status()
        return

    if not selected_objs and active_obj:
        selected_objs = [active_obj]

    selected_count = len(selected_objs)

    # 1. 聚合所有选中物体的修改器（去重有序收集）
    unique_mod_dict = {}  # key: mod_name -> { 'type': ..., 'instance': ..., 'owner': ..., 'count': ... }
    
    # 优先保证 active_obj 的修改器排在前面
    scan_order = [active_obj] + [o for o in selected_objs if o != active_obj]
    for obj in scan_order:
        if not obj:
            continue
        for mod in obj.modifiers:
            if mod.name not in unique_mod_dict:
                # 统计拥有该修改器的物体数量
                has_count = sum(1 for o in selected_objs if mod.name in o.modifiers)
                unique_mod_dict[mod.name] = {
                    'name': mod.name,
                    'type': mod.type,
                    'instance': mod,
                    'owner': obj,
                    'count': has_count
                }

    if not unique_mod_dict:
        modifier_box.label(text="当前选中的物体无修改器", icon='INFO')
        draw_status()
        return

    main_col = modifier_box.column(align=True)

    # 2. 遍历聚合的修改器卡片进行绘制
    for mod_info in unique_mod_dict.values():
        mod_name = mod_info['name']
        mod_type = mod_info['type']
        mod = mod_info['instance']
        owner_obj = mod_info['owner']
        has_count = mod_info['count']

        # 使用原生 panel 容器，自动具备官方折叠三角图标旋转及拖拽连续批量折叠功能
        header, panel = main_col.panel(f"umm_mod_{mod_name}", default_closed=not mod.show_expanded)

        # 1. 点击修改器图标：在场景中选择所有拥有此修改器的物体
        sel_op = header.operator(
            "object.select_by_modifier",
            text="",
            icon=get_modifier_icon(mod_type),
            emboss=False
        )
        sel_op.modifier_name = mod.name
        sel_op.modifier_type = mod_type

        # 2. 修改器名称（可直接修改）
        header.prop(mod, "name", text="")

        # 3. 右侧控制图标组（完全对齐官方原生蓝底高亮与标准图标）
        right_group = header.row(align=True)
        right_group.alignment = 'RIGHT'

        if hasattr(mod, "show_on_cage"):
            right_group.prop(mod, "show_on_cage", text="")

        if hasattr(mod, "show_in_editmode"):
            right_group.prop(mod, "show_in_editmode", text="")

        right_group.prop(mod, "show_viewport", text="")
        right_group.prop(mod, "show_render", text="")

        # 下拉/应用操作
        apply_op = right_group.operator("object.apply_modifier_to_all", text="", icon='CHECKMARK', emboss=False)
        apply_op.modifier_name = mod.name

        # 删除按钮
        remove_op = right_group.operator("object.remove_modifier_from_selected", text="", icon='X', emboss=False)
        remove_op.modifier_name = mod.name

        # 卡片主体 (Body) - 仅在展开时显示原生参数
        if panel:
            panel.use_property_split = True
            panel.use_property_decorate = False
            draw_modifier_body(panel, owner_obj, mod)

    draw_status()


# =========================================================================
# 注册与注销
# =========================================================================

classes = (
    DATA_PT_UVMMModifierSync,
    MODIFIER_OT_AddSyncedNative,
    UMM_OT_StatusTooltip,
    MODIFIER_OT_SelectByModifier,
    MODIFIER_OT_ToggleExpanded,
    MODIFIER_OT_AddCustom,
    MODIFIER_OT_ApplyToAll,
    MODIFIER_OT_RemoveFromSelected,
    MODIFIER_OT_SyncToSelected,
    OBJECT_MT_CustomModifierAdd,
    OBJECT_MT_CustomModifierAdd_Modify,
    OBJECT_MT_CustomModifierAdd_Generate,
    OBJECT_MT_CustomModifierAdd_Deform,
    OBJECT_MT_CustomModifierAdd_Physics,
)

def register():
    bpy.utils.register_class(ModifierSyncSnapshot)
    bpy.utils.register_class(ModifierSyncChange)
    bpy.utils.register_class(ModifierSyncHistory)
    bpy.utils.register_class(ModifierSyncRuntimeState)

    bpy.types.WindowManager.um_modifier_sync_histories = CollectionProperty(
        type=ModifierSyncHistory, options={'SKIP_SAVE'}
    )

    bpy.types.Scene.um_modifier_sync_state = PointerProperty(
        type=ModifierSyncRuntimeState,
        options={'SKIP_SAVE'}
    )
    bpy.types.Scene.um_auto_sync_modifiers = BoolProperty(
        name="自动同步修改器参数",
        description="在原生修改器面板调整参数时，同步到其他选中物体上同名、同类型的修改器",
        default=False,
        update=on_modifier_sync_toggle,
    )

    for cls in classes:
        bpy.utils.register_class(cls)


    if depsgraph_update_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(depsgraph_update_handler)
    if modifier_sync_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(modifier_sync_load_post)
    for handler_list, handler in (
        (bpy.app.handlers.undo_pre, modifier_sync_undo_pre),
        (bpy.app.handlers.redo_pre, modifier_sync_undo_pre),
        (bpy.app.handlers.undo_post, modifier_sync_undo_post),
        (bpy.app.handlers.redo_post, modifier_sync_redo_post),
    ):
        if handler not in handler_list:
            handler_list.append(handler)

def unregister():
    if bpy.app.timers.is_registered(_finish_modifier_operation):
        bpy.app.timers.unregister(_finish_modifier_operation)
    if depsgraph_update_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(depsgraph_update_handler)
    if modifier_sync_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(modifier_sync_load_post)
    for handler_list, handler in (
        (bpy.app.handlers.undo_pre, modifier_sync_undo_pre),
        (bpy.app.handlers.redo_pre, modifier_sync_undo_pre),
        (bpy.app.handlers.undo_post, modifier_sync_undo_post),
        (bpy.app.handlers.redo_post, modifier_sync_redo_post),
    ):
        if handler in handler_list:
            handler_list.remove(handler)

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    if hasattr(bpy.types.Scene, "um_auto_sync_modifiers"):
        del bpy.types.Scene.um_auto_sync_modifiers
    if hasattr(bpy.types.Scene, "um_modifier_sync_state"):
        del bpy.types.Scene.um_modifier_sync_state
    if hasattr(bpy.types.WindowManager, "um_modifier_sync_histories"):
        del bpy.types.WindowManager.um_modifier_sync_histories

    bpy.utils.unregister_class(ModifierSyncRuntimeState)
    bpy.utils.unregister_class(ModifierSyncHistory)
    bpy.utils.unregister_class(ModifierSyncChange)
    bpy.utils.unregister_class(ModifierSyncSnapshot)
