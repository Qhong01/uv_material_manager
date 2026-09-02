bl_info = {
    "name": "UV Checker",
    "author": "Your Name",
    "version": (1, 0),
    "blender": (4, 3, 0),
    "location": "UV Editor > Sidebar > UV Checker",
    "description": "检查并选择所有选中模型的UV翻转、超出第一象限和重叠的问题",
    "category": "UV",
}

import bpy
import bmesh
import mathutils
from mathutils import Vector

class UVCheckerPanel(bpy.types.Panel):
    """UV检查器面板"""
    bl_label = "UV Checker"
    bl_idname = "UV_PT_checker"
    bl_space_type = 'IMAGE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "UV Checker"

    def draw(self, context):
        layout = self.layout
        
        # 检查是否有选中的网格对象
        selected_meshes = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not selected_meshes:
            layout.label(text="请选择至少一个网格对象", icon='ERROR')
            return
            
        # 检查是否处于编辑模式
        if context.mode != 'EDIT_MESH':
            layout.operator("object.mode_set", text="进入编辑模式").mode = 'EDIT'
            return
            
        layout.operator("uv.check_and_select_flipped", icon='ARROW_LEFTRIGHT')
        layout.operator("uv.check_and_select_outside", icon='UV_FACESEL')
        layout.operator("uv.check_and_select_overlapping", icon='OVERLAY')

class CheckAndSelectUVFlipped(bpy.types.Operator):
    """检查并选择所有选中模型的UV翻转面"""
    bl_idname = "uv.check_and_select_flipped"
    bl_label = "检查并选择翻转面"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        selected_meshes = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not selected_meshes:
            self.report({'ERROR'}, "请选择至少一个网格对象")
            return {'CANCELLED'}
            
        # 保存当前活动对象
        active_obj = context.active_object
        
        # 切换到对象模式以安全地访问所有网格
        bpy.ops.object.mode_set(mode='OBJECT')
        
        # 初始化结果
        scene.uv_checker.flipped_faces.clear()
        flipped_uvs = []
        
        # 对每个选中的网格执行检查
        for obj in selected_meshes:
            me = obj.data
            bm = bmesh.new()
            bm.from_mesh(me)
            
            if not bm.loops.layers.uv:
                bm.free()
                continue
                
            uv_layer = bm.loops.layers.uv.active
            
            # 确保查找表是最新的
            bm.faces.ensure_lookup_table()
            
            for face in bm.faces:
                # 计算UV面的环绕顺序
                sum_ = 0.0
                loop_prev = face.loops[-1]
                for loop in face.loops:
                    uv_prev = loop_prev[uv_layer].uv
                    uv_curr = loop[uv_layer].uv
                    sum_ += (uv_curr.x - uv_prev.x) * (uv_curr.y + uv_prev.y)
                    loop_prev = loop
                
                # 如果环绕顺序是顺时针，则认为是翻转的
                if sum_ > 0:
                    item = scene.uv_checker.flipped_faces.add()
                    item.object_name = obj.name
                    item.face_index = face.index
                    flipped_uvs.append((obj.name, face.index))
            
            bm.free()
        
        # 恢复到编辑模式
        bpy.ops.object.mode_set(mode='EDIT')
        
        # 选择所有翻转的UV
        self.select_uvs(context, flipped_uvs)
        
        scene.uv_checker.show_flipped = True
        self.report({'INFO'}, f"发现并选择了 {len(flipped_uvs)} 个翻转的UV面")
        
        # 恢复活动对象
        if active_obj:
            context.view_layer.objects.active = active_obj
            
        return {'FINISHED'}
    
    def select_uvs(self, context, uv_items):
        """选择指定的UV"""
        # 切换到对象模式以安全地访问所有网格
        bpy.ops.object.mode_set(mode='OBJECT')
        
        # 按对象分组UV项
        uvs_by_object = {}
        for obj_name, face_idx in uv_items:
            if obj_name not in uvs_by_object:
                uvs_by_object[obj_name] = []
            uvs_by_object[obj_name].append(face_idx)
        
        # 对每个对象，选择对应的面的UV
        for obj_name, face_indices in uvs_by_object.items():
            obj = bpy.data.objects.get(obj_name)
            if not obj or obj.type != 'MESH':
                continue
                
            me = obj.data
            bm = bmesh.new()
            bm.from_mesh(me)
            
            if not bm.loops.layers.uv:
                bm.free()
                continue
                
            uv_layer = bm.loops.layers.uv.active
            
            # 确保查找表是最新的
            bm.faces.ensure_lookup_table()
            
            # 取消选择所有UV
            for face in bm.faces:
                for loop in face.loops:
                    loop[uv_layer].select = False
            
            # 选择指定面的所有UV
            for face_idx in face_indices:
                if face_idx < len(bm.faces):
                    for loop in bm.faces[face_idx].loops:
                        loop[uv_layer].select = True
            
            bm.to_mesh(me)
            bm.free()
        
        # 恢复到编辑模式
        bpy.ops.object.mode_set(mode='EDIT')

class CheckAndSelectUVOutside(bpy.types.Operator):
    """检查并选择所有选中模型的UV超出第一象限的面"""
    bl_idname = "uv.check_and_select_outside"
    bl_label = "检查并选择超出区域面"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        selected_meshes = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not selected_meshes:
            self.report({'ERROR'}, "请选择至少一个网格对象")
            return {'CANCELLED'}
            
        # 保存当前活动对象
        active_obj = context.active_object
        
        # 切换到对象模式以安全地访问所有网格
        bpy.ops.object.mode_set(mode='OBJECT')
        
        # 初始化结果
        scene.uv_checker.outside_faces.clear()
        outside_uvs = []
        
        # 对每个选中的网格执行检查
        for obj in selected_meshes:
            me = obj.data
            bm = bmesh.new()
            bm.from_mesh(me)
            
            if not bm.loops.layers.uv:
                bm.free()
                continue
                
            uv_layer = bm.loops.layers.uv.active
            
            # 确保查找表是最新的
            bm.faces.ensure_lookup_table()
            
            for face in bm.faces:
                outside = False
                for loop in face.loops:
                    uv = loop[uv_layer].uv
                    # 考虑一个小的容差值，避免浮点数精度问题
                    if uv.x < -0.0001 or uv.y < -0.0001 or uv.x > 1.0001 or uv.y > 1.0001:
                        outside = True
                        break
                
                if outside:
                    item = scene.uv_checker.outside_faces.add()
                    item.object_name = obj.name
                    item.face_index = face.index
                    outside_uvs.append((obj.name, face.index))
            
            bm.free()
        
        # 恢复到编辑模式
        bpy.ops.object.mode_set(mode='EDIT')
        
        # 选择所有超出区域的UV
        CheckAndSelectUVFlipped.select_uvs(self, context, outside_uvs)
        
        scene.uv_checker.show_outside = True
        self.report({'INFO'}, f"发现并选择了 {len(outside_uvs)} 个超出第一象限的UV面")
        
        # 恢复活动对象
        if active_obj:
            context.view_layer.objects.active = active_obj
            
        return {'FINISHED'}

class CheckAndSelectUVOverlapping(bpy.types.Operator):
    """检查并选择所有选中模型的UV重叠岛"""
    bl_idname = "uv.check_and_select_overlapping"
    bl_label = "检查并选择重叠UV岛"
    bl_options = {'REGISTER', 'UNDO'}
    
    # 容差值，用于处理浮点数精度问题
    EPSILON = 0.0001

    def execute(self, context):
        scene = context.scene
        selected_meshes = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not selected_meshes:
            self.report({'ERROR'}, "请选择至少一个网格对象")
            return {'CANCELLED'}
            
        # 保存当前活动对象
        active_obj = context.active_object
        
        # 切换到对象模式以安全地访问所有网格
        bpy.ops.object.mode_set(mode='OBJECT')
        
        # 初始化结果
        scene.uv_checker.overlapping_islands.clear()
        overlapping_uvs = []
        
        # 收集所有选中对象的UV岛及相关数据
        all_islands_data = []
        
        for obj in selected_meshes:
            me = obj.data
            bm = bmesh.new()
            bm.from_mesh(me)
            
            if not bm.loops.layers.uv:
                bm.free()
                continue
                
            uv_layer = bm.loops.layers.uv.active
            
            # 确保查找表是最新的
            bm.faces.ensure_lookup_table()
            
            # 识别UV岛
            islands = self.find_uv_islands(bm, uv_layer)
            
            # 为每个岛计算边界框并保存相关数据
            for island in islands:
                bbox = self.calculate_island_bbox(bm, island, uv_layer)
                edges = self.get_island_edges(bm, island, uv_layer)
                # 计算岛的UV面集合，用于包含测试
                faces = self.get_island_faces(bm, island, uv_layer)
                all_islands_data.append({
                    'obj_name': obj.name,
                    'island': island,
                    'bbox': bbox,
                    'edges': edges,
                    'faces': faces,
                    'bm': bm,  # 保存bmesh引用以便后续使用
                    'uv_layer': uv_layer
                })
        
        # 构建边界框树进行空间分区
        bbox_tree = self.build_bbox_tree(all_islands_data)
        
        # 检查所有岛之间的重叠
        overlapping_island_indices = set()
        
        # 使用边界框树快速查找可能重叠的岛对
        for i, island_data in enumerate(all_islands_data):
            bbox = island_data['bbox']
            # 查询与当前岛边界框相交的所有岛索引
            candidates = bbox_tree.intersect(bbox)
            
            for j in candidates:
                if j <= i:  # 避免重复比较
                    continue
                    
                other_data = all_islands_data[j]
                
                # 如果岛来自不同对象，需要检查
                if island_data['obj_name'] != other_data['obj_name']:
                    if self.islands_overlap(island_data, other_data):
                        overlapping_island_indices.add(i)
                        overlapping_island_indices.add(j)
                else:
                    # 同一对象的岛
                    if self.islands_overlap(island_data, other_data):
                        overlapping_island_indices.add(i)
                        overlapping_island_indices.add(j)
        
        # 释放所有bmesh资源
        for data in all_islands_data:
            data['bm'].free()
        
        # 收集重叠岛中的所有面
        for idx in overlapping_island_indices:
            island_data = all_islands_data[idx]
            for face_idx in island_data['island']:
                overlapping_uvs.append((island_data['obj_name'], face_idx))
                item = scene.uv_checker.overlapping_islands.add()
                item.object_name = island_data['obj_name']
                item.face_index = face_idx
        
        # 恢复到编辑模式
        bpy.ops.object.mode_set(mode='EDIT')
        
        # 选择所有重叠的UV
        CheckAndSelectUVFlipped.select_uvs(self, context, overlapping_uvs)
        
        scene.uv_checker.show_overlapping = True
        self.report({'INFO'}, f"发现并选择了 {len(overlapping_uvs)} 个属于重叠UV岛的面")
        
        # 恢复活动对象
        if active_obj:
            context.view_layer.objects.active = active_obj
            
        return {'FINISHED'}
    
    def find_uv_islands(self, bm, uv_layer):
        """识别UV岛（相连的UV面组）"""
        # 确保查找表是最新的
        bm.faces.ensure_lookup_table()
        
        # 初始化所有面为未访问
        visited = set()
        islands = []
        
        for face in bm.faces:
            if face.index not in visited:
                # 开始一个新岛
                island = set()
                stack = [face]
                
                while stack:
                    current_face = stack.pop()
                    if current_face.index in visited:
                        continue
                        
                    visited.add(current_face.index)
                    island.add(current_face.index)
                    
                    # 查找与当前面共享UV边的所有面
                    for loop in current_face.loops:
                        vert = loop.vert
                        uv = loop[uv_layer].uv
                        
                        # 查找具有相同UV坐标的相邻边
                        for edge in vert.link_edges:
                            if edge.is_boundary:
                                continue
                                
                            # 查找边的另一个面
                            other_face = None
                            for f in edge.link_faces:
                                if f != current_face:
                                    other_face = f
                                    break
                            
                            if other_face and other_face.index not in visited:
                                # 检查边的两个顶点的UV是否匹配
                                matches = 0
                                for other_loop in other_face.loops:
                                    if other_loop.vert == vert:
                                        if abs(other_loop[uv_layer].uv.x - uv.x) < self.EPSILON and \
                                           abs(other_loop[uv_layer].uv.y - uv.y) < self.EPSILON:
                                            matches += 1
                                
                                # 如果两个顶点的UV都匹配，则视为相连
                                if matches >= 2:
                                    stack.append(other_face)
                
                islands.append(island)
        
        return islands
    
    def build_bbox_tree(self, islands_data):
        """构建边界框树用于快速空间查询"""
        # 简化的边界框树实现，实际应用中可以使用更高效的数据结构
        # 这里使用一个简单的列表来存储边界框和索引
        bbox_tree = []
        
        for i, data in enumerate(islands_data):
            bbox = data['bbox']
            # 边界框格式：(min_x, min_y, max_x, max_y, index)
            bbox_tree.append((bbox[0], bbox[1], bbox[2], bbox[3], i))
        
        return BBoxTree(bbox_tree)
    
    def islands_overlap(self, island_data1, island_data2):
        """检查两个UV岛是否重叠"""
        # 快速边界框检查
        if not self.bboxes_overlap(island_data1['bbox'], island_data2['bbox']):
            return False
            
        # 更精确的检查：首先检查边是否相交
        edges1 = island_data1['edges']
        edges2 = island_data2['edges']
        
        # 只在边界框重叠时进行边相交检查
        for e1 in edges1:
            for e2 in edges2:
                if self.edges_overlap(e1, e2):
                    return True
        
        # 如果边不相交，检查一个岛是否完全包含在另一个岛内部
        return self.island_contains_other(island_data1, island_data2) or \
               self.island_contains_other(island_data2, island_data1)
    
    def calculate_island_bbox(self, bm, island, uv_layer):
        """计算UV岛的边界框"""
        # 确保查找表是最新的
        bm.faces.ensure_lookup_table()
        
        min_x = min_y = float('inf')
        max_x = max_y = float('-inf')
        
        for face_idx in island:
            if face_idx < len(bm.faces):
                face = bm.faces[face_idx]
                for loop in face.loops:
                    uv = loop[uv_layer].uv
                    min_x = min(min_x, uv.x)
                    min_y = min(min_y, uv.y)
                    max_x = max(max_x, uv.x)
                    max_y = max(max_y, uv.y)
                
        return (min_x, min_y, max_x, max_y)
    
    def bboxes_overlap(self, bbox1, bbox2):
        """检查两个边界框是否重叠"""
        min_x1, min_y1, max_x1, max_y1 = bbox1
        min_x2, min_y2, max_x2, max_y2 = bbox2
        
        if max_x1 < min_x2 - self.EPSILON or max_x2 < min_x1 - self.EPSILON:
            return False
        if max_y1 < min_y2 - self.EPSILON or max_y2 < min_y1 - self.EPSILON:
            return False
            
        return True
    
    def get_island_edges(self, bm, island, uv_layer):
        """获取UV岛的所有外部边"""
        # 确保查找表是最新的
        bm.faces.ensure_lookup_table()
        
        edges = set()
        
        for face_idx in island:
            if face_idx < len(bm.faces):
                face = bm.faces[face_idx]
                for loop in face.loops:
                    v1 = loop.vert
                    v2 = loop.link_loop_next.vert
                    
                    # 按顶点索引排序，确保边的一致性
                    if v1.index < v2.index:
                        edge_key = (v1.index, v2.index)
                    else:
                        edge_key = (v2.index, v1.index)
                        
                    # 只添加外部边（不被两个面共享）
                    if edge_key not in edges:
                        edges.add(edge_key)
                    else:
                        edges.remove(edge_key)  # 共享边，不是外部边
        
        # 将边转换为UV坐标
        uv_edges = []
        for v1_idx, v2_idx in edges:
            # 找到边的UV坐标（使用第一个遇到的面）
            uv1 = None
            uv2 = None
            
            for face_idx in island:
                if face_idx < len(bm.faces):
                    face = bm.faces[face_idx]
                    for loop in face.loops:
                        if loop.vert.index == v1_idx:
                            uv1 = loop[uv_layer].uv
                        if loop.vert.index == v2_idx:
                            uv2 = loop[uv_layer].uv
                            
                    if uv1 and uv2:
                        break
                        
            if uv1 and uv2:
                uv_edges.append((uv1, uv2))
                
        return uv_edges
    
    def edges_overlap(self, edge1, edge2):
        """检查两条边是否重叠或相交"""
        (a1, a2) = edge1
        (b1, b2) = edge2
        
        # 检查是否有共同端点
        if (abs(a1.x - b1.x) < self.EPSILON and abs(a1.y - b1.y) < self.EPSILON) or \
           (abs(a1.x - b2.x) < self.EPSILON and abs(a1.y - b2.y) < self.EPSILON) or \
           (abs(a2.x - b1.x) < self.EPSILON and abs(a2.y - b1.y) < self.EPSILON) or \
           (abs(a2.x - b2.x) < self.EPSILON and abs(a2.y - b2.y) < self.EPSILON):
            return False  # 仅共享端点不算重叠
            
        # 检查线段是否相交
        return self.segments_intersect(a1, a2, b1, b2)
    
    def segments_intersect(self, a1, a2, b1, b2):
        """检查两个线段是否相交"""
        x1, y1 = a1
        x2, y2 = a2
        x3, y3 = b1
        x4, y4 = b2
        
        # 计算行列式
        det = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
        
        # 如果行列式为0，线段平行或共线
        if abs(det) < self.EPSILON:
            return False
            
        # 计算交点参数
        t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / det
        u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / det
        
        # 检查参数是否在区间(0,1)内
        return (0 < t < 1) and (0 < u < 1)
    
    def get_island_faces(self, bm, island, uv_layer):
        """获取UV岛的所有面的UV表示"""
        faces = []
        for face_idx in island:
            if face_idx < len(bm.faces):
                face = bm.faces[face_idx]
                # 收集面的所有UV顶点
                uv_verts = [loop[uv_layer].uv for loop in face.loops]
                faces.append(uv_verts)
        return faces
    
    def is_point_inside_polygon(self, point, polygon):
        """检查点是否在多边形内部"""
        x, y = point
        inside = False
        
        # 射线法判断点是否在多边形内部
        for i in range(len(polygon)):
            x1, y1 = polygon[i]
            x2, y2 = polygon[(i+1) % len(polygon)]
            
            if y > min(y1, y2):
                if y <= max(y1, y2):
                    if x <= max(x1, x2):
                        if y1 != y2:
                            xinters = (y - y1) * (x2 - x1) / (y2 - y1) + x1
                        if x1 == x2 or x <= xinters:
                            inside = not inside
        
        return inside
    
    def is_island_face_inside_other(self, face, other_island_faces):
        """检查一个面是否在另一个岛的任何面内部"""
        # 对每个面的顶点，检查是否都在另一个岛的某个面内部
        for other_face in other_island_faces:
            all_inside = True
            for point in face:
                if not self.is_point_inside_polygon(point, other_face):
                    all_inside = False
                    break
            if all_inside:
                return True
        return False
    
    def island_contains_other(self, container_data, containee_data):
        """检查一个岛是否完全包含另一个岛"""
        container_faces = container_data['faces']
        containee_faces = containee_data['faces']
        
        # 检查containee的每个面是否都在container的某个面内
        for containee_face in containee_faces:
            face_inside = False
            for container_face in container_faces:
                # 检查containee的所有顶点是否都在container的面内
                all_points_inside = True
                for point in containee_face:
                    if not self.is_point_inside_polygon(point, container_face):
                        all_points_inside = False
                        break
                if all_points_inside:
                    face_inside = True
                    break
            if not face_inside:
                return False  # 有一个面不在内部，整个岛就不被包含
        
        return True  # 所有面都在内部，岛被完全包含

class BBoxTree:
    """简化的边界框树实现，用于快速空间查询"""
    def __init__(self, bboxes):
        self.bboxes = bboxes
        
    def intersect(self, bbox):
        """查询与给定边界框相交的所有项目索引"""
        min_x, min_y, max_x, max_y = bbox
        result = []
        
        for box in self.bboxes:
            box_min_x, box_min_y, box_max_x, box_max_y, index = box
            
            if box_max_x < min_x or max_x < box_min_x:
                continue
            if box_max_y < min_y or max_y < box_min_y:
                continue
                
            result.append(index)
            
        return result

class ClearAllSelections(bpy.types.Operator):
    """清除所有选择"""
    bl_idname = "uv.clear_selections"
    bl_label = "清除选择"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        selected_meshes = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not selected_meshes:
            self.report({'INFO'}, "没有选中的网格对象")
            return {'FINISHED'}
            
        # 保存当前活动对象
        active_obj = context.active_object
        
        # 切换到对象模式以安全地访问所有网格
        bpy.ops.object.mode_set(mode='OBJECT')
        
        # 清除所有UV选择
        for obj in selected_meshes:
            me = obj.data
            bm = bmesh.new()
            bm.from_mesh(me)
            
            if not bm.loops.layers.uv:
                bm.free()
                continue
                
            uv_layer = bm.loops.layers.uv.active
            
            # 确保查找表是最新的
            bm.faces.ensure_lookup_table()
            
            # 取消所有UV选择
            for face in bm.faces:
                for loop in face.loops:
                    loop[uv_layer].select = False
            
            bm.to_mesh(me)
            bm.free()
        
        # 恢复到编辑模式
        bpy.ops.object.mode_set(mode='EDIT')
        
        # 清除结果
        scene.uv_checker.flipped_faces.clear()
        scene.uv_checker.outside_faces.clear()
        scene.uv_checker.overlapping_islands.clear()
        
        scene.uv_checker.show_flipped = False
        scene.uv_checker.show_outside = False
        scene.uv_checker.show_overlapping = False
        
        self.report({'INFO'}, "已清除所有UV选择")
        
        # 恢复活动对象
        if active_obj:
            context.view_layer.objects.active = active_obj
            
        return {'FINISHED'}

class UVFaceIndex(bpy.types.PropertyGroup):
    object_name: bpy.props.StringProperty(name="Object Name")
    face_index: bpy.props.IntProperty(name="Face Index")

class UVCheckerProperties(bpy.types.PropertyGroup):
    flipped_faces: bpy.props.CollectionProperty(type=UVFaceIndex)
    outside_faces: bpy.props.CollectionProperty(type=UVFaceIndex)
    overlapping_islands: bpy.props.CollectionProperty(type=UVFaceIndex)
    
    show_flipped: bpy.props.BoolProperty(default=False)
    show_outside: bpy.props.BoolProperty(default=False)
    show_overlapping: bpy.props.BoolProperty(default=False)

classes = [
    UVFaceIndex,
    UVCheckerProperties,
    UVCheckerPanel,
    CheckAndSelectUVFlipped,
    CheckAndSelectUVOutside,
    CheckAndSelectUVOverlapping,
    # 移除ClearAllSelections类，不再需要
]

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.uv_checker = bpy.props.PointerProperty(type=UVCheckerProperties)

def unregister():
    del bpy.types.Scene.uv_checker
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register()    