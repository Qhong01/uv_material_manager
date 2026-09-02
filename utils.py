import bpy
import time
import json
import math
import bmesh
from bpy.types import PropertyGroup, Operator
from bpy.props import (
    StringProperty, IntProperty, FloatProperty,
    PointerProperty, CollectionProperty, BoolProperty, EnumProperty
)
from bpy.app.handlers import persistent

# 状态管理器
class StateManager:
    _instance = None
    
    def __init__(self):
        self.last_click = {"layer": 0, "mat": 0}
        self.last_target = {"layer": -1, "mat": ""}
        self.pending_click = None
    
    @classmethod
    def get(cls):
        if not cls._instance:
            cls._instance = cls()
        return cls._instance

# 工具函数
def select_material_objects_by_name(mat_name):
    """根据材质名称选择物体"""
    if bpy.context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    
    targets = []
    for obj in bpy.data.objects:
        if obj.type == 'MESH' and obj.visible_get():
            for slot in obj.material_slots:
                if slot.material and slot.material.name == mat_name:
                    targets.append(obj)
                    break
    
    bpy.ops.object.select_all(action='DESELECT')
    for obj in targets:
        obj.select_set(True)
    
    if targets:
        bpy.context.view_layer.objects.active = targets[0]

# 材质列表项
class MaterialListItem(PropertyGroup):
    """存储材质列表项的属性组"""
    material: PointerProperty(type=bpy.types.Material)

# 插件属性组
class UMProperties(PropertyGroup):
    copied_uv_data: StringProperty(
        name="Copied UV Data",
        description="JSON格式的UV层数据",
        default="",
        update=lambda self, context: self._validate_uv_data(context)
    )
    
    def _validate_uv_data(self, context):
        if self.copied_uv_data:
            try:
                data = json.loads(self.copied_uv_data)
                if not all(k in data for k in ('uv','pin_uv')):
                    self.copied_uv_data = ""
            except:
                self.copied_uv_data = ""

    edit_mode_selection: BoolProperty(default=False)
    selected_material_name: StringProperty(
        name="Selected Material Name",
        default="",
        description="当前选中的材质名称"
    )
    selected_material_names: StringProperty(
        name="Selected Materials",
        default="",
        description="分号分隔的选中材质名称"
    )
    max_material_rows: IntProperty(default=3, min=1, max=20)
    material_list_index: IntProperty()
    material_collection: CollectionProperty(type=MaterialListItem)
    uv_layout_active: BoolProperty(default=False)
    global_sync_active: BoolProperty(
        name="全局同步状态",
        default=False,
        description="控制是否启用全局材质同步"
    )

# 更新选中材质（编辑模式多面高亮）
@persistent
def update_selected_material(scene, depsgraph=None):
    context = bpy.context
    if not hasattr(context, 'scene') or not context.scene:
        return
    if not hasattr(context.scene, 'um_props'):
        return
    um_props = context.scene.um_props
    current_selected = set()
    um_props.edit_mode_selection = False

    if context.mode == 'EDIT_MESH' and context.selected_objects:
        um_props.edit_mode_selection = True
        for obj in context.selected_objects:
            if obj.type == 'MESH':
                try:
                    bm = bmesh.from_edit_mesh(obj.data)
                    selected_faces = [f for f in bm.faces if f.select]
                    for face in selected_faces:
                        if face.material_index < len(obj.material_slots):
                            mat = obj.material_slots[face.material_index].material
                            if mat:
                                current_selected.add(mat.name)
                except Exception:
                    pass

    new_selected = ";".join(sorted(current_selected))
    if um_props.selected_material_names != new_selected:
        um_props.selected_material_names = new_selected

# 重置并刷新材质信息函数 (文件打开事件)
@persistent
def reset_material_info(filepath=None, scene=None):
    context = bpy.context
    if not hasattr(context, 'scene') or not context.scene:
        return
    if hasattr(context.scene, 'um_props'):
        props = context.scene.um_props
        props.material_collection.clear()
        props.selected_material_names = ""
        props.material_list_index = 0

    update_material_list_handler(context.scene)

# 更新材质列表及自动调度缩略图生成与刷新
@persistent
def update_material_list_handler(scene=None, depsgraph=None):
    context = bpy.context
    if not hasattr(context, 'scene') or not context.scene:
        return
    if not hasattr(context.scene, 'um_props'):
        return
    props = context.scene.um_props
    materials = set()

    # 若 depsgraph 存在更新，检查材质或着色器变动，主动触发缩略图后台重载
    if depsgraph:
        for update in depsgraph.updates:
            if isinstance(update.id, bpy.types.Material):
                try:
                    if hasattr(update.id, 'preview_ensure'):
                        prev = update.id.preview_ensure()
                        if prev:
                            prev.reload()
                except Exception:
                    pass

    # 优先收集选中物体的材质，若无多选则读取活动物体材质
    scan_objs = [o for o in context.selected_objects if o.type == 'MESH']
    if not scan_objs and context.active_object and context.active_object.type == 'MESH':
        scan_objs = [context.active_object]

    for obj in scan_objs:
        for slot in obj.material_slots:
            if slot.material:
                materials.add(slot.material)

    new_materials = sorted(materials, key=lambda x: x.name)

    # 确保所有材质的 preview 都在后台渲染队列中并生成 icon_id
    for mat in new_materials:
        try:
            if hasattr(mat, 'preview_ensure'):
                mat.preview_ensure()
        except Exception:
            pass
    
    curr_len = len(props.material_collection)
    need_update = (curr_len != len(new_materials))
    if not need_update:
        for i, mat in enumerate(new_materials):
            if props.material_collection[i].material != mat:
                need_update = True
                break

    if need_update:
        props.material_collection.clear()
        for mat in new_materials:
            item = props.material_collection.add()
            item.material = mat

_material_enum_cache = []

def get_scene_materials_items(self, context):
    global _material_enum_cache
    items = []
    if context:
        for i, mat in enumerate(bpy.data.materials):
            prev = mat.preview_ensure() if hasattr(mat, 'preview_ensure') else mat.preview
            icon_id = prev.icon_id if prev else 0
            items.append((mat.name, mat.name, f"选择材质: {mat.name}", icon_id, i))
    if not items:
        items.append(("NONE", "无材质", "当前场景无材质", 0, 0))
    _material_enum_cache = items
    return _material_enum_cache

def _update_source_mat_from_enum(self, context):
    name = getattr(self, "um_source_mat_enum", "")
    if name and name != "NONE" and name in bpy.data.materials:
        if self.um_source_material != bpy.data.materials[name]:
            self.um_source_material = bpy.data.materials[name]
    else:
        if self.um_source_material is not None:
            self.um_source_material = None

def _update_source_mat_from_ptr(self, context):
    mat = self.um_source_material
    if mat and mat.name in bpy.data.materials:
        if getattr(self, "um_source_mat_enum", "") != mat.name:
            try:
                self.um_source_mat_enum = mat.name
            except Exception:
                pass

# 注册函数
def register():
    bpy.utils.register_class(MaterialListItem)
    bpy.utils.register_class(UMProperties)

    bpy.types.Scene.um_props = PointerProperty(type=UMProperties)
    bpy.types.Scene.um_source_mat_enum = EnumProperty(
        items=get_scene_materials_items,
        name="选择材质",
        description="选择材质",
        update=_update_source_mat_from_enum
    )
    bpy.types.Scene.um_source_material = PointerProperty(
        type=bpy.types.Material,
        name="选择材质",
        description="选择材质",
        update=_update_source_mat_from_ptr
    )
    bpy.types.Scene.uv_layout_active = BoolProperty(default=False)

    if update_selected_material not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(update_selected_material)
    if update_material_list_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(update_material_list_handler)
    
    if reset_material_info not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(reset_material_info)

# 注销函数
def unregister():
    if update_material_list_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(update_material_list_handler)
    if update_selected_material in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(update_selected_material)
    
    if reset_material_info in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(reset_material_info)

    if hasattr(bpy.types.Scene, 'uv_layout_active'):
        del bpy.types.Scene.uv_layout_active
    if hasattr(bpy.types.Scene, 'um_source_mat_enum'):
        del bpy.types.Scene.um_source_mat_enum
    if hasattr(bpy.types.Scene, 'um_source_material'):
        del bpy.types.Scene.um_source_material
    if hasattr(bpy.types.Scene, 'um_props'):
        del bpy.types.Scene.um_props

    bpy.utils.unregister_class(UMProperties)
    bpy.utils.unregister_class(MaterialListItem)