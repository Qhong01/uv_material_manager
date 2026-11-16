"""Collapsible model-checking panel below material management."""

import bpy


class VIEW3D_PT_ModelChecker(bpy.types.Panel):
    bl_label = "检查"
    bl_idname = "VIEW3D_PT_UVMM_ModelChecker"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "UV Tools"
    bl_order = 4
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        state = context.scene.model_checker_state
        addon = context.preferences.addons.get(__package__.split('.')[0])
        if addon is not None:
            self.layout.row(align=True).prop(addon.preferences, 'uv_display_mode', expand=True)
        grid = self.layout.grid_flow(row_major=True, columns=3, even_columns=True, even_rows=True, align=True)
        features = (
            ("多边面", "mesh.model_checker_check_ngons", None, 'NGONS'),
            ("UV分界", "mesh.model_checker_toggle_uv", 'uv_show_boundaries', 'UV_BOUNDARIES'),
            ("UV正反", "mesh.model_checker_toggle_uv", 'uv_show_orientation', 'UV_ORIENTATION'),
            ("UV拉伸", "mesh.model_checker_toggle_uv", 'uv_show_stretch', 'UV_STRETCH'),
            ("棋盘格", "mesh.model_checker_toggle_uv", 'uv_show_checker', 'UV_CHECKER'),
            ("孤立点", "mesh.model_checker_check_isolated", None, 'ISOLATED'),
            ("UV缩点", "mesh.model_checker_check_collapsed_uv", None, None),
            ("模型边界", "mesh.model_checker_check_model_boundaries", None, None),
        )
        for label, operator_id, feature, settings_target in features:
            row = grid.row(align=True)
            row.operator_context = 'INVOKE_DEFAULT'
            if feature is None:
                row.operator(operator_id, text=label)
            else:
                toggle = row.operator(
                    operator_id,
                    text=label,
                    depress=getattr(state, feature),
                )
                toggle.feature = feature
            if settings_target is not None:
                settings = row.operator("wm.model_checker_settings", text="", icon='PREFERENCES')
                settings.target = settings_target

        row = self.layout.row()
        row.operator_context = 'INVOKE_DEFAULT'
        row.operator('mesh.model_checker_uv_checks', text='UV 综合检查', icon='CHECKMARK',
                     depress=any((state.uv_show_stretch, state.uv_show_orientation, state.uv_show_checker)))
