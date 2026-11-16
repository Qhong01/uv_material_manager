"""Model Checker add-on for Blender 5.2+."""

bl_info = {
    "name": "Model Checker",
    "author": "OpenAI",
    "version": (0, 1, 0),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > 模型检查",
    "description": "检查多边面、UV 拉伸和孤立顶点",
    "category": "Mesh",
}

import importlib
import json
import time

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, PointerProperty, StringProperty
from bpy.app.handlers import persistent

from . import diagnostic, native_preview, overlay, operators, panel

# Blender can keep a child module imported from an older add-on version after
# the package itself is reloaded. Refresh operators only when its current API
# is incomplete, so class registration below cannot fail on stale symbols.
_REQUIRED_OPERATOR_CLASSES = (
    "MC_OT_check_ngons",
    "MC_OT_check_isolated_vertices",
    "MC_OT_check_collapsed_uv",
    "MC_OT_check_model_boundaries",
    "MC_OT_toggle_uv_overlay",
    "MC_OT_uv_checks",
    "MC_OT_settings_popup",
)
if any(not hasattr(operators, name) for name in _REQUIRED_OPERATOR_CLASSES):
    operators = importlib.reload(operators)

ADDON_ID = __package__.split('.')[0]


def _redraw_viewports():
    screen = bpy.context.screen
    if screen is not None:
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
    return None


def _save_user_preferences():
    """Persist the add-on's shared checker settings after the user finishes editing."""
    # Never touch the real user preferences from automated/测试 instances:
    # ``--background`` / ``--factory-startup`` runs must not overwrite the
    # profile of the running Blender the user is working in.
    if bpy.app.background or getattr(bpy.app, "factory_startup", False):
        return None
    try:
        bpy.ops.wm.save_userpref()
    except RuntimeError as exc:
        print(f"UV Material Manager: 无法自动保存首选项：{exc}")
    return None


def _schedule_preferences_save(_self=None, _context=None):
    if not bpy.app.timers.is_registered(_save_user_preferences):
        bpy.app.timers.register(_save_user_preferences, first_interval=0.5)


def _sync_scene_overlay(scene, prefs, force_refresh=True):
    if not hasattr(scene, "model_checker_state"):
        return
    scene_state = scene.model_checker_state
    for name in (
        "uv_show_stretch", "uv_show_boundaries", "uv_show_checker",
        "uv_show_orientation", "uv_boundary_color", "uv_boundary_width", "uv_stretch_base_color",
        "uv_stretch_max_color", "uv_stretch_strength", "uv_stretch_threshold",
        "uv_stretch_opacity",
        "uv_checker_opacity", "uv_checker_density", "uv_checker_color_a",
        "uv_checker_color_b", "uv_checker_pattern", "uv_checker_custom_image",
        "uv_orientation_opacity",
        "uv_orientation_front_color", "uv_orientation_back_color",
    ):
        setattr(scene_state, name, getattr(prefs, name))
    scene_state.uv_display_mode = prefs.uv_display_mode
    scene_state.overlay_active = any((
        scene_state.uv_show_stretch,
        scene_state.uv_show_boundaries,
        scene_state.uv_show_checker,
        scene_state.uv_show_orientation,
    ))
    if force_refresh:
        scene_state.overlay_cache_dirty = True
        diagnostic.mark_full_refresh(scene_state)


def _sync_all_scenes(prefs, force_refresh=True):
    for scene in bpy.data.scenes:
        _sync_scene_overlay(scene, prefs, force_refresh=force_refresh)


def _enforce_uv_display_exclusivity(prefs):
    if (prefs.uv_mode_normalizing or not prefs.uv_show_checker
            or prefs.uv_display_mode == 'MATERIAL'):
        return
    prefs.uv_mode_normalizing = True
    try:
        prefs.uv_show_stretch = False
        prefs.uv_show_orientation = False
    finally:
        prefs.uv_mode_normalizing = False


def _update_uv_feature(self, context, feature):
    if self.uv_mode_normalizing:
        return
    if self.uv_display_mode == 'CLASSIC' and getattr(self, feature):
        self.uv_mode_normalizing = True
        try:
            if feature == 'uv_show_checker':
                self.uv_show_stretch = self.uv_show_orientation = False
            else:
                self.uv_show_checker = False
        finally:
            self.uv_mode_normalizing = False
    _update_scene_overlay(self, context)


def _update_uv_stretch(self, context):
    _update_uv_feature(self, context, 'uv_show_stretch')


def _update_uv_orientation(self, context):
    _update_uv_feature(self, context, 'uv_show_orientation')


def _update_uv_checker(self, context):
    _update_uv_feature(self, context, 'uv_show_checker')


def _update_uv_display_mode(self, context):
    if self.uv_mode_normalizing:
        return
    if self.uv_display_mode == 'CLASSIC' and (self.uv_show_stretch or self.uv_show_orientation):
        self.uv_mode_normalizing = True
        try:
            self.uv_show_checker = False
        finally:
            self.uv_mode_normalizing = False
    _update_scene_overlay(self, context)


def _update_scene_overlay(self, context):
    if self.uv_mode_normalizing:
        return
    if self.uv_display_mode == 'MATERIAL' and not self.uv_material_colors_initialized:
        self.uv_mode_normalizing = True
        try:
            self.uv_material_base_color = (*self.uv_stretch_base_color, 0.0)
            self.uv_material_max_color = (*self.uv_stretch_max_color, self.uv_stretch_opacity)
            self.uv_material_front_color = (*self.uv_orientation_front_color, 0.0)
            self.uv_material_back_color = (*self.uv_orientation_back_color, self.uv_orientation_opacity)
            self.uv_material_colors_initialized = True
        finally:
            self.uv_mode_normalizing = False
    _enforce_uv_display_exclusivity(self)
    style_only_material = (
        self.uv_display_mode == 'MATERIAL'
        and all(
            not hasattr(scene, "model_checker_state")
            or (
                scene.model_checker_state.uv_display_mode == self.uv_display_mode
                and scene.model_checker_state.uv_show_stretch == self.uv_show_stretch
                and scene.model_checker_state.uv_show_checker == self.uv_show_checker
                and scene.model_checker_state.uv_show_orientation == self.uv_show_orientation
                and scene.model_checker_state.uv_checker_pattern == self.uv_checker_pattern
                and scene.model_checker_state.uv_checker_custom_image == self.uv_checker_custom_image
            )
            for scene in bpy.data.scenes
        )
    )
    _sync_all_scenes(self, force_refresh=not style_only_material)
    if style_only_material:
        native_preview.update_material_uniforms(self)
    show_diagnostics = any((
        self.uv_show_stretch,
        self.uv_show_checker,
        self.uv_show_orientation,
    ))
    color_type = 'VERTEX' if self.uv_show_stretch or self.uv_show_orientation else 'TEXTURE'
    diagnostic.set_viewport_mode(context, show_diagnostics, color_type,
                                 'MATERIAL' if self.uv_display_mode == 'MATERIAL' else 'SOLID')
    if not bpy.app.timers.is_registered(_redraw_viewports):
        bpy.app.timers.register(_redraw_viewports, first_interval=0.1)
    diagnostic.start_refresh_timer()
    _schedule_preferences_save()


_CHECKER_DEFAULT_DENSITY = 64


def _checker_density_key(prefs):
    """Identify the image the density slider currently belongs to."""
    pattern = getattr(prefs, 'uv_checker_pattern', 'CLASSIC')
    if pattern == 'CUSTOM':
        path = getattr(prefs, 'uv_checker_custom_image', '')
        return 'CUSTOM:' + bpy.path.abspath(path) if path else 'CUSTOM:'
    return pattern


def _read_checker_densities(prefs):
    raw = getattr(prefs, 'uv_checker_density_map', '')
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result = {}
    for key, value in data.items():
        try:
            result[str(key)] = max(1, min(256, int(value)))
        except (TypeError, ValueError):
            continue
    return result


def _write_checker_densities(prefs, data):
    prefs.uv_checker_density_map = json.dumps(
        data, ensure_ascii=False, sort_keys=True)


def _remember_checker_density(prefs):
    if getattr(prefs, 'uv_checker_density_switching', False):
        return
    key = getattr(prefs, 'uv_checker_density_key', '') or _checker_density_key(prefs)
    data = _read_checker_densities(prefs)
    data[key] = int(prefs.uv_checker_density)
    _write_checker_densities(prefs, data)
    prefs.uv_checker_density_key = key


def _init_checker_density_key(prefs):
    """Bind the legacy global density to the currently selected image once."""
    if prefs.uv_checker_density_key:
        return
    key = _checker_density_key(prefs)
    prefs.uv_checker_density_key = key
    data = _read_checker_densities(prefs)
    data.setdefault(key, int(prefs.uv_checker_density))
    _write_checker_densities(prefs, data)


def _update_checker_pattern(self, context):
    """Per-image density: store the old image's value, restore the new one."""
    if getattr(self, 'uv_checker_density_switching', False):
        return
    self.uv_checker_density_switching = True
    try:
        data = _read_checker_densities(self)
        old_key = self.uv_checker_density_key
        if old_key:
            data[old_key] = int(self.uv_checker_density)
        new_key = _checker_density_key(self)
        density = data.get(new_key, _CHECKER_DEFAULT_DENSITY)
        self.uv_checker_density = density
        self.uv_checker_density_key = new_key
        _write_checker_densities(self, data)
    finally:
        self.uv_checker_density_switching = False
    _update_checker_settings(self, context)


def _update_checker_settings(self, context):
    if getattr(self, 'uv_checker_density_switching', False):
        return
    _remember_checker_density(self)
    deadline = time.monotonic() + 0.4
    states = [
        scene.model_checker_state for scene in bpy.data.scenes
        if hasattr(scene, "model_checker_state")
    ]
    style_only_material = self.uv_display_mode == 'MATERIAL' and all(
        state.uv_display_mode == self.uv_display_mode
        and state.uv_show_checker == self.uv_show_checker
        and state.uv_checker_pattern == self.uv_checker_pattern
        and state.uv_checker_custom_image == self.uv_checker_custom_image
        for state in states
    )
    for state in states:
        for name in (
            "uv_checker_opacity", "uv_checker_density",
            "uv_checker_color_a", "uv_checker_color_b",
            "uv_checker_pattern", "uv_checker_custom_image",
        ):
            setattr(state, name, getattr(self, name))
        state.uv_display_mode = self.uv_display_mode
        if not style_only_material:
            diagnostic.mark_full_refresh(state)
            if self.uv_show_checker:
                state.uv_display_defer_until = max(state.uv_display_defer_until, deadline)
    if style_only_material:
        native_preview.update_material_uniforms(self)
    else:
        diagnostic.start_refresh_timer()
    _schedule_preferences_save()


def _update_boundary_width(self, context):
    for scene in bpy.data.scenes:
        if hasattr(scene, "model_checker_state"):
            scene.model_checker_state.uv_boundary_width = self.uv_boundary_width
    if not bpy.app.timers.is_registered(_redraw_viewports):
        bpy.app.timers.register(_redraw_viewports, first_interval=0.1)
    _schedule_preferences_save()


@persistent
def _restore_overlay_settings(_dummy):
    addon = bpy.context.preferences.addons.get(ADDON_ID)
    if addon is not None:
        prefs = addon.preferences
        _init_checker_density_key(prefs)
        _enforce_uv_display_exclusivity(prefs)
        _sync_all_scenes(prefs)
        diagnostic.start_refresh_timer()


@persistent
def _disable_checker_before_lifecycle_change(_dummy):
    """Turn off the temporary checker before save, render, or file replacement."""
    addon = bpy.context.preferences.addons.get(ADDON_ID)
    native_preview.suspend_for_save()
    for scene in bpy.data.scenes:
        if hasattr(scene, 'model_checker_state'):
            scene.model_checker_state.uv_native_materials.clear()
    if addon is not None and addon.preferences.uv_display_mode == 'MATERIAL':
        for scene in bpy.data.scenes:
            if hasattr(scene, 'model_checker_state'):
                state = scene.model_checker_state
                state.native_resume = True
                state.uv_native_materials.clear()
                diagnostic.sync_viewport_mode(scene, True, state.preview_shading_color_type,
                                              state.preview_shading_type)
        return
    checker_was_enabled = addon is not None and addon.preferences.uv_show_checker
    if checker_was_enabled:
        addon.preferences.uv_show_checker = False
    diagnostic.remove_checker_previews()

    if not checker_was_enabled:
        return
    for scene in bpy.data.scenes:
        if not hasattr(scene, "model_checker_state"):
            continue
        state = scene.model_checker_state
        state.uv_show_checker = False
        diagnostic.mark_full_refresh(state)
        state.overlay_cache_dirty = True
        remaining = any((state.uv_show_stretch, state.uv_show_orientation))
        diagnostic.sync_viewport_mode(scene, remaining, 'VERTEX')


@persistent
def _resume_native_preview(_dummy):
    addon = bpy.context.preferences.addons.get(ADDON_ID)
    if addon is None or addon.preferences.uv_display_mode != 'MATERIAL':
        return
    for scene in bpy.data.scenes:
        if not hasattr(scene, 'model_checker_state'):
            continue
        state = scene.model_checker_state
        if state.native_resume:
            state.native_resume = False
            enabled = any((addon.preferences.uv_show_stretch,
                           addon.preferences.uv_show_orientation,
                           addon.preferences.uv_show_checker))
            diagnostic.sync_viewport_mode(scene, enabled, 'MATERIAL', 'MATERIAL')
            diagnostic.mark_full_refresh(state)
    diagnostic.start_refresh_timer()


@persistent
def _suspend_native_before_undo(_dummy):
    """Release evaluated preview links before Blender replaces its Main IDs."""
    states = [scene.model_checker_state for scene in bpy.data.scenes
              if hasattr(scene, 'model_checker_state')]
    addon = bpy.context.preferences.addons.get(ADDON_ID)
    active = addon is not None and addon.preferences.uv_display_mode == 'MATERIAL' and any((
        addon.preferences.uv_show_stretch,addon.preferences.uv_show_orientation,
        addon.preferences.uv_show_checker))
    if not active and not any(state.uv_native_materials or state.uv_native_scratch for state in states):
        return
    diagnostic.stop_refresh_timer()
    for scene in bpy.data.scenes:
        if hasattr(scene, 'model_checker_state'):
            state = scene.model_checker_state
            state.refresh_in_progress = True
            state.uv_native_scratch = ''
            state.uv_native_materials.clear()
    native_preview.remove_all()
    # Flush removal before undo frees the old material/node-tree evaluations.
    if bpy.context.view_layer is not None:
        bpy.context.view_layer.update()


@persistent
def _refresh_native_after_undo(_dummy):
    addon = bpy.context.preferences.addons.get(ADDON_ID)
    if addon is None:
        return
    for scene in bpy.data.scenes:
        if hasattr(scene, 'model_checker_state'):
            state = scene.model_checker_state
            state.refresh_in_progress = False
            _sync_scene_overlay(scene,addon.preferences)
            for item in state.uv_mesh_states:
                item.native_signature = ''
                item.native_force_refresh = True
            diagnostic.mark_full_refresh(state)
    diagnostic.start_refresh_timer()


class MC_PG_native_material(bpy.types.PropertyGroup):
    # Transient preview IDs can disappear across undo. Names are resolved
    # against the current Main instead of embedding their pointers in Scene.
    source: StringProperty(options={'SKIP_SAVE'})
    material: StringProperty(options={'SKIP_SAVE'})
    signature: StringProperty(options={'SKIP_SAVE'})
    key: StringProperty(options={'SKIP_SAVE'})


class MC_PG_mesh_state(bpy.types.PropertyGroup):
    """Cached source signature and invalidation state for one mesh."""

    mesh: PointerProperty(type=bpy.types.Mesh, options={'SKIP_SAVE'})
    native_signature: StringProperty(options={'SKIP_SAVE'})
    native_source_signature: StringProperty(options={'SKIP_SAVE'})
    native_force_refresh: BoolProperty(default=False, options={'SKIP_SAVE'})
    source_signature: StringProperty(default="", options={'SKIP_SAVE'})
    dirty: BoolProperty(default=False, options={'SKIP_SAVE'})
    transform_only: BoolProperty(default=False, options={'SKIP_SAVE'})


class MC_PG_scene_state(bpy.types.PropertyGroup):
    """Per-file runtime state used by the viewport overlay."""

    check_feedback_message: StringProperty(default="", options={'SKIP_SAVE'})
    check_feedback_level: EnumProperty(
        items=(
            ('INFO', "信息", ""),
            ('WARNING', "提醒", ""),
        ),
        default='INFO',
        options={'SKIP_SAVE'},
    )
    check_feedback_until: FloatProperty(default=0.0, options={'SKIP_SAVE'})
    overlay_active: BoolProperty(default=False, options={'SKIP_SAVE'})
    overlay_cache_dirty: BoolProperty(default=True, options={'SKIP_SAVE'})
    uv_display_dirty: BoolProperty(default=True, options={'SKIP_SAVE'})
    uv_display_full_refresh: BoolProperty(default=True, options={'SKIP_SAVE'})
    uv_display_refresh_cursor: IntProperty(default=0, options={'SKIP_SAVE'})
    uv_display_defer_until: FloatProperty(default=0.0, options={'SKIP_SAVE'})
    uv_native_materials: CollectionProperty(type=MC_PG_native_material, options={'SKIP_SAVE'})
    uv_native_scratch: StringProperty(options={'SKIP_SAVE'})
    native_resume: BoolProperty(default=False, options={'SKIP_SAVE'})
    uv_mesh_states: CollectionProperty(type=MC_PG_mesh_state, options={'SKIP_SAVE'})
    refresh_in_progress: BoolProperty(default=False, options={'SKIP_SAVE'})
    uv_display_mode: EnumProperty(
        items=(('CLASSIC', "实体", ""), ('MATERIAL', "材质", "")),
        default='CLASSIC', options={'SKIP_SAVE'},
    )
    uv_show_stretch: BoolProperty(default=False, options={'SKIP_SAVE'})
    uv_stretch_opacity: FloatProperty(default=0.85, min=0.0, max=1.0, options={'SKIP_SAVE'})
    uv_stretch_base_color: FloatVectorProperty(size=3, subtype='COLOR', default=(0.43, 0.78, 1.0), min=0.0, max=1.0, options={'SKIP_SAVE'})
    uv_stretch_max_color: FloatVectorProperty(size=3, subtype='COLOR', default=(1.0, 0.0, 0.0), min=0.0, max=1.0, options={'SKIP_SAVE'})
    uv_stretch_strength: FloatProperty(default=2.0, min=0.25, max=8.0, options={'SKIP_SAVE'})
    uv_stretch_threshold: FloatProperty(default=0.2, min=0.0, max=0.999, subtype='PERCENTAGE', options={'SKIP_SAVE'})
    uv_show_boundaries: BoolProperty(default=False, options={'SKIP_SAVE'})
    uv_boundary_color: FloatVectorProperty(size=3, subtype='COLOR', default=(1.0, 0.55, 0.08), min=0.0, max=1.0, options={'SKIP_SAVE'})
    uv_boundary_width: FloatProperty(default=4.0, min=1.0, max=12.0, options={'SKIP_SAVE'})
    uv_show_checker: BoolProperty(default=False, options={'SKIP_SAVE'})
    uv_checker_opacity: FloatProperty(default=0.75, min=0.0, max=1.0, options={'SKIP_SAVE'})
    uv_checker_density: IntProperty(default=64, min=1, max=256, options={'SKIP_SAVE'})
    uv_checker_color_a: FloatVectorProperty(size=3, subtype='COLOR', default=(0.95, 0.95, 0.95), min=0.0, max=1.0, options={'SKIP_SAVE'})
    uv_checker_color_b: FloatVectorProperty(size=3, subtype='COLOR', default=(0.12, 0.12, 0.12), min=0.0, max=1.0, options={'SKIP_SAVE'})
    uv_checker_pattern: EnumProperty(items=diagnostic.CHECKER_PATTERN_ITEMS, default='CLASSIC', options={'SKIP_SAVE'})
    uv_checker_custom_image: StringProperty(default="", subtype='FILE_PATH', options={'SKIP_SAVE'})
    uv_show_orientation: BoolProperty(default=False, options={'SKIP_SAVE'})
    uv_orientation_opacity: FloatProperty(default=0.8, min=0.0, max=1.0, options={'SKIP_SAVE'})
    uv_orientation_front_color: FloatVectorProperty(size=3, subtype='COLOR', default=(0.43, 0.78, 1.0), min=0.0, max=1.0, options={'SKIP_SAVE'})
    uv_orientation_back_color: FloatVectorProperty(size=3, subtype='COLOR', default=(0.94, 0.045, 0.055), min=0.0, max=1.0, options={'SKIP_SAVE'})
    preview_shading_saved: BoolProperty(default=False, options={'SKIP_SAVE'})
    preview_area_pointer: StringProperty(default="", options={'SKIP_SAVE'})
    preview_shading_type: EnumProperty(
        items=(
            ('SOLID', "实体", ""),
            ('MATERIAL', "材质预览", ""),
            ('RENDERED', "渲染", ""),
            ('WIREFRAME', "线框", ""),
        ),
        default='SOLID',
        options={'SKIP_SAVE'},
    )
    preview_shading_color_type: EnumProperty(
        items=(
            ('MATERIAL', "材质", ""),
            ('OBJECT', "物体", ""),
            ('RANDOM', "随机", ""),
            ('VERTEX', "颜色属性", ""),
            ('TEXTURE', "纹理", ""),
            ('SINGLE', "单色", ""),
        ),
        default='MATERIAL',
        options={'SKIP_SAVE'},
    )


class MC_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_ID

    uv_display_mode: EnumProperty(name="显示方式", items=(
        ('CLASSIC', "实体", "使用原来的实体视图显示"),
        ('MATERIAL', "材质", "使用有光照的检查材质；颜色内 Alpha 为零时显示原材质")),
        default='MATERIAL', update=_update_uv_display_mode)
    uv_display_mode_version: IntProperty(default=0, options={'HIDDEN'})
    uv_material_colors_initialized: BoolProperty(default=False, options={'HIDDEN'})
    uv_material_base_color: FloatVectorProperty(name="无拉伸颜色", size=4, subtype='COLOR', default=(.43,.78,1,0.0), min=0, max=1, update=_update_scene_overlay)
    uv_material_max_color: FloatVectorProperty(name="最大拉伸颜色", size=4, subtype='COLOR', default=(1,0,0,.85), min=0, max=1, update=_update_scene_overlay)
    uv_material_front_color: FloatVectorProperty(name="正向颜色", size=4, subtype='COLOR', default=(.43,.78,1,0.0), min=0, max=1, update=_update_scene_overlay)
    uv_material_back_color: FloatVectorProperty(name="反向颜色", size=4, subtype='COLOR', default=(.94,.045,.055,.8), min=0, max=1, update=_update_scene_overlay)
    uv_mode_normalizing: BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})
    ngon_clear_selection: BoolProperty(name="运行前清除其他选择", default=True)
    ngon_sides: IntProperty(name="边数大于", description="顶点数超过此值的面会被选中", default=4, min=3, max=100)
    uv_show_stretch: BoolProperty(name="显示 UV 拉伸", default=False, update=_update_uv_stretch)
    uv_stretch_opacity: FloatProperty(name="热力图透明度", default=0.85, min=0.0, max=1.0, update=_update_scene_overlay)
    uv_stretch_base_color: FloatVectorProperty(name="无拉伸颜色", subtype='COLOR', size=3, default=(0.43, 0.78, 1.0), min=0.0, max=1.0, update=_update_scene_overlay)
    uv_stretch_max_color: FloatVectorProperty(name="拉伸颜色", subtype='COLOR', size=3, default=(1.0, 0.0, 0.0), min=0.0, max=1.0, update=_update_scene_overlay)
    uv_stretch_strength: FloatProperty(name="拉伸显示强度", description="提高后较轻微的拉伸也会有更明显的颜色变化", default=2.0, min=0.25, max=8.0, update=_update_scene_overlay)
    uv_stretch_threshold: FloatProperty(name="可接受拉伸阈值", description="低于此阈值的拉伸按无拉伸颜色显示", default=0.2, min=0.0, max=0.999, subtype='PERCENTAGE', update=_update_scene_overlay)
    uv_show_boundaries: BoolProperty(name="显示 UV 分界边", default=False, update=_update_scene_overlay)
    uv_boundary_color: FloatVectorProperty(name="边界颜色", subtype='COLOR', size=3, default=(1.0, 0.55, 0.08), min=0.0, max=1.0, update=_update_scene_overlay)
    uv_boundary_width: FloatProperty(name="分界线粗细", default=4.0, min=1.0, max=12.0, update=_update_boundary_width)
    uv_show_checker: BoolProperty(name="显示棋盘格", default=False, update=_update_uv_checker)
    uv_checker_opacity: FloatProperty(name="棋盘格透明度", default=0.75, min=0.0, max=1.0, update=_update_checker_settings)
    uv_checker_density: IntProperty(name="棋盘格密度", default=64, min=1, max=256, update=_update_checker_settings)
    uv_checker_color_a: FloatVectorProperty(name="棋盘格颜色 A", subtype='COLOR', size=3, default=(0.95, 0.95, 0.95), min=0.0, max=1.0, update=_update_checker_settings)
    uv_checker_color_b: FloatVectorProperty(name="棋盘格颜色 B", subtype='COLOR', size=3, default=(0.12, 0.12, 0.12), min=0.0, max=1.0, update=_update_checker_settings)
    uv_show_orientation: BoolProperty(name="显示 UV 正反", default=False, update=_update_uv_orientation)
    uv_orientation_opacity: FloatProperty(name="UV 正反透明度", default=0.8, min=0.0, max=1.0, update=_update_scene_overlay)
    uv_orientation_front_color: FloatVectorProperty(name="正向颜色", subtype='COLOR', size=3, default=(0.43, 0.78, 1.0), min=0.0, max=1.0, update=_update_scene_overlay)
    uv_orientation_back_color: FloatVectorProperty(name="反向颜色", subtype='COLOR', size=3, default=(0.94, 0.045, 0.055), min=0.0, max=1.0, update=_update_scene_overlay)
    isolated_include_hidden: BoolProperty(name="包括隐藏顶点", default=False)
    isolated_clear_selection: BoolProperty(name="运行前清除其他选择", default=True)
    uv_overlay_tuning_version: IntProperty(default=0, options={'HIDDEN'})

    def draw(self, context):
        layout = self.layout
        layout.label(text="多边面")
        row = layout.row()
        row.label(text="边数大于")
        row.prop(self, "ngon_sides", text="")
        layout.prop(self, "ngon_clear_selection")
        layout.separator()
        layout.label(text="UV 显示")
        layout.prop(self, "uv_stretch_opacity", slider=True)
        layout.prop(self, "uv_stretch_base_color")
        layout.prop(self, "uv_stretch_max_color")
        layout.prop(self, "uv_stretch_strength", slider=True)
        layout.prop(self, "uv_stretch_threshold", slider=True)
        layout.prop(self, "uv_boundary_color")
        layout.prop(self, "uv_boundary_width", slider=True)
        layout.prop(self, "uv_checker_opacity", slider=True)
        layout.prop(self, "uv_checker_density")
        layout.prop(self, "uv_checker_color_a")
        layout.prop(self, "uv_checker_color_b")
        layout.prop(self, "uv_orientation_opacity", slider=True)
        layout.prop(self, "uv_orientation_front_color")
        layout.prop(self, "uv_orientation_back_color")
        layout.separator()
        layout.label(text="孤立顶点")
        layout.prop(self, "isolated_include_hidden")
        layout.prop(self, "isolated_clear_selection")


CLASSES = (
    MC_PG_native_material,
    MC_PG_mesh_state,
    MC_PG_scene_state,
    operators.MC_OT_check_ngons,
    operators.MC_OT_check_isolated_vertices,
    operators.MC_OT_check_collapsed_uv,
    operators.MC_OT_check_model_boundaries,
    operators.MC_OT_toggle_uv_overlay,
    operators.MC_OT_uv_checks,
    operators.MC_OT_settings_popup,
    panel.VIEW3D_PT_ModelChecker,
)


def _registered_class_named(cls):
    """Find this class or an older registered class with the same name."""
    if cls.is_registered:
        return cls
    for base in cls.__bases__:
        for candidate in base.__subclasses__():
            if candidate.__name__ == cls.__name__ and candidate.is_registered:
                return candidate
    return None


def _remove_registered_classes():
    if hasattr(bpy.types.Scene, "model_checker_state"):
        del bpy.types.Scene.model_checker_state
    for cls in reversed(CLASSES):
        registered = _registered_class_named(cls)
        if registered is not None:
            bpy.utils.unregister_class(registered)


def _migrate_overlay_opacity_defaults():
    addon = bpy.context.preferences.addons.get(ADDON_ID)
    if addon is None:
        return
    prefs = addon.preferences
    _enforce_uv_display_exclusivity(prefs)
    if prefs.uv_overlay_tuning_version >= 6:
        return
    if prefs.uv_overlay_tuning_version >= 4:
        prefs.uv_stretch_base_color = tuple(prefs.uv_orientation_front_color)
        prefs.uv_overlay_tuning_version = 6
        return
    # Raise only old defaults; preserve custom opacity values.
    if prefs.uv_overlay_tuning_version < 2:
        if abs(prefs.uv_stretch_opacity - 0.45) < 1e-6 or abs(prefs.uv_stretch_opacity - 0.18) < 1e-6:
            prefs.uv_stretch_opacity = 0.12
        if abs(prefs.uv_orientation_opacity - 0.45) < 1e-6:
            prefs.uv_orientation_opacity = 0.18
    if abs(prefs.uv_stretch_opacity - 0.12) < 1e-6:
        prefs.uv_stretch_opacity = 0.85
    if abs(prefs.uv_checker_opacity - 0.24) < 1e-6:
        prefs.uv_checker_opacity = 0.75
    if abs(prefs.uv_orientation_opacity - 0.18) < 1e-6:
        prefs.uv_orientation_opacity = 0.9
    old_max = (0.72, 0.34, 0.025)
    prefs.uv_stretch_base_color = tuple(prefs.uv_orientation_front_color)
    if all(abs(a - b) < 1e-6 for a, b in zip(prefs.uv_stretch_max_color, old_max)):
        prefs.uv_stretch_max_color = (1.0, 0.0, 0.0)
    prefs.uv_overlay_tuning_version = 6


def _migrate_check_display_defaults(prefs):
    if prefs.uv_overlay_tuning_version >= 9:
        return
    if prefs.uv_overlay_tuning_version < 7:
        for name, old_alpha in (('uv_material_base_color', 0.85), ('uv_material_front_color', 0.9)):
            color = tuple(getattr(prefs, name))
            if abs(color[3] - old_alpha) < 1e-6:
                setattr(prefs, name, (*color[:3], 0.0))
        densities = _read_checker_densities(prefs)
        _write_checker_densities(prefs, {key: 64 if value == 8 else value for key, value in densities.items()})
        if prefs.uv_checker_density == 8:
            prefs.uv_checker_density = 64
    if abs(prefs.uv_stretch_threshold) < 1e-6:
        prefs.uv_stretch_threshold = 0.2
    if prefs.uv_overlay_tuning_version < 9:
        if abs(prefs.uv_orientation_opacity - 0.9) < 1e-6:
            prefs.uv_orientation_opacity = 0.8
        back_color = tuple(prefs.uv_material_back_color)
        if len(back_color) == 4 and abs(back_color[3] - 0.9) < 1e-6:
            prefs.uv_material_back_color = (*back_color[:3], 0.8)
    prefs.uv_overlay_tuning_version = 9
    _schedule_preferences_save()


def migrate_preferences():
    """Copy the former stand-alone add-on preferences into this add-on once."""
    addon = bpy.context.preferences.addons.get(ADDON_ID)
    legacy_addon = bpy.context.preferences.addons.get("Model_Checker")
    if addon is None or legacy_addon is None:
        return
    prefs = addon.preferences
    if not prefs.model_checker_migrated:
        setting_names = (
            "ngon_clear_selection", "ngon_sides", "uv_show_stretch",
            "uv_stretch_opacity", "uv_stretch_base_color", "uv_stretch_max_color",
            "uv_stretch_strength", "uv_stretch_threshold", "uv_show_boundaries",
            "uv_boundary_color", "uv_show_checker", "uv_checker_opacity",
            "uv_checker_density", "uv_checker_color_a", "uv_checker_color_b",
            "uv_show_orientation", "uv_orientation_opacity",
            "uv_orientation_front_color", "uv_orientation_back_color",
            "isolated_include_hidden", "isolated_clear_selection",
            "uv_overlay_tuning_version",
        )
        for name in setting_names:
            setattr(prefs, name, getattr(legacy_addon.preferences, name))
        prefs.model_checker_migrated = True
    _migrate_overlay_opacity_defaults()
    _migrate_check_display_defaults(prefs)
    import addon_utils
    addon_utils.disable("Model_Checker", default_set=True)


def _initialize_after_register():
    """Apply scene settings after Blender leaves its restricted register phase."""
    _migrate_overlay_opacity_defaults()
    addon = bpy.context.preferences.addons.get(ADDON_ID)
    if addon is not None:
        prefs = addon.preferences
        _migrate_check_display_defaults(prefs)
        if prefs.uv_display_mode_version < 1:
            prefs.uv_display_mode = 'MATERIAL'
            prefs.uv_display_mode_version = 1
            _schedule_preferences_save()
        needs_save = any((
            prefs.uv_show_stretch,
            prefs.uv_show_boundaries,
            prefs.uv_show_checker,
            prefs.uv_show_orientation,
        ))
        prefs.uv_show_stretch = False
        prefs.uv_show_boundaries = False
        prefs.uv_show_checker = False
        prefs.uv_show_orientation = False
        if needs_save:
            _schedule_preferences_save()
        _init_checker_density_key(prefs)
        _enforce_uv_display_exclusivity(prefs)
        _sync_all_scenes(prefs)
        diagnostic.start_refresh_timer()
    return None


def register():
    for cls in CLASSES:
        registered = _registered_class_named(cls)
        if registered is not None and registered is not cls:
            _remove_registered_classes()
            break
    for cls in CLASSES:
        if _registered_class_named(cls) is None:
            bpy.utils.register_class(cls)
    if not hasattr(bpy.types.Scene, "model_checker_state"):
        bpy.types.Scene.model_checker_state = PointerProperty(type=MC_PG_scene_state)
    if _restore_overlay_settings not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_restore_overlay_settings)
    for handler_list in (
        bpy.app.handlers.save_pre,
        bpy.app.handlers.render_pre,
        bpy.app.handlers.load_pre,
    ):
        if _disable_checker_before_lifecycle_change not in handler_list:
            handler_list.append(_disable_checker_before_lifecycle_change)
    if not bpy.app.timers.is_registered(_initialize_after_register):
        bpy.app.timers.register(_initialize_after_register, first_interval=0.1)
    for handlers in (bpy.app.handlers.save_post, bpy.app.handlers.render_post,
                     bpy.app.handlers.render_cancel):
        if _resume_native_preview not in handlers:
            handlers.append(_resume_native_preview)
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _refresh_native_after_undo not in handlers:
            handlers.append(_refresh_native_after_undo)
    for handlers in (bpy.app.handlers.undo_pre, bpy.app.handlers.redo_pre):
        if _suspend_native_before_undo not in handlers:
            handlers.append(_suspend_native_before_undo)
    overlay.register_draw_handler()


def unregister():
    if bpy.app.timers.is_registered(migrate_preferences):
        bpy.app.timers.unregister(migrate_preferences)
    if bpy.app.timers.is_registered(_save_user_preferences):
        bpy.app.timers.unregister(_save_user_preferences)
    addon = bpy.context.preferences.addons.get(ADDON_ID)
    if addon is not None and addon.preferences.uv_show_checker:
        addon.preferences.uv_show_checker = False
    diagnostic.cleanup_all()
    if bpy.app.timers.is_registered(_redraw_viewports):
        bpy.app.timers.unregister(_redraw_viewports)
    if bpy.app.timers.is_registered(_initialize_after_register):
        bpy.app.timers.unregister(_initialize_after_register)
    if _restore_overlay_settings in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_restore_overlay_settings)
    for handler_list in (
        bpy.app.handlers.save_pre,
        bpy.app.handlers.render_pre,
        bpy.app.handlers.load_pre,
    ):
        if _disable_checker_before_lifecycle_change in handler_list:
            handler_list.remove(_disable_checker_before_lifecycle_change)
    for handlers in (bpy.app.handlers.save_post, bpy.app.handlers.render_post,
                     bpy.app.handlers.render_cancel):
        if _resume_native_preview in handlers:
            handlers.remove(_resume_native_preview)
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _refresh_native_after_undo in handlers:
            handlers.remove(_refresh_native_after_undo)
    for handlers in (bpy.app.handlers.undo_pre, bpy.app.handlers.redo_pre):
        if _suspend_native_before_undo in handlers:
            handlers.remove(_suspend_native_before_undo)
    overlay.unregister_draw_handler()
    operators.unregister_keymap()
    _remove_registered_classes()


if __name__ == "__main__":
    register()
