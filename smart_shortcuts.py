"""Mirror user-assigned modeling shortcuts into Blender's Edit Mesh keymap."""

import bpy


_MIRRORS = {
    "mesh.uvmm_smart_vertex": "mesh.uvmm_smart_vertex_shortcut",
    "mesh.uvmm_smart_edge": "mesh.uvmm_smart_edge_shortcut",
}


def _key_signature(item):
    return (
        item.type, item.value, item.any, item.shift, item.ctrl, item.alt,
        item.oskey, item.key_modifier, item.direction,
    )


def _sync_user_shortcuts():
    window_manager = bpy.context.window_manager
    if window_manager is None:
        return 1.0
    user_config = window_manager.keyconfigs.user
    addon_config = window_manager.keyconfigs.addon
    if user_config is None or addon_config is None:
        return 1.0

    source_items = {
        name: {source_id: [] for source_id in _MIRRORS}
        for name in {'Mesh', '3D View'}
    }
    for keymap in user_config.keymaps:
        if keymap.name not in source_items:
            continue
        for item in keymap.keymap_items:
            if item.active and item.idname in _MIRRORS:
                source_items[keymap.name][item.idname].append(item)

    for name, space_type, region_type in (
        ('Mesh', 'EMPTY', 'WINDOW'),
        ('3D View', 'VIEW_3D', 'WINDOW'),
    ):
        sources = source_items[name]
        target_keymap = addon_config.keymaps.get(name)
        if target_keymap is None and any(sources.values()):
            source_map = next(
                (keymap for keymap in user_config.keymaps if keymap.name == name),
                None,
            )
            target_keymap = addon_config.keymaps.new(
                name=name,
                space_type=source_map.space_type if source_map else space_type,
                region_type=source_map.region_type if source_map else region_type,
            )
        if target_keymap is None:
            continue

        for source_id, target_id in _MIRRORS.items():
            desired_sources = sources[source_id]
            desired = sorted(_key_signature(item) for item in desired_sources)
            existing = sorted(
                _key_signature(item)
                for item in target_keymap.keymap_items
                if item.idname == target_id
            )
            if desired == existing:
                continue
            for item in list(target_keymap.keymap_items):
                if item.idname == target_id:
                    target_keymap.keymap_items.remove(item)
            for source in desired_sources:
                mirror = target_keymap.keymap_items.new(
                    target_id,
                    source.type,
                    source.value,
                    any=source.any,
                    shift=source.shift,
                    ctrl=source.ctrl,
                    alt=source.alt,
                    oskey=source.oskey,
                    key_modifier=source.key_modifier,
                    head=True,
                )
                mirror.direction = source.direction
    return 1.0


def register():
    if not bpy.app.timers.is_registered(_sync_user_shortcuts):
        bpy.app.timers.register(_sync_user_shortcuts, first_interval=0.5, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_sync_user_shortcuts):
        bpy.app.timers.unregister(_sync_user_shortcuts)
    window_manager = bpy.context.window_manager
    if window_manager is None or window_manager.keyconfigs.addon is None:
        return
    for name in {'Mesh', '3D View'}:
        keymap = window_manager.keyconfigs.addon.keymaps.get(name)
        if keymap is None:
            continue
        for item in list(keymap.keymap_items):
            if item.idname in _MIRRORS.values():
                keymap.keymap_items.remove(item)
