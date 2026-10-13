import os
import json
import math
import mathutils
import bpy
import bmesh
import bpy_extras
from collections import OrderedDict
import traceback
from bpy.types import Operator, PropertyGroup
from bpy.props import BoolProperty, StringProperty, PointerProperty, FloatProperty, FloatVectorProperty, EnumProperty


# =========================================================================
# =========================================================================
# 0. 智能环选：纯直线 + 可选特征折线追踪 (Straight Line / Crease Loop)
# =========================================================================
SMART_LOOP_CONFIG_FILENAME = "smart_loop_config.json"
SMART_LOOP_DEFAULTS = {
    "max_angle": 60.0,
    "follow_feature_creases": True,
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


def _is_feature_edge(e: bmesh.types.BMEdge) -> bool:
    """判断一条边是否属于特征边（标记为锐边非 smooth，或物理开放边界）。"""
    return (not e.smooth) or (len(e.link_faces) == 1)


def _choose_next_edge(curr_vert: bmesh.types.BMVert,
                      curr_edge: bmesh.types.BMEdge,
                      prev_vert: bmesh.types.BMVert,
                      visited_edges: set,
                      max_angle_rad: float,
                      follow_feature_creases: bool) -> bmesh.types.BMEdge | None:
    """
    在当前顶点选择下一条前进边：
    - 若开启 follow_feature_creases 且当前边为锐边/特征边：
      只沿着平滑过渡（转向偏角 < 90°，即 dot > 0.0）的特征边继续追踪；遇到 >= 90° 的直角转折坚决不拐弯，在转折点立刻停止。
    - 否则：保持纯几何直线延伸（在 max_angle_rad 范围内找最直的边）。
    """
    candidates = [e for e in curr_vert.link_edges if e is not curr_edge and e not in visited_edges]
    if not candidates:
        return None

    vec_in = curr_vert.co - prev_vert.co
    if vec_in.length_squared < 1e-8:
        return candidates[0]
    vec_in = vec_in.normalized()

    # 1. 锐边/特征折线追踪模式 (遇到 >= 90° 折角坚决不拐弯，立刻停止)
    if follow_feature_creases and _is_feature_edge(curr_edge):
        feat_candidates = [e for e in candidates if _is_feature_edge(e)]
        if not feat_candidates:
            return None

        # 过滤掉所有偏转角 >= 90° 的大折角分支 (即 vec_in · vec_out <= 0.0)
        smooth_feat_candidates = []
        for e in feat_candidates:
            other_v = e.other_vert(curr_vert)
            vec_out = other_v.co - curr_vert.co
            length = vec_out.length
            if length < 1e-8:
                continue
            vec_out_n = vec_out / length
            dot_turn = vec_in.dot(vec_out_n)

            # 偏转角 < 90° (dot > 0.0) 才视为平滑弧线继续通行；>= 90° (dot <= 0.0) 直角拐弯直接截断
            if dot_turn > 1e-4:
                smooth_feat_candidates.append((dot_turn, e, other_v, length))

        if not smooth_feat_candidates:
            # 所有特征候选均为 >= 90° 的大折角，在转折顶点原地停住
            return None

        if len(smooth_feat_candidates) == 1:
            return smooth_feat_candidates[0][1]

        # 多个平滑特征分支时：按共壁连续性与方向平顺度综合打分
        inc_faces = set(curr_edge.link_faces)
        best_e = None
        best_score = -float('inf')

        for dot_turn, e, other_v, length in smooth_feat_candidates:
            has_shared_face = bool(inc_faces.intersection(e.link_faces))
            shared_score = 1.5 if has_shared_face else 0.0
            total_score = dot_turn * 1.0 + shared_score

            if total_score > best_score:
                best_score = total_score
                best_e = e

        return best_e

    # 2. 纯直线延伸逻辑
    best_edge = None
    best_dot = -1.0
    min_cos = math.cos(max_angle_rad)

    for e in candidates:
        other_v = e.other_vert(curr_vert)
        vec_out = other_v.co - curr_vert.co
        if vec_out.length_squared < 1e-8:
            continue
        vec_out = vec_out.normalized()

        dot = vec_in.dot(vec_out)
        if dot > best_dot:
            best_dot = dot
            best_edge = e

    if best_edge and best_dot >= min_cos:
        return best_edge
    return None


class MESH_OT_SmartLoopSelect(Operator):
    bl_idname = "mesh.smart_loop_select"
    bl_label = "智能环选"
    bl_description = "智能环选：沿最直方向延伸；勾选「追踪特征折线」时优先沿锐边/台阶折角连贯选择"
    bl_options = {'REGISTER', 'UNDO'}

    max_angle: FloatProperty(
        name="最大偏角",
        description="直线模式下允许偏离直线的最大角度阈值",
        default=math.radians(60.0),
        min=math.radians(5.0),
        max=math.radians(120.0),
        subtype='ANGLE',
        unit='ROTATION'
    )
    follow_feature_creases: BoolProperty(
        name="追踪特征折线",
        description="勾选时优先追踪锐边或折角特征线（支持直角/台阶拐弯）；未勾选时保持纯直线延伸",
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
        self.follow_feature_creases = cfg.get("follow_feature_creases", True)
        return self.execute(context)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "max_angle")
        layout.prop(self, "follow_feature_creases")

    def execute(self, context):
        cfg = {
            "max_angle": math.degrees(self.max_angle),
            "follow_feature_creases": self.follow_feature_creases,
        }
        save_smart_loop_config(cfg)

        obj = context.active_object
        mesh = obj.data
        bm = bmesh.from_edit_mesh(mesh)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()

        context.tool_settings.mesh_select_mode = (False, True, False)

        initial_selected_edges = [e for e in bm.edges if e.select]
        if not initial_selected_edges:
            self.report({'WARNING'}, "请先选择至少一条边")
            return {'CANCELLED'}

        # 获取主要起始边
        active_edge = None
        if bm.select_history.active and isinstance(bm.select_history.active, bmesh.types.BMEdge) and bm.select_history.active.select:
            active_edge = bm.select_history.active
        else:
            active_edge = initial_selected_edges[0]

        start_edges = [active_edge] if len(initial_selected_edges) == 1 else initial_selected_edges
        all_selected_edges = set(start_edges)

        for start_edge in start_edges:
            loop_is_closed = False

            # 从起始边的两个端点分别延伸
            for start_vert in start_edge.verts:
                if loop_is_closed:
                    break

                curr_edge = start_edge
                curr_vert = start_vert
                prev_vert = curr_edge.other_vert(curr_vert)
                visited_branch = {start_edge}

                while True:
                    chosen_edge = _choose_next_edge(
                        curr_vert, curr_edge, prev_vert, visited_branch,
                        self.max_angle, self.follow_feature_creases
                    )

                    if not chosen_edge:
                        break

                    chosen_other_v = chosen_edge.other_vert(curr_vert)

                    # 闭环即停：首尾闭合立刻终止
                    if len(visited_branch) >= 3 and (chosen_edge in all_selected_edges or chosen_other_v in start_edge.verts):
                        all_selected_edges.add(chosen_edge)
                        loop_is_closed = True
                        break

                    all_selected_edges.add(chosen_edge)
                    visited_branch.add(chosen_edge)
                    prev_vert = curr_vert
                    curr_vert = chosen_other_v
                    curr_edge = chosen_edge

        # 清除旧选区并应用新闭环
        for e in bm.edges:
            e.select = (e in all_selected_edges)

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


# =========================================================================
# 6. 90度视口自适应世界轴吸附旋转操作符与饼菜单
# =========================================================================
def get_view_aligned_world_rotation(rv3d, direction, angle_deg=90.0):
    """
    根据当前 3D 视口相机朝向，计算吸附到世界坐标轴（World X/Y/Z）的严格 90° 旋转轴与旋转量。
    保证旋转永远与世界网格正交对齐，绝不歪斜。
    """
    rad = math.radians(abs(angle_deg))
    if not rv3d:
        if direction == 'LEFT': return 'Z', -rad, (False, False, True)
        elif direction == 'RIGHT': return 'Z', rad, (False, False, True)
        elif direction == 'UP': return 'X', -rad, (True, False, False)
        elif direction == 'DOWN': return 'X', rad, (True, False, False)

    view_rot = rv3d.view_rotation
    v_right = view_rot @ mathutils.Vector((1.0, 0.0, 0.0))
    v_up = view_rot @ mathutils.Vector((0.0, 1.0, 0.0))
    v_forward = view_rot @ mathutils.Vector((0.0, 0.0, -1.0))

    if direction in {'LEFT', 'RIGHT'}:
        # 左右转向：严格绕世界竖直 Z 轴 (0, 0, 1) 旋转，模型保持直立在地平面
        is_upright = (v_up.z >= 0)
        sign = 1.0 if is_upright else -1.0
        
        # 左右操作对调
        if direction == 'LEFT':
            val = -rad * sign
        else:
            val = rad * sign
        return 'Z', val, (False, False, True)

    elif direction in {'UP', 'DOWN'}:
        # 上下翻转：吸附到当前视口横向最接近的世界水平轴 (World X 或 World Y)
        if abs(v_forward.z) > 0.95:
            # 顶视/底视特殊情况
            axis = 'X'
            constraint = (True, False, False)
            sign = 1.0 if v_right.x >= 0 else -1.0
        else:
            abs_x = abs(v_right.x)
            abs_y = abs(v_right.y)
            if abs_x >= abs_y:
                axis = 'X'
                constraint = (True, False, False)
                sign = 1.0 if v_right.x >= 0 else -1.0
            else:
                axis = 'Y'
                constraint = (False, True, False)
                sign = 1.0 if v_right.y >= 0 else -1.0

        # 上下操作对调
        if direction == 'UP':
            val = -rad * sign
        else:
            val = rad * sign

        return axis, val, constraint

    elif direction in {'CW', 'CCW'}:
        # 顺时针/逆时针平面旋转：吸附到当前视口视线方向最平行的世界轴 (World X, Y 或 Z)
        abs_x = abs(v_forward.x)
        abs_y = abs(v_forward.y)
        abs_z = abs(v_forward.z)

        if abs_z >= abs_x and abs_z >= abs_y:
            axis = 'Z'
            constraint = (False, False, True)
            sign = 1.0 if v_forward.z >= 0 else -1.0
        elif abs_y >= abs_x and abs_y >= abs_z:
            axis = 'Y'
            constraint = (False, True, False)
            sign = 1.0 if v_forward.y >= 0 else -1.0
        else:
            axis = 'X'
            constraint = (True, False, False)
            sign = 1.0 if v_forward.x >= 0 else -1.0

        if direction == 'CW':
            val = rad * sign
        else:
            val = -rad * sign

        return axis, val, constraint


class OBJECT_OT_Rotate90Degrees(Operator):
    bl_idname = "object.rotate_90_degrees"
    bl_label = "90° 视口旋转"
    bl_description = "根据当前 3D 视口方向吸附世界坐标轴旋转 90 度（支持物体模式与编辑模式）"
    bl_options = {'REGISTER', 'UNDO'}

    direction: EnumProperty(
        name="方向",
        items=[
            ('LEFT', "向左", "以当前视口向左旋转 90°", 'TRIA_LEFT', 0),
            ('RIGHT', "向右", "以当前视口向右旋转 90°", 'TRIA_RIGHT', 1),
            ('UP', "向上", "以当前视口向上翻转 90°", 'TRIA_UP', 2),
            ('DOWN', "向下", "以当前视口向下翻转 90°", 'TRIA_DOWN', 3),
            ('CW', "顺时针", "以当前视口顺时针旋转 90°", 'FILE_REFRESH', 4),
            ('CCW', "逆时针", "以当前视口逆时针旋转 90°", 'LOOP_BACK', 5),
        ],
        default='LEFT'
    )

    angle: FloatProperty(
        name="旋转角度",
        default=90.0,
        description="旋转角度（度）"
    )

    def execute(self, context):
        rv3d = getattr(context, 'region_data', None)
        win_region = None

        if context.area and context.area.type == 'VIEW_3D':
            for r in context.area.regions:
                if r.type == 'WINDOW':
                    win_region = r
                    break
            if not rv3d:
                for space in context.area.spaces:
                    if space.type == 'VIEW_3D':
                        rv3d = space.region_3d
                        break

        if not rv3d and context.screen:
            for a in context.screen.areas:
                if a.type == 'VIEW_3D':
                    for space in a.spaces:
                        if space.type == 'VIEW_3D' and space.region_3d:
                            rv3d = space.region_3d
                            break
                    if rv3d:
                        break

        axis, val, constraint = get_view_aligned_world_rotation(rv3d, self.direction, self.angle)

        try:
            if win_region:
                with context.temp_override(region=win_region):
                    bpy.ops.transform.rotate(
                        value=val,
                        orient_axis=axis,
                        orient_type='GLOBAL',
                        constraint_axis=constraint
                    )
            else:
                bpy.ops.transform.rotate(
                    value=val,
                    orient_axis=axis,
                    orient_type='GLOBAL',
                    constraint_axis=constraint
                )
        except Exception as e:
            self.report({'WARNING'}, f"视口旋转失败: {e}")
            return {'CANCELLED'}

        return {'FINISHED'}


class PIE_MT_Rotate90Pie(bpy.types.Menu):
    bl_idname = "PIE_MT_rotate_90_pie"
    bl_label = "90° 视口旋转"

    def draw(self, context):
        pie = self.layout.menu_pie()
        # 1. 正左 (West): 向左转 90°
        pie.operator("object.rotate_90_degrees", text="向左 90°", icon='TRIA_LEFT').direction = 'LEFT'
        # 2. 正右 (East): 向右转 90°
        pie.operator("object.rotate_90_degrees", text="向右 90°", icon='TRIA_RIGHT').direction = 'RIGHT'
        # 3. 正下 (South): 向下翻 90°
        pie.operator("object.rotate_90_degrees", text="向下 90°", icon='TRIA_DOWN').direction = 'DOWN'
        # 4. 正上 (North): 向上翻 90°
        pie.operator("object.rotate_90_degrees", text="向上 90°", icon='TRIA_UP').direction = 'UP'
        # 5. 左上 (North-West): 逆时针 90°
        pie.operator("object.rotate_90_degrees", text="逆时针 90°", icon='LOOP_BACK').direction = 'CCW'
        # 6. 右上 (North-East): 顺时针 90°
        pie.operator("object.rotate_90_degrees", text="顺时针 90°", icon='FILE_REFRESH').direction = 'CW'


class VIEW3D_OT_OpenRotate90Menu(Operator):
    bl_idname = "view3d.open_rotate_90_menu"
    bl_label = "90°旋转"
    bl_description = "呼出 90° 视口自适应快速旋转菜单（支持右键直接分配快捷键）"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        bpy.ops.wm.call_menu_pie(name="PIE_MT_rotate_90_pie")
        return {'FINISHED'}


classes = (
    OutlinerState,
    FLATTEN_FACE_OT_operator,
    HOLE_FILL_OT_operator,
    MATERIAL_OT_UltraCombine,
    OUTLINER_OT_ToggleOperator,
    MESH_OT_SmartLoopSelect,
    OBJECT_OT_Rotate90Degrees,
    PIE_MT_Rotate90Pie,
    VIEW3D_OT_OpenRotate90Menu,
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