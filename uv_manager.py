import bpy
import json
import bmesh
from mathutils import Vector
from bpy.types import Operator, UIList
from bpy.props import IntProperty
from bpy.app.handlers import persistent

# =========================================================================
# UV层独立专属缝合边管理器 (Per-UV-Layer Seams Manager)
# =========================================================================

class UVSeamsManager:
    """每个 UV 层独立专属缝合边的存储与切换引擎"""

    @staticmethod
    def get_current_seams(obj):
        """获取当前网格当前的缝合边索引列表"""
        if not obj or obj.type != 'MESH':
            return []
        me = obj.data
        if obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(me)
            return [e.index for e in bm.edges if e.seam]
        else:
            return [e.index for e in me.edges if e.use_seam]

    @staticmethod
    def set_current_seams(obj, seam_indices):
        """将指定的缝合边索引应用到网格并即时刷新视口"""
        if not obj or obj.type != 'MESH':
            return
        me = obj.data
        seam_set = set(seam_indices)
        if obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(me)
            for e in bm.edges:
                e.seam = (e.index in seam_set)
            bmesh.update_edit_mesh(me)
        else:
            for e in me.edges:
                e.use_seam = (e.index in seam_set)
            me.update()

    @classmethod
    def save_seams_for_uv(cls, obj, uv_name=None):
        """保存指定（或当前活动）UV 层的缝合边"""
        if not obj or obj.type != 'MESH' or not obj.data.uv_layers:
            return
        if uv_name is None:
            active_uv = obj.data.uv_layers.active
            if not active_uv:
                return
            uv_name = active_uv.name

        me = obj.data
        current_seams = cls.get_current_seams(obj)
        try:
            seams_dict = json.loads(me.get("_uv_seams_dict", "{}"))
        except Exception:
            seams_dict = {}

        seams_dict[uv_name] = current_seams
        me["_uv_seams_dict"] = json.dumps(seams_dict)

    @classmethod
    def load_seams_for_uv(cls, obj, uv_name=None):
        """载入指定（或当前活动）UV 层的专属缝合边"""
        if not obj or obj.type != 'MESH' or not obj.data.uv_layers:
            return
        if uv_name is None:
            active_uv = obj.data.uv_layers.active
            if not active_uv:
                return
            uv_name = active_uv.name

        me = obj.data
        try:
            seams_dict = json.loads(me.get("_uv_seams_dict", "{}"))
        except Exception:
            seams_dict = {}

        if uv_name in seams_dict:
            cls.set_current_seams(obj, seams_dict[uv_name])
        else:
            # 该层尚未记录过缝合边，将当前缝合边自动存为该层的初始数据
            current_seams = cls.get_current_seams(obj)
            seams_dict[uv_name] = current_seams
            me["_uv_seams_dict"] = json.dumps(seams_dict)

    @classmethod
    def switch_layer_with_seams(cls, obj, target_index):
        """切换 UV 层并自动换存专属缝合边"""
        if not obj or obj.type != 'MESH' or not obj.data.uv_layers:
            return
        layers = obj.data.uv_layers
        if not (0 <= target_index < len(layers)):
            return
        if layers.active_index == target_index:
            return

        old_uv_name = layers.active.name if layers.active else None
        if old_uv_name:
            cls.save_seams_for_uv(obj, old_uv_name)

        UVAutoSync.record_active_index(obj.name, target_index)
        layers.active_index = target_index

        new_uv_name = layers.active.name if layers.active else None
        if new_uv_name:
            cls.load_seams_for_uv(obj, new_uv_name)

    @classmethod
    def generate_seams_from_uv(cls, obj):
        """从当前活动 UV 层的岛边界自动生成专属缝合边"""
        if not obj or obj.type != 'MESH' or not obj.data.uv_layers:
            return 0
        me = obj.data
        is_edit = (obj.mode == 'EDIT')
        if is_edit:
            bm = bmesh.from_edit_mesh(me)
        else:
            bm = bmesh.new()
            bm.from_mesh(me)

        uv_lay = bm.loops.layers.uv.active
        if not uv_lay:
            if not is_edit:
                bm.free()
            return 0

        seam_count = 0
        for edge in bm.edges:
            if len(edge.link_faces) == 1:
                edge.seam = True
                seam_count += 1
            elif len(edge.link_faces) == 2:
                f1, f2 = edge.link_faces
                l1 = next((l for l in f1.loops if l.edge == edge), None)
                l2 = next((l for l in f2.loops if l.edge == edge), None)
                if not l1 or not l2:
                    continue
                v0, v1 = edge.verts
                uv1_v0 = l1[uv_lay].uv if l1.vert == v0 else l1.link_loop_next[uv_lay].uv
                uv1_v1 = l1[uv_lay].uv if l1.vert == v1 else l1.link_loop_next[uv_lay].uv
                uv2_v0 = l2[uv_lay].uv if l2.vert == v0 else l2.link_loop_next[uv_lay].uv
                uv2_v1 = l2[uv_lay].uv if l2.vert == v1 else l2.link_loop_next[uv_lay].uv

                if (uv1_v0 - uv2_v0).length > 1e-4 or (uv1_v1 - uv2_v1).length > 1e-4:
                    edge.seam = True
                    seam_count += 1
                else:
                    edge.seam = False
            else:
                edge.seam = True
                seam_count += 1

        if is_edit:
            bmesh.update_edit_mesh(me)
        else:
            bm.to_mesh(me)
            bm.free()
            me.update()

        cls.save_seams_for_uv(obj)
        return seam_count


# =========================================================================
# UV层自动同步引擎 (当活动物体切换UV层时，自动同步所有选中物体及专属缝合边)
# =========================================================================

class UVAutoSync:
    _last_active_index = {}
    _last_render_index = {}
    _is_syncing = False

    @classmethod
    def record_active_index(cls, obj_name, idx):
        cls._last_active_index[obj_name] = idx

    @classmethod
    def record_render_index(cls, obj_name, idx):
        cls._last_render_index[obj_name] = idx

    @classmethod
    def update_handler(cls, scene, depsgraph):
        if cls._is_syncing:
            return

        context = bpy.context
        active_obj = context.view_layer.objects.active
        if not active_obj or active_obj.type != 'MESH':
            return

        uv_layers = active_obj.data.uv_layers if active_obj.data else None
        if not uv_layers:
            return

        curr_active_idx = uv_layers.active_index
        prev_active_idx = cls._last_active_index.get(active_obj.name)

        # 检查当前渲染UV层
        curr_render_idx = None
        for i, l in enumerate(uv_layers):
            if l.active_render:
                curr_render_idx = i
                break
        prev_render_idx = cls._last_render_index.get(active_obj.name)

        cls._last_active_index[active_obj.name] = curr_active_idx
        cls._last_render_index[active_obj.name] = curr_render_idx

        # 检查是否发生索引变动
        has_active_change = (prev_active_idx is not None and prev_active_idx != curr_active_idx)
        has_render_change = (curr_render_idx is not None and prev_render_idx is not None and prev_render_idx != curr_render_idx)

        if not has_active_change and not has_render_change:
            return

        selected_objs = [o for o in context.selected_objects if o.type == 'MESH' and o != active_obj]

        cls._is_syncing = True
        try:
            # 1. 如果活动层在原生面板被切换，处理专属缝合边换存与多物体同步
            if has_active_change:
                old_layer_name = uv_layers[prev_active_idx].name if 0 <= prev_active_idx < len(uv_layers) else None
                if old_layer_name:
                    UVSeamsManager.save_seams_for_uv(active_obj, old_layer_name)
                new_layer_name = uv_layers[curr_active_idx].name if 0 <= curr_active_idx < len(uv_layers) else None
                if new_layer_name:
                    UVSeamsManager.load_seams_for_uv(active_obj, new_layer_name)

                for target_obj in selected_objs:
                    t_layers = target_obj.data.uv_layers
                    if t_layers and 0 <= curr_active_idx < len(t_layers):
                        UVSeamsManager.switch_layer_with_seams(target_obj, curr_active_idx)

            # 2. 同步渲染UV层
            if has_render_change:
                for target_obj in selected_objs:
                    t_layers = target_obj.data.uv_layers
                    if t_layers and 0 <= curr_render_idx < len(t_layers):
                        t_layers[curr_render_idx].active_render = True
        finally:
            cls._is_syncing = False


@persistent
def uv_depsgraph_update_handler(scene, depsgraph):
    UVAutoSync.update_handler(scene, depsgraph)


# =========================================================================
# UV管理功能与操作符
# =========================================================================

class UV_OT_AddLayer(Operator):
    bl_idname = "uv.add_layer_pro"
    bl_label = "添加UV层"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.object
        if obj and obj.type == 'MESH':
            UVSeamsManager.save_seams_for_uv(obj)
            layers = obj.data.uv_layers
            new_layer = layers.new(name=f"UVMap.{len(layers)+1}")
            layers.active_index = len(layers)-1
            UVSeamsManager.load_seams_for_uv(obj)
            sync_uv_layers(obj)
        return {'FINISHED'}


class UV_OT_RemoveLayer(Operator):
    bl_idname = "uv.remove_layer_pro"
    bl_label = "删除UV层"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.object and context.object.type == 'MESH' and len(context.object.data.uv_layers) > 1

    def execute(self, context):
        obj = context.object
        layers = obj.data.uv_layers
        old_active_name = layers.active.name if layers.active else None
        layers.remove(layers.active)
        layers.active_index = min(layers.active_index, len(layers)-1)
        if old_active_name:
            try:
                d = json.loads(obj.data.get("_uv_seams_dict", "{}"))
                d.pop(old_active_name, None)
                obj.data["_uv_seams_dict"] = json.dumps(d)
            except Exception:
                pass
        UVSeamsManager.load_seams_for_uv(obj)
        sync_uv_layers(obj)
        return {'FINISHED'}


class UV_OT_SetActiveLayerDirect(Operator):
    bl_idname = "uv.set_active_layer_direct"
    bl_label = "切换当前UV层"
    bl_description = "点击直接将所有选中物体的当前UV层切换到此层并自动恢复专属缝合边"
    bl_options = {'REGISTER', 'UNDO'}

    layer_index: IntProperty()

    def execute(self, context):
        target_index = self.layer_index
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not selected_objs and context.object and context.object.type == 'MESH':
            selected_objs = [context.object]

        count = 0
        for obj in selected_objs:
            uv_layers = obj.data.uv_layers
            if 0 <= target_index < len(uv_layers):
                UVSeamsManager.switch_layer_with_seams(obj, target_index)
                count += 1

        for area in context.screen.areas:
            if area.type in {'IMAGE_EDITOR', 'VIEW_3D'}:
                area.tag_redraw()

        return {'FINISHED'}


class UV_OT_SyncLayerSelection(Operator):
    bl_idname = "uv.sync_layer_selection_pro"
    bl_label = "同步选择UV层"
    bl_description = "将所有选中物体的当前UV层切换到此层并恢复专属缝合边"
    bl_options = {'REGISTER', 'UNDO'}

    layer_index: IntProperty()

    def execute(self, context):
        target_index = self.layer_index
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not selected_objs and context.object and context.object.type == 'MESH':
            selected_objs = [context.object]

        for obj in selected_objs:
            uv_layers = obj.data.uv_layers
            if 0 <= target_index < len(uv_layers):
                UVSeamsManager.switch_layer_with_seams(obj, target_index)

        for area in context.screen.areas:
            if area.type in {'IMAGE_EDITOR', 'VIEW_3D'}:
                area.tag_redraw()

        return {'FINISHED'}


class UV_OT_SaveCurrentSeams(Operator):
    """手动将当前视图网格中的缝合边保存并绑定到当前活动 UV 层"""
    bl_idname = "uv.save_current_seams_pro"
    bl_label = "保存当前UV缝合边"
    bl_description = "将当前模型标记的缝合边显式保存到当前活动UV层"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not selected_objs and context.object and context.object.type == 'MESH':
            selected_objs = [context.object]

        for obj in selected_objs:
            UVSeamsManager.save_seams_for_uv(obj)

        self.report({'INFO'}, f"已保存 {len(selected_objs)} 个物体的当前UV专属缝合边")
        return {'FINISHED'}


class UV_OT_SeamsFromIslands(Operator):
    """将当前活动 UV 层的 UV 岛边界自动转化为缝合边并保存"""
    bl_idname = "uv.seams_from_islands_pro"
    bl_label = "当前UV转缝合边"
    bl_description = "根据当前活动UV层的UV岛边界自动标记缝合边并绑定到当前层"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not selected_objs and context.object and context.object.type == 'MESH':
            selected_objs = [context.object]

        total_seams = 0
        for obj in selected_objs:
            total_seams += UVSeamsManager.generate_seams_from_uv(obj)

        for area in context.screen.areas:
            if area.type in {'IMAGE_EDITOR', 'VIEW_3D'}:
                area.tag_redraw()

        self.report({'INFO'}, f"成功根据当前UV生成并保存了 {total_seams} 条缝合边")
        return {'FINISHED'}


class UV_OT_ClearCurrentSeams(Operator):
    """清除当前活动 UV 层绑定的缝合边标记"""
    bl_idname = "uv.clear_current_seams_pro"
    bl_label = "清除当前缝合边"
    bl_description = "清除当前活动UV层绑定的全部缝合边"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not selected_objs and context.object and context.object.type == 'MESH':
            selected_objs = [context.object]

        for obj in selected_objs:
            UVSeamsManager.set_current_seams(obj, [])
            UVSeamsManager.save_seams_for_uv(obj)

        for area in context.screen.areas:
            if area.type in {'IMAGE_EDITOR', 'VIEW_3D'}:
                area.tag_redraw()

        self.report({'INFO'}, f"已清除 {len(selected_objs)} 个物体当前层的缝合边")
        return {'FINISHED'}


class UV_OT_SyncLayers(Operator):
    bl_idname = "uv.sync_layers_pro"
    bl_label = "同步UV层"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        sync_uv_layers(context.object)
        self.report({'INFO'}, f"已同步 {len(context.selected_objects)} 个物体")
        return {'FINISHED'}


class UV_OT_SelectMaxLayers(Operator):
    bl_idname = "uv.select_max_layers_pro"
    bl_label = "选择最大层物体"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        mesh_objs = [obj for obj in bpy.data.objects if obj.type == 'MESH']
        if not mesh_objs:
            self.report({'WARNING'}, "场景中没有网格物体")
            return {'CANCELLED'}

        max_layers = max(len(obj.data.uv_layers) for obj in mesh_objs)
        targets = [obj for obj in mesh_objs if len(obj.data.uv_layers) == max_layers]

        bpy.ops.object.select_all(action='DESELECT')
        for obj in targets:
            obj.select_set(True)

        if targets:
            context.view_layer.objects.active = targets[0]
            self.report({'INFO'}, f"选中 {len(targets)} 个{max_layers}层物体")
        return {'FINISHED'}


def sync_uv_layers(src_obj):
    if not src_obj or src_obj.type != 'MESH':
        return

    src_data = src_obj.data
    src_layers = src_data.uv_layers
    target_count = len(src_layers)
    src_active_idx = src_layers.active_index

    for obj in bpy.context.selected_objects:
        if obj == src_obj or obj.type != 'MESH':
            continue

        with bpy.context.temp_override(active_object=obj):
            dst_layers = obj.data.uv_layers

            while len(dst_layers) < target_count:
                dst_layers.new(name=src_layers[len(dst_layers)].name)

            while len(dst_layers) > target_count and len(dst_layers) > 1:
                dst_layers.remove(dst_layers[-1])

            for idx, dst_layer in enumerate(dst_layers):
                if idx < len(src_layers):
                    dst_layer.name = src_layers[idx].name

            if dst_layers:
                dst_layers.active_index = min(src_active_idx, len(dst_layers)-1)


class UV_UL_LayersList(UIList):
    bl_idname = "UV_UL_LayersListPro"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        is_active = (index == active_data.active_index)

        # 单选状态指示与切换
        op = row.operator(
            "uv.set_active_layer_direct",
            text="",
            icon='RADIOBUT_ON' if is_active else 'RADIOBUT_OFF',
            emboss=False
        )
        op.layer_index = index

        row.prop(item, "name", text="", emboss=False)

        # 渲染层相机图标
        render_icon = 'RESTRICT_RENDER_OFF' if item.active_render else 'RESTRICT_RENDER_ON'
        row.prop(item, "active_render", text="", icon=render_icon, emboss=False)


# =========================================================================
# UV复制粘贴功能
# =========================================================================

class UV_OT_CopyActiveLayer(Operator):
    bl_idname = "uv.copy_active_layer_pro"
    bl_label = "复制UV层"
    bl_description = "复制所有选中物体当前激活UV层的数据（若在编辑模式下有选中的面或UV块，则仅复制选中的部分）"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if not hasattr(context.scene, 'um_props'):
            self.report({'ERROR'}, "场景缺少um_props属性")
            return {'CANCELLED'}

        original_mode = context.mode

        # 获取所有选中的网格物体
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not selected_objs and context.object and context.object.type == 'MESH':
            selected_objs = [context.object]

        if not selected_objs:
            self.report({'ERROR'}, "请先选择至少一个网格物体")
            return {'CANCELLED'}

        copied_by_name = {}
        primary_data = None
        active_obj = context.view_layer.objects.active

        total_copied_faces = 0
        total_copied_objs = 0
        any_partial = False

        for obj in selected_objs:
            uv_layer = obj.data.uv_layers.active
            if not uv_layer:
                continue

            mesh = obj.data
            is_in_edit = (obj.mode == 'EDIT')
            
            if is_in_edit:
                bm = bmesh.from_edit_mesh(mesh)
            else:
                bm = bmesh.new()
                bm.from_mesh(mesh)

            bm.faces.ensure_lookup_table()
            uv_layer_bm = bm.loops.layers.uv.active
            if not uv_layer_bm:
                if not is_in_edit:
                    bm.free()
                continue

            # 检测是否有局部选中的面或选中的UV顶点/块
            selected_faces = []
            if is_in_edit:
                selected_faces = [f for f in bm.faces if f.select or any(l.uv_select_vert for l in f.loops)]

            is_partial = len(selected_faces) > 0 and len(selected_faces) < len(bm.faces)
            if is_partial:
                any_partial = True
                faces_to_record = selected_faces
            else:
                faces_to_record = list(bm.faces)

            face_data = {}
            for f in faces_to_record:
                face_data[f.index] = {
                    'uv': [tuple(l[uv_layer_bm].uv) for l in f.loops],
                    'pin_uv': [l[uv_layer_bm].pin_uv for l in f.loops]
                }

            all_loops = [loop for face in bm.faces for loop in face.loops]
            uv_data = [tuple(loop[uv_layer_bm].uv) for loop in all_loops]
            pin_uv = [loop[uv_layer_bm].pin_uv for loop in all_loops]

            if not is_in_edit:
                bm.free()

            obj_data_dict = {
                'obj_name': obj.name,
                'mesh_name': obj.data.name,
                'vertex_count': len(obj.data.vertices),
                'loop_count': len(all_loops),
                'face_count': len(faces_to_record),
                'is_partial': is_partial,
                'face_data': face_data,
                'uv': uv_data,
                'pin_uv': pin_uv
            }

            copied_by_name[obj.name] = obj_data_dict
            total_copied_objs += 1
            total_copied_faces += len(face_data)

            if obj == active_obj or primary_data is None:
                primary_data = obj_data_dict

        if total_copied_objs == 0:
            self.report({'WARNING'}, "选中的物体均无激活UV层")
            return {'CANCELLED'}

        payload = {
            'by_name': copied_by_name,
            'primary': primary_data,
            'is_partial': any_partial,
            'mesh_name': primary_data['mesh_name'],
            'vertex_count': primary_data['vertex_count'],
            'loop_count': primary_data['loop_count'],
            'uv': primary_data['uv'],
            'pin_uv': primary_data['pin_uv']
        }

        context.scene.um_props.copied_uv_data = json.dumps(payload)

        if any_partial:
            self.report({'INFO'}, f"已复制 {total_copied_objs} 个物体中选中的 {total_copied_faces} 个 UV 块/面数据")
        else:
            self.report({'INFO'}, f"已完整复制 {total_copied_objs} 个物体的当前UV层数据")
        return {'FINISHED'}


class UV_OT_PasteToActiveLayer(Operator):
    bl_idname = "uv.paste_to_active_layer_pro"
    bl_label = "粘贴UV层"
    bl_description = "将复制的UV数据粘贴到所有选中物体的当前激活UV层"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return hasattr(context.scene, 'um_props') and hasattr(context.scene.um_props, 'copied_uv_data') and bool(context.scene.um_props.copied_uv_data)

    def execute(self, context):
        original_mode = context.mode

        try:
            copied_json = context.scene.um_props.copied_uv_data
            if not copied_json:
                self.report({'ERROR'}, "无复制的UV数据")
                return {'CANCELLED'}

            payload = json.loads(copied_json)
            by_name = payload.get('by_name', {})
            primary = payload.get('primary') or payload

            selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']
            if not selected_objs and context.object and context.object.type == 'MESH':
                selected_objs = [context.object]

            if not selected_objs:
                self.report({'WARNING'}, "未选择网格物体")
                return {'CANCELLED'}

            active_obj = context.view_layer.objects.active
            processed = 0
            partial_pasted = False

            for obj in selected_objs:
                mesh = obj.data
                loop_cnt = len(mesh.loops)
                vert_cnt = len(mesh.vertices)

                # 1. 优先按物体名字匹配专属复制的 UV 数据
                data_to_use = by_name.get(obj.name)

                # 2. 若名字未匹配，尝试按 primary 匹配拓扑
                if not data_to_use and primary:
                    if primary.get('loop_count') == loop_cnt and primary.get('vertex_count') == vert_cnt:
                        data_to_use = primary

                # 3. 若仍未匹配，在 by_name 中寻找拓扑匹配的任意数据
                if not data_to_use and by_name:
                    for name, d in by_name.items():
                        if d.get('loop_count') == loop_cnt and d.get('vertex_count') == vert_cnt:
                            data_to_use = d
                            break

                if not data_to_use:
                    self.report({'WARNING'}, f"拓扑不匹配或未找到对应数据：{obj.name}")
                    continue

                is_in_edit = (obj.mode == 'EDIT')
                if is_in_edit:
                    bm = bmesh.from_edit_mesh(mesh)
                else:
                    bm = bmesh.new()
                    bm.from_mesh(mesh)

                bm.faces.ensure_lookup_table()
                uv_layer = bm.loops.layers.uv.active
                if not uv_layer:
                    uv_layer = bm.loops.layers.uv.new(obj.data.uv_layers.active.name if obj.data.uv_layers.active else "UVMap")

                # 如果是局部选中的 UV 块复制
                if data_to_use.get('is_partial') and 'face_data' in data_to_use:
                    partial_pasted = True
                    face_data = data_to_use['face_data']
                    for f_idx_str, f_info in face_data.items():
                        f_idx = int(f_idx_str)
                        if 0 <= f_idx < len(bm.faces):
                            face = bm.faces[f_idx]
                            uv_list = f_info.get('uv', [])
                            pin_list = f_info.get('pin_uv', [])
                            if len(face.loops) == len(uv_list):
                                for loop_i, (loop, uv_val) in enumerate(zip(face.loops, uv_list)):
                                    loop[uv_layer].uv = Vector(uv_val)
                                    if loop_i < len(pin_list):
                                        loop[uv_layer].pin_uv = pin_list[loop_i]
                else:
                    # 全层复制粘贴
                    loops = [loop for face in bm.faces for loop in face.loops]
                    if len(loops) == len(data_to_use['uv']):
                        for loop, uv_val, pin_val in zip(loops, data_to_use['uv'], data_to_use['pin_uv']):
                            loop[uv_layer].uv = Vector(uv_val)
                            loop[uv_layer].pin_uv = pin_val

                if is_in_edit:
                    bmesh.update_edit_mesh(mesh)
                else:
                    bm.to_mesh(mesh)
                    bm.free()
                    mesh.update()

                processed += 1

            for area in context.screen.areas:
                if area.type in {'IMAGE_EDITOR', 'VIEW_3D'}:
                    area.tag_redraw()

            if partial_pasted:
                self.report({'INFO'}, f"成功粘贴选中 UV 块到 {processed} 个物体的当前UV层")
            else:
                self.report({'INFO'}, f"成功粘贴到 {processed} 个物体的当前UV层")
            return {'FINISHED'}

        except Exception as e:
            self.report({'ERROR'}, f"粘贴失败: {str(e)}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}


# =========================================================================
# 注册与注销
# =========================================================================

classes = (
    UV_OT_AddLayer,
    UV_OT_RemoveLayer,
    UV_OT_SyncLayers,
    UV_OT_SelectMaxLayers,
    UV_OT_SetActiveLayerDirect,
    UV_OT_SyncLayerSelection,
    UV_UL_LayersList,
    UV_OT_CopyActiveLayer,
    UV_OT_PasteToActiveLayer,
    UV_OT_SaveCurrentSeams,
    UV_OT_SeamsFromIslands,
    UV_OT_ClearCurrentSeams,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    if uv_depsgraph_update_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(uv_depsgraph_update_handler)


def unregister():
    if uv_depsgraph_update_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(uv_depsgraph_update_handler)

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)