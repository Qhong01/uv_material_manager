import bpy
import bmesh
from collections import OrderedDict
import traceback
from bpy.types import Operator, PropertyGroup
from bpy.props import BoolProperty, StringProperty, PointerProperty


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