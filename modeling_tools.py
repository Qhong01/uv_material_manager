import os
import json
import math
import bpy
import bmesh
from collections import OrderedDict
import traceback
from bpy.types import Operator, PropertyGroup
from bpy.props import BoolProperty, StringProperty, PointerProperty, FloatProperty


# =========================================================================
# 0. 智能环选持久化配置与管理
# =========================================================================
SMART_LOOP_CONFIG_FILENAME = "smart_loop_config.json"
SMART_LOOP_DEFAULTS = {
    "max_angle": 60.0,
    "stop_at_seams": False,
    "stop_at_sharps": False,
    "stop_at_material_boundaries": False,
    "prioritize_sharp_loop": True,
}

def get_smart_loop_config_path():
    addon_dir = os.path.dirname(__file__)
    return os.path.join(addon_dir, SMART_LOOP_CONFIG_FILENAME)

def load_smart_loop_config():
    fp = get_smart_loop_config_path()
    if os.path.exists(fp):
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    res = dict(SMART_LOOP_DEFAULTS)
                    res.update(data)
                    return res
        except Exception:
            pass
    return dict(SMART_LOOP_DEFAULTS)

def save_smart_loop_config(cfg):
    fp = get_smart_loop_config_path()
    tmp_fp = fp + ".tmp"
    try:
        with open(tmp_fp, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(fp):
            os.replace(tmp_fp, fp)
        else:
            os.rename(tmp_fp, fp)
    except Exception:
        if os.path.exists(tmp_fp):
            try:
                os.remove(tmp_fp)
            except Exception:
                pass


def is_material_boundary(edge):
    faces = edge.link_faces
    if len(faces) == 2:
        return faces[0].material_index != faces[1].material_index
    return False

def find_topo_opposite(vert, incoming_edge):
    if len(vert.link_faces) == 4 and len(vert.link_edges) == 4:
        inc_faces = set(incoming_edge.link_faces)
        for e in vert.link_edges:
            if e != incoming_edge and not inc_faces.intersection(e.link_faces):
                return e
    return None

class MESH_OT_SmartLoopSelect(Operator):
    bl_idname = "mesh.smart_loop_select"
    bl_label = "智能环选"
    bl_description = "智能穿透循环选择边：遇到极点或三角面自动按几何走向穿透延伸；如果当前选中的是锐边，则优先选中整圈锐边特征"
    bl_options = {'REGISTER', 'UNDO'}

    max_angle: FloatProperty(
        name="最大偏角",
        description="遇到极点或三角面分叉时，允许向前穿透的最大偏折角度",
        default=math.radians(60.0),
        min=math.radians(10.0),
        max=math.radians(120.0),
        subtype='ANGLE',
        unit='ROTATION'
    )
    stop_at_seams: BoolProperty(
        name="遇到缝合边停止",
        description="遇到 UV 缝合边时停止继续穿透",
        default=False
    )
    stop_at_sharps: BoolProperty(
        name="遇到锐边停止",
        description="遇到标记为锐边的边时停止继续穿透（普通边模式生效）",
        default=False
    )
    stop_at_material_boundaries: BoolProperty(
        name="遇到材质边界停止",
        description="遇到不同材质分配的面之间的交界边时停止",
        default=False
    )
    prioritize_sharp_loop: BoolProperty(
        name="锐边特征优先",
        description="当选中的起始边本身为锐边时，优先追踪整圈锐边特征",
        default=True
    )

    @classmethod
    def poll(cls, context):
        return (context.mode == 'EDIT_MESH' and 
                context.active_object and 
                context.active_object.type == 'MESH')

    def invoke(self, context, event):
        cfg = load_smart_loop_config()
        self.max_angle = math.radians(cfg.get("max_angle", 60.0))
        self.stop_at_seams = cfg.get("stop_at_seams", False)
        self.stop_at_sharps = cfg.get("stop_at_sharps", False)
        self.stop_at_material_boundaries = cfg.get("stop_at_material_boundaries", False)
        self.prioritize_sharp_loop = cfg.get("prioritize_sharp_loop", True)
        return self.execute(context)

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.prop(self, "max_angle")
        col.prop(self, "prioritize_sharp_loop")
        
        box = layout.box()
        box.label(text="阻断条件开关:")
        box.prop(self, "stop_at_seams")
        box.prop(self, "stop_at_sharps")
        box.prop(self, "stop_at_material_boundaries")

    def execute(self, context):
        # 持久化当前设置
        cfg = {
            "max_angle": math.degrees(self.max_angle),
            "stop_at_seams": self.stop_at_seams,
            "stop_at_sharps": self.stop_at_sharps,
            "stop_at_material_boundaries": self.stop_at_material_boundaries,
            "prioritize_sharp_loop": self.prioritize_sharp_loop,
        }
        save_smart_loop_config(cfg)

        obj = context.active_object
        mesh = obj.data
        bm = bmesh.from_edit_mesh(mesh)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        # 确保处于边选择模式
        context.tool_settings.mesh_select_mode = (False, True, False)

        initial_selected_edges = [e for e in bm.edges if e.select]
        if not initial_selected_edges:
            self.report({'WARNING'}, "请先选择至少一条边")
            return {'CANCELLED'}

        max_angle_deg = math.degrees(self.max_angle)
        all_selected_edges = set(initial_selected_edges)

        for start_edge in initial_selected_edges:
            is_start_sharp = self.prioritize_sharp_loop and (not start_edge.smooth)

            for start_vert in start_edge.verts:
                curr_edge = start_edge
                curr_vert = start_vert
                prev_vert = curr_edge.other_vert(curr_vert)
                visited_branch_edges = {start_edge}

                while True:
                    # 检查到达的当前顶点是否碰到了阻断边界（例如缝合边、锐边、材质边界）
                    # 只要相连的边中有阻断标记，就表示遇到了交汇边界，在此停止穿透
                    if self.stop_at_seams and any(e.seam for e in curr_vert.link_edges if e != curr_edge):
                        break
                    if self.stop_at_sharps and (not is_start_sharp) and any((not e.smooth) for e in curr_vert.link_edges if e != curr_edge):
                        break
                    if self.stop_at_material_boundaries:
                        mat_faces = curr_vert.link_faces
                        if len(mat_faces) > 1 and len({f.material_index for f in mat_faces}) > 1:
                            break
                        if any(is_material_boundary(e) for e in curr_vert.link_edges if e != curr_edge):
                            break

                    vec_in = curr_vert.co - prev_vert.co
                    if vec_in.length_squared < 1e-8:
                        break
                    vec_in.normalize()

                    candidates = [e for e in curr_vert.link_edges if e != curr_edge and e not in visited_branch_edges]
                    if not candidates:
                        break

                    # 1. 锐边特征环优先追踪模式 (当起点为锐边)
                    if is_start_sharp:
                        sharp_cands = [e for e in candidates if (not e.smooth)]
                        valid_sharp = []
                        for e in sharp_cands:
                            if self.stop_at_seams and e.seam:
                                continue
                            if self.stop_at_material_boundaries and is_material_boundary(e):
                                continue

                            other_v = e.other_vert(curr_vert)
                            vec_out = other_v.co - curr_vert.co
                            if vec_out.length_squared < 1e-8:
                                continue
                            vec_out.normalize()
                            dot = max(-1.0, min(1.0, vec_in.dot(vec_out)))
                            valid_sharp.append((dot, e, other_v))

                        if not valid_sharp:
                            break

                        valid_sharp.sort(key=lambda x: x[0], reverse=True)
                        chosen_edge = valid_sharp[0][1]
                        chosen_other_v = valid_sharp[0][2]

                        if chosen_edge in all_selected_edges:
                            all_selected_edges.add(chosen_edge)
                            break

                        all_selected_edges.add(chosen_edge)
                        visited_branch_edges.add(chosen_edge)
                        prev_vert = curr_vert
                        curr_vert = chosen_other_v
                        curr_edge = chosen_edge
                        continue

                    # 2. 普通表面边：拓扑扇区排除 + 曲面切线投影几何平滑穿透模式
                    # 拓扑扇区排除：当顶点具有 >= 4 条边时，排除与进向边共面的侧翼边（90°侧转边），只在对向扇区中选边
                    if len(curr_vert.link_edges) >= 4:
                        inc_faces = set(curr_edge.link_faces)
                        opposite_candidates = [e for e in candidates if not inc_faces.intersection(e.link_faces)]
                        if opposite_candidates:
                            candidates = opposite_candidates

                    # 计算顶点法线与局部曲面切平面基底
                    vert_norm = curr_vert.normal
                    t_in = vec_in - vec_in.dot(vert_norm) * vert_norm
                    if t_in.length_squared > 1e-6:
                        t_in.normalize()
                    else:
                        t_in = vec_in

                    topo_opposite = find_topo_opposite(curr_vert, curr_edge)

                    valid_candidates = []
                    for e in candidates:
                        if self.stop_at_seams and e.seam:
                            continue
                        if self.stop_at_sharps and (not e.smooth):
                            continue
                        if self.stop_at_material_boundaries and is_material_boundary(e):
                            continue

                        other_v = e.other_vert(curr_vert)
                        vec_out = other_v.co - curr_vert.co
                        if vec_out.length_squared < 1e-8:
                            continue
                        vec_out.normalize()

                        # 切平面切向点积与 3D 点积加权计算
                        t_out = vec_out - vec_out.dot(vert_norm) * vert_norm
                        if t_out.length_squared > 1e-6:
                            t_out.normalize()
                        else:
                            t_out = vec_out

                        dot_tangent = max(-1.0, min(1.0, t_in.dot(t_out)))
                        dot_3d = max(-1.0, min(1.0, vec_in.dot(vec_out)))

                        # 综合得分：以曲面切线流向为主(70%)，3D空间为辅(30%)
                        score = dot_tangent * 0.7 + dot_3d * 0.3
                        angle_deg = math.degrees(math.acos(dot_3d))

                        if angle_deg <= max_angle_deg or dot_tangent > 0.5:
                            valid_candidates.append((score, e, other_v, angle_deg, dot_tangent))

                    if not valid_candidates:
                        break

                    # 排序
                    valid_candidates.sort(key=lambda x: x[0], reverse=True)

                    chosen_edge = valid_candidates[0][1]
                    chosen_other_v = valid_candidates[0][2]

                    # 如果标准四边形对边在有效候选列表中且表现优良，优先使用标准对边
                    if topo_opposite:
                        for s, e, ov, adeg, dtan in valid_candidates:
                            if e == topo_opposite and (adeg <= max_angle_deg or dtan > 0.4):
                                chosen_edge = e
                                chosen_other_v = ov
                                break

                    if chosen_edge in all_selected_edges:
                        all_selected_edges.add(chosen_edge)
                        break

                    all_selected_edges.add(chosen_edge)
                    visited_branch_edges.add(chosen_edge)
                    prev_vert = curr_vert
                    curr_vert = chosen_other_v
                    curr_edge = chosen_edge

        for e in all_selected_edges:
            e.select = True

        bmesh.update_edit_mesh(mesh)
        context.view_layer.update()
        self.report({'INFO'}, f"智能环选已选中 {len(all_selected_edges)} 条边")
        return {'FINISHED'}


# =========================================================================
# 1. 大纲视图状态管理
# =========================================================================
class OutlinerState(PropertyGroup):
    is_open: BoolProperty(name="大纲视图状态", default=False)
    area_ptr1: StringProperty(name="属性区域指针", default="")
    area_ptr2: StringProperty(name="大纲区域指针", default="")
    is_right: BoolProperty(name="是否在右侧", default=False)


# =========================================================================
# 2. 拍平面工具 (默认且始终激活自动模式，使用活动面)
# =========================================================================
class FLATTEN_FACE_OT_operator(Operator):
    bl_idname = "mesh.flatten_face_by_three_vertices"
    bl_label = "拍平面"
    bl_description = "以活动面为基准参考平面，将当前选中的所有面拍平到该平面"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'EDIT_MESH' and 
                context.active_object and 
                context.active_object.type == 'MESH')

    def execute(self, context):
        obj = context.active_object
        mesh = obj.data
        bm = bmesh.from_edit_mesh(mesh)
        selected_faces = [f for f in bm.faces if f.select]

        if not selected_faces:
            self.report({'WARNING'}, "未选择任何面")
            return {'CANCELLED'}

        # 默认使用活动面作为参考基准面
        active_face = bm.faces.active
        if not active_face or not active_face.select:
            active_face = selected_faces[0]

        if len(active_face.verts) < 3:
            self.report({'ERROR'}, "活动面至少需要3个顶点以定义参考平面")
            return {'CANCELLED'}

        ref_verts = active_face.verts[:3]
        ref_points_co = [v.co.copy() for v in ref_verts]
        vec1 = ref_points_co[1] - ref_points_co[0]
        vec2 = ref_points_co[2] - ref_points_co[0]
        plane_normal = vec1.cross(vec2)

        if plane_normal.length_squared < 1e-6:
            self.report({'ERROR'}, "参考点共线，无法定义平面")
            return {'CANCELLED'}

        plane_normal.normalize()
        plane_point = ref_points_co[0]

        def project_to_plane(point):
            vec_to_point = point - plane_point
            distance = vec_to_point.dot(plane_normal)
            return point - distance * plane_normal

        moved_verts = 0
        ref_vert_set = set(ref_verts)
        for face in selected_faces:
            for vert in face.verts:
                if vert not in ref_vert_set:
                    vert.co = project_to_plane(vert.co)
                    moved_verts += 1

        bmesh.update_edit_mesh(mesh)
        context.view_layer.update()
        self.report({'INFO'}, f"成功拍平 {len(selected_faces)} 个面 ({moved_verts} 个顶点已对齐)")
        return {'FINISHED'}


# =========================================================================
# 3. 填充孔洞工具
# =========================================================================
class HOLE_FILL_OT_operator(Operator):
    bl_idname = "mesh.zhineng_fill_holes"
    bl_label = "填充孔洞"
    bl_description = "自动检测网格破洞边界并进行填充"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.active_object and 
                context.active_object.type == 'MESH')

    def execute(self, context):
        try:
            obj = context.active_object
            was_in_edit_mode = False
            if context.mode == 'EDIT_MESH':
                was_in_edit_mode = True
                bpy.ops.object.mode_set(mode='OBJECT')

            mesh = obj.data
            bm = bmesh.new()
            bm.from_mesh(mesh)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()

            boundary_edges = [e for e in bm.edges if e.is_boundary]

            if not boundary_edges:
                self.report({'INFO'}, "没有发现孔洞")
                bm.free()
                if was_in_edit_mode:
                    bpy.ops.object.mode_set(mode='EDIT')
                return {'CANCELLED'}

            visited = set()
            boundary_loops = []

            for edge in boundary_edges:
                if edge in visited:
                    continue

                loop = []
                queue = [edge]
                visited.add(edge)

                while queue:
                    e = queue.pop()
                    loop.append(e)

                    for v in e.verts:
                        for linked_edge in v.link_edges:
                            if (linked_edge.is_boundary and 
                                linked_edge not in visited and 
                                linked_edge != e):
                                visited.add(linked_edge)
                                queue.append(linked_edge)

                boundary_loops.append(loop)

            filled_count = 0
            for loop in boundary_loops:
                if len(loop) < 3:
                    continue

                try:
                    fill_result = bmesh.ops.holes_fill(bm, edges=loop)
                    if 'faces' in fill_result and fill_result['faces']:
                        filled_count += 1
                except Exception:
                    try:
                        verts = []
                        for edge in loop:
                            for v in edge.verts:
                                if v not in verts:
                                    verts.append(v)

                        if len(verts) >= 3:
                            bm.faces.new(verts)
                            filled_count += 1
                    except Exception:
                        continue

            bm.to_mesh(mesh)
            mesh.update()
            bm.free()

            if was_in_edit_mode:
                bpy.ops.object.mode_set(mode='EDIT')

            context.view_layer.update()

            if filled_count > 0:
                self.report({'INFO'}, f"成功填充 {filled_count} 个孔洞")
                return {'FINISHED'}
            else:
                self.report({'WARNING'}, "未能填充任何孔洞")
                return {'CANCELLED'}

        except Exception as e:
            self.report({'ERROR'}, f"填充孔洞失败: {str(e)}")
            if 'bm' in locals():
                bm.free()
            if was_in_edit_mode:
                bpy.ops.object.mode_set(mode='EDIT')
            return {'CANCELLED'}


# =========================================================================
# 4. 合并材质工具
# =========================================================================
class MATERIAL_OT_UltraCombine(Operator):
    bl_idname = "object.ultra_material_combine"
    bl_label = "合并材质"
    bl_description = "智能遍历选中网格物体，重新映射材质槽并合并创建主材质节点树"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            global_materials = self.collect_valid_materials(context)
            if not global_materials:
                self.report({'ERROR'}, "未发现有效材质（需包含节点树）")
                return {'CANCELLED'}

            self.remap_material_slots(context, global_materials)
            master_mat = self.create_master_material(global_materials)

            if master_mat:
                self.report({'INFO'}, f"主材质创建成功: {master_mat.name}")
            else:
                self.report({'WARNING'}, "创建了空主材质（无有效节点）")

            return {'FINISHED'}

        except Exception as e:
            self.report({'ERROR'}, f"严重错误: {str(e)}")
            print(f"错误详情:\n{traceback.format_exc()}")
            return {'CANCELLED'}

    def collect_valid_materials(self, context):
        valid_materials = OrderedDict()
        for obj in context.selected_objects:
            if obj.type == 'MESH' and hasattr(obj, 'material_slots'):
                for slot in obj.material_slots:
                    if slot and (mat := slot.material):
                        if self.is_valid_material(mat):
                            mat_id = f"{mat.name}|{hash(tuple(mat.diffuse_color))}"
                            valid_materials[mat_id] = mat
        return list(valid_materials.values())

    def remap_material_slots(self, context, material_list):
        for obj in context.selected_objects:
            if obj.type == 'MESH':
                face_materials = []
                for poly in obj.data.polygons:
                    if poly.material_index < len(obj.material_slots):
                        slot = obj.material_slots[poly.material_index]
                        face_materials.append(slot.material if slot else None)
                    else:
                        face_materials.append(None)

                obj.data.materials.clear()
                valid_materials = [m for m in material_list if m]
                for mat in valid_materials:
                    obj.data.materials.append(mat)

                for poly, mat in zip(obj.data.polygons, face_materials):
                    try:
                        if mat in valid_materials:
                            poly.material_index = valid_materials.index(mat)
                        else:
                            poly.material_index = 0
                    except Exception:
                        poly.material_index = 0

    def create_master_material(self, materials):
        try:
            master_mat = bpy.data.materials.new(name="Master_Material")
            master_mat.use_nodes = True
            nodes = master_mat.node_tree.nodes
            links = master_mat.node_tree.links
            nodes.clear()

            output_node = nodes.new('ShaderNodeOutputMaterial')
            output_node.location = (0, 0)

            valid_materials = [m for m in materials if m and m.node_tree and len(m.node_tree.nodes) > 0]
            prev_node = None

            for idx, mat in enumerate(valid_materials):
                try:
                    mat_node = nodes.new('ShaderNodeGroup')
                    mat_node.node_tree = mat.node_tree.copy()
                    mat_node.name = f"{mat.name}_Group"
                    mat_node.location = (idx * 400, 300)

                    if idx == 0:
                        links.new(mat_node.outputs[0], output_node.inputs['Surface'])
                        prev_node = mat_node
                    else:
                        mix_node = nodes.new('ShaderNodeMixShader')
                        mix_node.location = (idx * 400 - 200, 0)
                        links.new(prev_node.outputs[0], mix_node.inputs[1])
                        links.new(mat_node.outputs[0], mix_node.inputs[2])
                        links.new(mix_node.outputs[0], output_node.inputs['Surface'])
                        prev_node = mix_node
                except Exception:
                    continue

            return master_mat if valid_materials else None
        except Exception:
            if 'master_mat' in locals() and master_mat:
                bpy.data.materials.remove(master_mat)
            return None

    @staticmethod
    def is_valid_material(mat):
        return (mat is not None and
                hasattr(mat, 'node_tree') and
                mat.node_tree is not None and
                len(mat.node_tree.nodes) > 0)


# =========================================================================
# 5. 大纲视图切换工具
# =========================================================================
class OUTLINER_OT_ToggleOperator(Operator):
    bl_idname = "view3d.toggle_outliner"
    bl_label = "切换大纲视图"
    bl_description = "切换大纲视图与属性视图的显示与隐藏"

    @classmethod
    def poll(cls, context):
        return context.area and context.area.type == 'VIEW_3D'

    def execute(self, context):
        scene = context.scene
        outliner_state = scene.outliner_state
        win = context.window

        if not win or not win.screen:
            self.report({'ERROR'}, "无法获取屏幕上下文")
            return {'CANCELLED'}

        def detect_existing_outliner_properties_pair():
            areas = win.screen.areas
            area_bounds = []
            for area in areas:
                area_bounds.append({
                    'area': area,
                    'x': area.x,
                    'y': area.y,
                    'width': area.width,
                    'height': area.height,
                    'right': area.x + area.width,
                    'bottom': area.y + area.height
                })

            for i, a1 in enumerate(area_bounds):
                for j, a2 in enumerate(area_bounds):
                    if i == j:
                        continue
                    same_column = (abs(a1['x'] - a2['x']) <= 1 and abs(a1['right'] - a2['right']) <= 1)
                    adjacent = False
                    if abs(a1['bottom'] - a2['y']) <= 1:
                        adjacent = True
                        upper = a1
                        lower = a2
                    elif abs(a2['bottom'] - a1['y']) <= 1:
                        adjacent = True
                        upper = a2
                        lower = a1

                    if same_column and adjacent:
                        if (upper['area'].type == 'OUTLINER' and lower['area'].type == 'PROPERTIES') or                            (upper['area'].type == 'PROPERTIES' and lower['area'].type == 'OUTLINER'):
                            return (upper['area'], lower['area'])

            view3d_right = 0
            for area in areas:
                if area.type == 'VIEW_3D':
                    view3d_right = max(view3d_right, area.x + area.width)

            outliner_areas = [a for a in areas if a.type == 'OUTLINER' and a.x > view3d_right]
            properties_areas = [a for a in areas if a.type == 'PROPERTIES' and a.x > view3d_right]

            if outliner_areas and properties_areas:
                return (outliner_areas[0], properties_areas[0])

            for outliner_area in [a for a in areas if a.type == 'OUTLINER']:
                for properties_area in [a for a in areas if a.type == 'PROPERTIES']:
                    x_close = abs((outliner_area.x + outliner_area.width/2) - 
                                 (properties_area.x + properties_area.width/2)) < 200
                    outliner_range = (outliner_area.y, outliner_area.y + outliner_area.height)
                    properties_range = (properties_area.y, properties_area.y + properties_area.height)
                    overlap = (outliner_range[0] <= properties_range[1] + 1 and 
                              properties_range[0] <= outliner_range[1] + 1)

                    if x_close and overlap:
                        if outliner_area.y > properties_area.y:
                            return (outliner_area, properties_area)
                        else:
                            return (properties_area, outliner_area)

            return None

        existing_pair = detect_existing_outliner_properties_pair()

        if existing_pair or outliner_state.is_open:
            areas_to_close = []
            if existing_pair:
                areas_to_close = list(existing_pair)
            else:
                for area in win.screen.areas:
                    ptr = hex(area.as_pointer())
                    if ptr in (outliner_state.area_ptr1, outliner_state.area_ptr2):
                        areas_to_close.append(area)

            for area in areas_to_close:
                try:
                    with context.temp_override(window=win, screen=win.screen, area=area):
                        bpy.ops.screen.area_close()
                except Exception as e:
                    self.report({'WARNING'}, f"关闭区域失败: {str(e)}")

            outliner_state.is_open = False
            outliner_state.area_ptr1 = ""
            outliner_state.area_ptr2 = ""
            self.report({'INFO'}, "大纲视图已关闭")
            return {'FINISHED'}

        view3d_area = next((a for a in win.screen.areas if a.type == 'VIEW_3D'), None)
        if not view3d_area:
            self.report({'ERROR'}, "未找到 3D 视图")
            return {'CANCELLED'}

        with context.temp_override(window=win, screen=win.screen, area=view3d_area):
            bpy.ops.screen.area_split(direction='VERTICAL', factor=0.85)

        new_area = win.screen.areas[-1]
        new_area.type = 'PROPERTIES'

        with context.temp_override(window=win, screen=win.screen, area=new_area):
            bpy.ops.screen.area_split(direction='HORIZONTAL', factor=0.7)
            properties_area = context.area if context.area else new_area
            outliner_area = win.screen.areas[-1]
            outliner_area.type = 'OUTLINER'

            for space in outliner_area.spaces:
                if space.type == 'OUTLINER':
                    space.display_mode = 'VIEW_LAYER'
                    space.show_restrict_column_select = True

        outliner_state.is_open = True
        outliner_state.area_ptr1 = hex(properties_area.as_pointer())
        outliner_state.area_ptr2 = hex(outliner_area.as_pointer())
        outliner_state.is_right = True

        self.report({'INFO'}, "大纲视图已打开")
        return {'FINISHED'}


classes = (
    OutlinerState,
    FLATTEN_FACE_OT_operator,
    HOLE_FILL_OT_operator,
    MATERIAL_OT_UltraCombine,
    OUTLINER_OT_ToggleOperator,
    MESH_OT_SmartLoopSelect,
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

def safe_unregister_class(cls):
    try:
        bpy.utils.unregister_class(cls)
    except Exception:
        pass

def register():
    for cls in classes:
        safe_register_class(cls)
    bpy.types.Scene.outliner_state = PointerProperty(type=OutlinerState)

def unregister():
    if hasattr(bpy.types.Scene, 'outliner_state'):
        del bpy.types.Scene.outliner_state
    for cls in reversed(classes):
        safe_unregister_class(cls)