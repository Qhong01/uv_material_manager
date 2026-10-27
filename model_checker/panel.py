"""Collapsible model-checking panel nested at the bottom of the main panel."""

import bpy


class VIEW3D_PT_ModelChecker(bpy.types.Panel):
    bl_label = "检查"
    bl_idname = "VIEW3D_PT_UVMM_ModelChecker"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_parent_id = "VIEW3D_PT_UltimateManager"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        state = context.scene.model_checker_state
        grid = self.layout.grid_flow(columns=3, even_columns=True, even_rows=True, align=True)
        features = (
            ("多边面", "mesh.model_checker_check_ngons", None, 'NGONS'),
            ("UV拉伸", "mesh.model_checker_toggle_uv", 'uv_show_stretch', 'UV_STRETCH'),
            ("UV分界", "mesh.model_checker_toggle_uv", 'uv_show_boundaries', 'UV_BOUNDARIES'),
            ("棋盘格", "mesh.model_checker_toggle_uv", 'uv_show_checker', 'UV_CHECKER'),
            ("UV正反", "mesh.model_checker_toggle_uv", 'uv_show_orientation', 'UV_ORIENTATION'),
            ("孤立点", "mesh.model_checker_check_isolated", None, 'ISOLATED'),
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
            settings = row.operator("wm.model_checker_settings", text="", icon='PREFERENCES')
            settings.target = settings_target
