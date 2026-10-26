"""Topology-aware connect/merge action for Edit Mode vertex selections."""

import bpy
import bmesh
from bpy.props import BoolProperty, EnumProperty
from bpy.types import Operator


def _active_selected_vertex(bm):
    active = bm.select_history.active
    if isinstance(active, bmesh.types.BMVert) and active.is_valid and active.select:
        return active
    return None


class MESH_OT_uvmm_smart_vertex(Operator):
    bl_idname = "mesh.uvmm_smart_vertex"
    bl_label = "智能顶点"
    bl_description = (
        "选择多个顶点并指定最后激活点时合并到该点，或合并到所选顶点中心"
    )
    bl_options = {'REGISTER', 'UNDO'}

    merge_position: EnumProperty(
        name="合并位置",
        description="普通顶点合并时使用的位置",
        items=(
            ('LAST', "最后选中点", "合并到选择历史中的最后一个顶点"),
            ('CENTER', "中心", "合并到所选顶点的中心"),
        ),
        default='LAST',
    )
    affect_uv: BoolProperty(
        name="影响 UV",
        description="合并时由 Blender 修正 UV；关闭时保留 UV 坐标",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return (
            context.mode == 'EDIT_MESH'
            and context.edit_object is not None
            and context.edit_object.type == 'MESH'
            and context.tool_settings.mesh_select_mode[0]
        )

    def execute(self, context):
        obj = context.edit_object
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        selected = [vert for vert in bm.verts if vert.select and not vert.hide]
        active = _active_selected_vertex(bm)
        if len(selected) < 2 or active is None:
            self.report({'WARNING'}, "请至少选择两个顶点，并最后点选目标顶点")
            return {'CANCELLED'}

        merge_type = 'LAST' if self.merge_position == 'LAST' else 'CENTER'
        result = bpy.ops.mesh.merge(type=merge_type, uvs=self.affect_uv)
        if 'FINISHED' not in result:
            self.report({'WARNING'}, "顶点合并未执行")
            return {'CANCELLED'}
        return {'FINISHED'}


class MESH_OT_uvmm_smart_vertex_shortcut(Operator):
    bl_idname = "mesh.uvmm_smart_vertex_shortcut"
    bl_label = "智能顶点快捷键分发"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH' and context.edit_object is not None

    def execute(self, context):
        bpy.ops.mesh.select_mode(type='VERT')
        result = bpy.ops.mesh.uvmm_smart_vertex('EXEC_DEFAULT')
        return result


CLASSES = (MESH_OT_uvmm_smart_vertex, MESH_OT_uvmm_smart_vertex_shortcut)


def register():
    for cls in CLASSES:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            bpy.utils.unregister_class(cls)
            bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
