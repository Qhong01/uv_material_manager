"""Selection-aware Smart Edge actions for Edit Mode meshes."""

import bpy
import bmesh
from bpy.types import Operator


def _is_closed_edge_loop(edges):
    if len(edges) < 3:
        return False

    edge_set = set(edges)
    linked = {}
    for edge in edge_set:
        for vert in edge.verts:
            linked.setdefault(vert, set()).add(edge)
    if any(len(vertex_edges) != 2 for vertex_edges in linked.values()):
        return False

    visited = set()
    stack = [next(iter(edge_set))]
    while stack:
        edge = stack.pop()
        if edge in visited:
            continue
        visited.add(edge)
        for vert in edge.verts:
            stack.extend(linked[vert] - visited)
    return visited == edge_set


def _is_quad_diagonal(edge):
    if len(edge.link_faces) != 2:
        return False
    first_face, second_face = edge.link_faces
    if len(first_face.verts) != 3 or len(second_face.verts) != 3:
        return False
    quad_verts = set(first_face.verts) | set(second_face.verts)
    return len(quad_verts) == 4


def _connect_selected_verts_to_active(bm, selected_verts):
    active_vert = bm.select_history.active
    if (
        not isinstance(active_vert, bmesh.types.BMVert)
        or not active_vert.select
        or active_vert.hide
    ):
        active_vert = next(
            (
                element
                for element in reversed(list(bm.select_history))
                if isinstance(element, bmesh.types.BMVert)
                and element.select
                and not element.hide
            ),
            None,
        )
    if active_vert is None:
        return [], "请最后激活要连接到的目标顶点"

    sources = [vert for vert in selected_verts if vert is not active_vert]
    if not sources:
        return [], "请至少选择两个顶点，并最后激活目标顶点"

    bm.verts.index_update()
    active_index = active_vert.index
    source_indices = [vert.index for vert in sources]

    # Preflight on a copy so an unsupported pair cannot leave half the fan
    # connected in the user's edit mesh.
    staged_bm = bm.copy()
    staged_bm.verts.ensure_lookup_table()
    try:
        for source_index in source_indices:
            source = staged_bm.verts[source_index]
            target = staged_bm.verts[active_index]
            if staged_bm.edges.get((source, target)):
                continue
            bmesh.ops.connect_vert_pair(staged_bm, verts=[source, target])
    except (RuntimeError, ValueError):
        return [], "所选顶点之间无法沿网格面连接"
    finally:
        staged_bm.free()

    new_edges = []
    for source in sources:
        if bm.edges.get((source, active_vert)):
            continue
        result = bmesh.ops.connect_vert_pair(bm, verts=[source, active_vert])
        new_edges.extend(edge for edge in result.get("edges", ()) if edge.is_valid)

    if not new_edges:
        return [], "所选顶点已连接到目标顶点"
    return new_edges, None


class MESH_OT_uvmm_smart_edge(Operator):
    bl_idname = "mesh.uvmm_smart_edge"
    bl_label = "智能边"
    bl_description = (
        "按当前选择连接顶点、切换对角边或切换环内区域；"
        "未选择或只选一个顶点时启动 Blender 原生切割"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (
            context.mode == 'EDIT_MESH'
            and context.edit_object is not None
            and context.edit_object.type == 'MESH'
        )

    def _start_native_knife(self):
        result = bpy.ops.mesh.knife_tool('INVOKE_DEFAULT')
        return {'FINISHED'} if 'RUNNING_MODAL' in result else result

    def _run_action(self, context):
        obj = context.edit_object
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        selected_verts = [vert for vert in bm.verts if vert.select and not vert.hide]
        selected_edges = [edge for edge in bm.edges if edge.select and not edge.hide]
        selected_faces = [face for face in bm.faces if face.select and not face.hide]
        vert_mode, edge_mode, face_mode = context.tool_settings.mesh_select_mode

        if not selected_edges and not selected_faces and len(selected_verts) <= 1:
            return self._start_native_knife()

        # Connect every selected vertex to the last active selected vertex.
        if vert_mode and not edge_mode and not face_mode:
            new_edges, error = _connect_selected_verts_to_active(bm, selected_verts)
            if error:
                self.report({'WARNING'}, error)
                return {'CANCELLED'}
            if not new_edges:
                self.report({'INFO'}, "所选顶点已连接到目标顶点")
                return {'CANCELLED'}
            for edge in new_edges:
                edge.select_set(True)
            bm.normal_update()
            bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=True)
            return {'FINISHED'}

        # An edge loop converted to a region has selected faces; convert that
        # region back to its boundary loop on the next invocation.
        if edge_mode and not face_mode:
            if selected_faces:
                return bpy.ops.mesh.region_to_loop()

            if len(selected_edges) == 1 and _is_quad_diagonal(selected_edges[0]):
                return bpy.ops.mesh.edge_rotate(use_ccw=True)

            if _is_closed_edge_loop(selected_edges):
                return bpy.ops.mesh.loop_to_region(select_bigger=False)

        self.report(
            {'INFO'},
            "请在顶点模式选择多个顶点，或在边模式选择四边面内部边/闭合边环",
        )
        return {'CANCELLED'}

    def invoke(self, context, event):
        return self._run_action(context)

    def execute(self, context):
        return self._run_action(context)


class MESH_OT_uvmm_smart_edge_shortcut(Operator):
    bl_idname = "mesh.uvmm_smart_edge_shortcut"
    bl_label = "智能边快捷键分发"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH' and context.edit_object is not None

    def execute(self, context):
        # Preserve the current component mode so Smart Edge can distinguish
        # vertex, diagonal-edge, and closed-loop selections.
        result = bpy.ops.mesh.uvmm_smart_edge('INVOKE_DEFAULT')
        return {'FINISHED'} if 'RUNNING_MODAL' in result else result


CLASSES = (MESH_OT_uvmm_smart_edge, MESH_OT_uvmm_smart_edge_shortcut)


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
