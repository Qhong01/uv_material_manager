import os
import json
import shutil
import bpy
import bmesh
import time
from bpy.types import Operator, UIList
from bpy.props import StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper
from .utils import StateManager, select_material_objects_by_name

# =========================================================================
# 本地持久化材质模板库管理器 (跨工程/跨文件通用)
# =========================================================================

class MaterialTemplateManager:
    @classmethod
    def get_lib_path(cls):
        addon_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(addon_dir, "material_templates.blend")

    @classmethod
    def get_template_names(cls):
        p = cls.get_lib_path()
        if not os.path.exists(p):
            return set()
        try:
            with bpy.data.libraries.load(p) as (data_from, _):
                return {str(n) for n in data_from.materials}
        except Exception:
            return set()

    @classmethod
    def save_template(cls, mat):
        if not mat:
            return False
        p = cls.get_lib_path()
        mat.use_fake_user = True

        mats_to_save = {mat}
        if os.path.exists(p):
            try:
                with bpy.data.libraries.load(p) as (data_from, _):
                    other_names = [str(n) for n in data_from.materials if str(n) != mat.name]

                to_load = [n for n in other_names if n not in bpy.data.materials]
                for n in other_names:
                    if n in bpy.data.materials:
                        mats_to_save.add(bpy.data.materials[n])

                if to_load:
                    with bpy.data.libraries.load(p) as (data_from, data_to):
                        data_to.materials = to_load
                    for loaded in data_to.materials:
                        if loaded:
                            mats_to_save.add(loaded)
            except Exception as e:
                print("加载现有材质模板失败:", e)

        try:
            bpy.data.libraries.write(p, mats_to_save, fake_user=True)
            return True
        except Exception as e:
            print("保存材质模板库失败:", e)
            return False

    @classmethod
    def delete_template(cls, mat_name):
        p = cls.get_lib_path()
        if not os.path.exists(p):
            return False
        try:
            with bpy.data.libraries.load(p) as (data_from, _):
                remaining_names = [str(n) for n in data_from.materials if str(n) != mat_name]
        except Exception:
            return False

        if not remaining_names:
            try:
                os.remove(p)
            except Exception:
                pass
            return True

        mats_to_save = set()
        to_load = [n for n in remaining_names if n not in bpy.data.materials]
        for n in remaining_names:
            if n in bpy.data.materials:
                mats_to_save.add(bpy.data.materials[n])

        if to_load:
            try:
                with bpy.data.libraries.load(p) as (data_from, data_to):
                    data_to.materials = to_load
                for loaded in data_to.materials:
                    if loaded:
                        mats_to_save.add(loaded)
            except Exception as e:
                print("加载模板数据失败:", e)

        try:
            bpy.data.libraries.write(p, mats_to_save, fake_user=True)
            return True
        except Exception as e:
            print("更新材质模板库失败:", e)
            return False

    @classmethod
    def load_single_template(cls, mat_name):
        p = cls.get_lib_path()
        if not os.path.exists(p):
            return None
        try:
            with bpy.data.libraries.load(p) as (data_from, data_to):
                if mat_name in data_from.materials:
                    data_to.materials = [mat_name]
            for m in bpy.data.materials:
                if m.name == mat_name or m.name.startswith(mat_name):
                    m.use_fake_user = True
                    return m
        except Exception as e:
            print("载入模板失败:", e)
        return None

    @classmethod
    def import_from_blend(cls, src_blend_path):
        """从任意 .blend 文件导入材质模板并合并入库"""
        if not os.path.exists(src_blend_path):
            return 0
        try:
            with bpy.data.libraries.load(src_blend_path) as (data_from, _):
                mat_names = [str(n) for n in data_from.materials]
            if not mat_names:
                return 0
            
            with bpy.data.libraries.load(src_blend_path) as (data_from, data_to):
                data_to.materials = list(mat_names)
            
            loaded_mats = [m for m in data_to.materials if m]
            count = 0
            for orig_name, m in zip(mat_names, loaded_mats):
                # 若内存中原本已有同名材质被加上了 .001，恢复原始名字保存
                if m.name != orig_name and orig_name not in bpy.data.materials:
                    try:
                        m.name = orig_name
                    except Exception:
                        pass
                if cls.save_template(m):
                    count += 1
            return count
        except Exception as e:
            print("导入材质库失败:", e)
            return 0

    @classmethod
    def export_to_blend(cls, dst_blend_path):
        """将当前材质库导出至指定路径"""
        src_p = cls.get_lib_path()
        if not os.path.exists(src_p):
            return False
        try:
            shutil.copy2(src_p, dst_blend_path)
            return True
        except Exception as e:
            print("导出材质库失败:", e)
            return False

# 统计材质用户数
def count_material_users_by_name(mat_name):
    """根据材质名称统计实际用户数"""
    count = 0
    for obj in bpy.data.objects:
        if obj.type == 'MESH':
            for slot in obj.material_slots:
                if slot.material and slot.material.name == mat_name:
                    count += 1
    return count
    
class MATERIAL_OT_ApplyMaterial(Operator):
    bl_idname = "material.apply_from_scene"
    bl_label = "添加材质"
    bl_description = "将当前源材质添加到选中的物体或选中的面"
    bl_options = {'REGISTER', 'UNDO'}
    
    material_name: StringProperty(name="材质名称", default="")
    
    def execute(self, context):
        # 检查是否有选中的物体
        if not context.selected_objects:
            self.report({'WARNING'}, "请先选择要应用材质的物体")
            return {'CANCELLED'}
        
        # 获取指定名称的材质或源材质
        mat = None
        if self.material_name:
            mat = bpy.data.materials.get(self.material_name)
        if not mat and hasattr(context.scene, 'um_source_material') and context.scene.um_source_material:
            mat = context.scene.um_source_material
            
        if not mat:
            self.report({'WARNING'}, "请先在'源材质'中选择要应用的材质")
            return {'CANCELLED'}
        
        applied_count = 0
        is_edit_mode = (context.mode == 'EDIT_MESH')
        original_mode = context.mode
        
        for obj in context.selected_objects:
            if obj.type == 'MESH':
                existing_index = None
                for i, slot in enumerate(obj.material_slots):
                    if slot.material == mat:
                        existing_index = i
                        break
                
                if is_edit_mode:
                    bpy.ops.object.mode_set(mode='OBJECT')
                    
                    has_selected_faces = False
                    for face in obj.data.polygons:
                        if face.select:
                            has_selected_faces = True
                            break
                    
                    if not has_selected_faces:
                        bpy.ops.object.mode_set(mode='EDIT')
                        continue
                    
                    if existing_index is not None:
                        obj.active_material_index = existing_index
                        obj.active_material = mat
                    else:
                        obj.data.materials.append(mat)
                        obj.active_material_index = len(obj.material_slots) - 1
                    
                    bpy.ops.object.mode_set(mode='EDIT')
                    bpy.ops.object.material_slot_assign()
                    applied_count += 1
                else:
                    if existing_index is not None:
                        obj.active_material_index = existing_index
                        obj.active_material = mat
                    else:
                        obj.data.materials.append(mat)
                        obj.active_material_index = len(obj.material_slots) - 1
                    applied_count += 1
        
        if applied_count > 0:
            self.report({'INFO'}, f"成功将材质 '{mat.name}' 应用到 {applied_count} 个物体")
            return {'FINISHED'}
        else:
            self.report({'WARNING'}, "没有可应用材质的网格物体")
            return {'CANCELLED'}

class MATERIAL_OT_DeleteMaterial(Operator):
    bl_idname = "material.delete_pro"
    bl_label = "删除材质"
    bl_description = "删除材质列表中当前选中的材质"
    bl_options = {'REGISTER', 'UNDO'}
    
    mat_name: StringProperty(default="")  # 接收材质名称的属性，为空时自动删除列表当前选中项
    
    def execute(self, context):
        mat_name = self.mat_name
        props = getattr(context.scene, 'um_props', None)

        if not mat_name:
            # 1. 优先获取材质列表中的当前选中项
            if props and props.material_collection and 0 <= props.material_list_index < len(props.material_collection):
                item = props.material_collection[props.material_list_index]
                if item.material:
                    mat_name = item.material.name

            # 2. 其次使用 selected_material_name
            if not mat_name and props and getattr(props, 'selected_material_name', ''):
                mat_name = props.selected_material_name

            # 3. 再次获取活动物体的活动材质
            if not mat_name and context.active_object and context.active_object.type == 'MESH':
                if context.active_object.active_material:
                    mat_name = context.active_object.active_material.name

        if not mat_name:
            self.report({'WARNING'}, "请先在材质列表中选择要删除的材质")
            return {'CANCELLED'}

        mat = bpy.data.materials.get(mat_name)
        if not mat:
            self.report({'WARNING'}, f"材质 '{mat_name}' 不存在")
            return {'CANCELLED'}
        
        # 从所有选中物体的材质槽中移除该材质
        removed_from_slots = False
        
        # 遍历所有选中物体
        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue
            
            # 获取需要删除的材质槽索引
            slots_to_remove = [
                idx for idx, slot in enumerate(obj.material_slots) 
                if slot.material == mat
            ]
            
            # 逆序删除材质槽（避免索引变化）
            for idx in reversed(slots_to_remove):
                obj.active_material_index = idx
                with context.temp_override(
                    window=context.window,
                    area=context.area,
                    region=context.region,
                    object=obj
                ):
                    bpy.ops.object.material_slot_remove()
                removed_from_slots = True

        # 若没有选中物体，也检查活动物体
        if not context.selected_objects and context.active_object and context.active_object.type == 'MESH':
            obj = context.active_object
            slots_to_remove = [
                idx for idx, slot in enumerate(obj.material_slots) 
                if slot.material == mat
            ]
            for idx in reversed(slots_to_remove):
                obj.active_material_index = idx
                with context.temp_override(
                    window=context.window,
                    area=context.area,
                    region=context.region,
                    object=obj
                ):
                    bpy.ops.object.material_slot_remove()
                removed_from_slots = True
        
        # 清理未使用的材质
        if mat.users == 0 and not mat.use_fake_user:
            bpy.data.materials.remove(mat)
            self.report({'INFO'}, f"材质 '{mat_name}' 已彻底删除")
        elif removed_from_slots:
            self.report({'INFO'}, f"已从物体的材质槽中移除材质 '{mat_name}'")
        else:
            if mat.users <= 1:
                bpy.data.materials.remove(mat)
                self.report({'INFO'}, f"材质 '{mat_name}' 已删除")
            else:
                self.report({'INFO'}, f"材质 '{mat_name}' 仍有 {mat.users} 个用户使用")

        # 同步清理源材质引用
        if context.scene.um_source_material and (context.scene.um_source_material == mat or context.scene.um_source_material.name == mat_name):
            context.scene.um_source_material = None

        # 刷新列表
        try:
            bpy.ops.material.update_list_pro()
        except Exception:
            pass

        return {'FINISHED'}

class MATERIAL_OT_CreateNew(Operator):
    bl_idname = "material.create_new_pro"
    bl_label = "新建材质"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        new_mat = bpy.data.materials.new(name="Material")
        new_mat.use_nodes = True
        
        # 初始化节点
        nodes = new_mat.node_tree.nodes
        nodes.clear()
        principled = nodes.new('ShaderNodeBsdfPrincipled')
        output = nodes.new('ShaderNodeOutputMaterial')
        new_mat.node_tree.links.new(principled.outputs['BSDF'], output.inputs['Surface'])

        # 处理不同模式
        if context.mode == 'EDIT_MESH':
            # 确保至少选择一个面
            if not context.active_object or not context.active_object.data.total_face_sel:
                self.report({'WARNING'}, "请先选择面")
                return {'CANCELLED'}
            
            # 遍历所有选中物体
            for obj in context.selected_objects:
                if obj.type != 'MESH':
                    continue
                
                # 进入物体模式设置材质
                bpy.ops.object.mode_set(mode='OBJECT')
                
                # 添加材质槽（如果不存在）
                if not obj.data.materials:
                    obj.data.materials.append(new_mat)
                else:
                    # 追加新材质并设置为活动槽
                    obj.data.materials.append(new_mat)
                    obj.active_material_index = len(obj.data.materials) - 1
                
                # 返回编辑模式并分配材质
                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.object.material_slot_assign()
                
        else:
            # 对象模式默认处理
            for obj in context.selected_objects:
                if obj.type == 'MESH':
                    obj.data.materials.append(new_mat)

        # 设置为当前源材质
        context.scene.um_source_material = new_mat

        # 预览生成
        def safe_preview():
            try:
                if new_mat.preview:
                    new_mat.preview.ensure()
            except Exception as e:
                print(f"预览生成失败: {str(e)}")
        
        bpy.app.timers.register(safe_preview, first_interval=0.2)
        return {'FINISHED'}

    def invoke(self, context, event):
        # 自动处理模式切换
        if context.mode == 'OBJECT':
            return self.execute(context)
        elif context.mode == 'EDIT_MESH':
            # 直接执行（跳过确认对话框）
            return self.execute(context)
class MATERIAL_OT_SaveSourceTemplate(Operator):
    """将当前选择的源材质保存为本地模板库，可在任何文件中直接使用"""
    bl_idname = "material.save_source_template"
    bl_label = "保存为源材质模板"
    bl_description = "将当前选择的源材质保存到本地模板库，打开其他文件也能跨工程直接调用"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        mat = context.scene.um_source_material
        if not mat:
            # 若源材质未显式选定，则获取活动物体的当前材质
            if context.active_object and context.active_object.type == 'MESH' and context.active_object.active_material:
                mat = context.active_object.active_material
                context.scene.um_source_material = mat
            else:
                self.report({'WARNING'}, "请先选择要保存为模板的源材质")
                return {'CANCELLED'}

        success = MaterialTemplateManager.save_template(mat)
        if success:
            self.report({'INFO'}, f"已将源材质 '{mat.name}' 保存为本地模板（跨工程可用）")
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, "保存源材质模板失败")
            return {'CANCELLED'}


class MATERIAL_OT_DeleteSourceTemplate(Operator):
    """从本地模板库中删除材质模板"""
    bl_idname = "material.delete_source_template"
    bl_label = "删除材质模板"
    bl_description = "从本地材质模板库中彻底删除该材质模板"
    bl_options = {'REGISTER', 'UNDO'}

    template_name: StringProperty(default="")

    def execute(self, context):
        mat_name = self.template_name
        if not mat_name:
            mat = context.scene.um_source_material
            if mat:
                mat_name = mat.name

        if not mat_name:
            self.report({'WARNING'}, "请先选择要删除的材质模板")
            return {'CANCELLED'}

        templates = MaterialTemplateManager.get_template_names()
        if mat_name not in templates:
            self.report({'WARNING'}, f"'{mat_name}' 不是已保存的材质模板")
            return {'CANCELLED'}

        success = MaterialTemplateManager.delete_template(mat_name)
        if success:
            if context.scene.um_source_material and context.scene.um_source_material.name == mat_name:
                context.scene.um_source_material = None
            self.report({'INFO'}, f"已从本地模板库中删除模板 '{mat_name}'")
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, "删除材质模板失败")
            return {'CANCELLED'}

class MATERIAL_OT_LoadTemplateToSource(Operator):
    """从本地模板库中载入选定的材质模板到源材质"""
    bl_idname = "material.load_template_to_source"
    bl_label = "载入材质模板"
    bl_description = "从本地模板库中载入选定的材质模板到源材质"
    bl_options = {'REGISTER', 'UNDO'}

    template_name: StringProperty()

    def execute(self, context):
        if not self.template_name:
            return {'CANCELLED'}

        mat = bpy.data.materials.get(self.template_name)
        if not mat:
            mat = MaterialTemplateManager.load_single_template(self.template_name)

        if mat:
            context.scene.um_source_material = mat
            self.report({'INFO'}, f"已从本地模板库载入源材质: '{mat.name}'")
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, f"无法载入材质模板: '{self.template_name}'")
            return {'CANCELLED'}


class MATERIAL_OT_ExportTemplateLibrary(Operator, ExportHelper):
    """将本地材质模板库导出为独立的 .blend 文件，方便在其它电脑使用或备份"""
    bl_idname = "material.export_template_library"
    bl_label = "导出材质模板库"
    bl_description = "将当前材质模板库保存为独立的 .blend 文件，方便拷贝到其它电脑"

    filename_ext = ".blend"
    filter_glob: StringProperty(default="*.blend", options={'HIDDEN'})
    filepath: StringProperty(default="material_templates.blend", subtype='FILE_PATH')

    def execute(self, context):
        if not self.filepath:
            return {'CANCELLED'}
        success = MaterialTemplateManager.export_to_blend(self.filepath)
        if success:
            self.report({'INFO'}, f"已成功导出材质模板库至: {self.filepath}")
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, "导出材质模板库失败，当前可能没有已保存的模板")
            return {'CANCELLED'}


class MATERIAL_OT_ImportTemplateLibrary(Operator, ImportHelper):
    """从外部 .blend 文件导入材质模板并合并到当前模板库中"""
    bl_idname = "material.import_template_library"
    bl_label = "导入材质模板库"
    bl_description = "从其它电脑导出的材质库或任意 .blend 文件中导入材质模板"

    filename_ext = ".blend"
    filter_glob: StringProperty(default="*.blend", options={'HIDDEN'})

    def execute(self, context):
        if not self.filepath or not os.path.exists(self.filepath):
            self.report({'WARNING'}, "未选择有效的 .blend 文件")
            return {'CANCELLED'}
        count = MaterialTemplateManager.import_from_blend(self.filepath)
        if count > 0:
            self.report({'INFO'}, f"成功导入 {count} 个材质模板到本地库")
            return {'FINISHED'}
        else:
            self.report({'WARNING'}, "选中的文件中未找到有效材质或导入失败")
            return {'CANCELLED'}


class MATERIAL_OT_OpenTemplateLibraryFolder(Operator):
    """打开本地材质模板库所在文件夹"""
    bl_idname = "material.open_template_library_folder"
    bl_label = "打开材质库目录"
    bl_description = "在文件管理器中打开存放材质模板库的插件文件夹"

    def execute(self, context):
        folder = os.path.dirname(MaterialTemplateManager.get_lib_path())
        if os.path.exists(folder):
            try:
                os.startfile(folder)
                self.report({'INFO'}, f"已打开文件夹: {folder}")
                return {'FINISHED'}
            except Exception as e:
                self.report({'ERROR'}, f"无法打开文件夹: {e}")
                return {'CANCELLED'}
        else:
            self.report({'WARNING'}, "文件夹不存在")
            return {'CANCELLED'}


class MATERIAL_OT_ReloadAllTextures(Operator):
    """一键刷新工程中所有材质的图像贴图和材质球预览"""
    bl_idname = "material.reload_all_textures"
    bl_label = "刷新全部材质贴图"
    bl_description = "一键重载所有材质使用的外部图像贴图文件并刷新材质球预览 (相当于批量 Alt+R)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        reloaded_images = set()
        
        # 1. 刷新所有图像数据块
        for img in bpy.data.images:
            if img.source in {'FILE', 'SEQUENCE', 'TILED'}:
                try:
                    img.reload()
                    reloaded_images.add(img.name)
                except Exception as e:
                    print(f"刷新贴图 {img.name} 失败: {e}")

        # 2. 刷新所有材质的着色器节点并触发更新
        mat_count = 0
        for mat in bpy.data.materials:
            if mat.use_nodes and mat.node_tree:
                for node in mat.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        if node.image.name not in reloaded_images:
                            try:
                                node.image.reload()
                                reloaded_images.add(node.image.name)
                            except Exception:
                                pass
            mat.update_tag()
            if hasattr(mat, 'preview_ensure'):
                mat.preview_ensure()
            if mat.preview:
                try:
                    mat.preview.reload()
                except Exception:
                    pass
            mat_count += 1

        # 3. 强制视图层和所有 3D 视图/图像/着色器编辑器重绘
        if context.view_layer:
            context.view_layer.update()
            
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type in {'VIEW_3D', 'IMAGE_EDITOR', 'NODE_EDITOR', 'PROPERTIES'}:
                    area.tag_redraw()

        msg = f"已成功刷新 {len(reloaded_images)} 张贴图与 {mat_count} 个材质预览"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class MATERIAL_PT_TemplateLibraryPopover(bpy.types.Panel):
    """材质模板库气泡弹窗，支持单项选择与删除"""
    bl_idname = "MATERIAL_PT_TemplateLibraryPopover"
    bl_label = "材质模板库"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_ui_units_x = 12

    def draw(self, context):
        layout = self.layout
        templates = sorted(MaterialTemplateManager.get_template_names())
        
        if not templates:
            layout.label(text="(本地模板库为空，请点击 + 保存)", icon='INFO')
            return

        col = layout.column(align=True)
        for name in templates:
            row = col.row(align=True)
            mat = bpy.data.materials.get(name)
            prev = mat.preview_ensure() if (mat and hasattr(mat, 'preview_ensure')) else (mat.preview if mat else None)
            icon_val = prev.icon_id if prev else 0
            
            # 左侧载入按钮（占满剩余宽度）
            if icon_val:
                op = row.operator("material.load_template_to_source", text=f"★ {name}", icon_value=icon_val)
            else:
                op = row.operator("material.load_template_to_source", text=f"★ {name}", icon='MATERIAL')
            op.template_name = name

            # 右侧删除 X 按钮（无边框紧凑对齐）
            del_op = row.operator("material.delete_source_template", text="", icon='X', emboss=False)
            del_op.template_name = name


class MATERIAL_MT_TemplateLibraryMenu(bpy.types.Menu):
    bl_idname = "MATERIAL_MT_TemplateLibraryMenu"
    bl_label = "材质模板库"

    def draw(self, context):
        layout = self.layout
        templates = sorted(MaterialTemplateManager.get_template_names())
        if not templates:
            layout.label(text="(本地模板库为空，请点击 + 保存)", icon='INFO')
            return

        for name in templates:
            row = layout.row(align=True)
            mat = bpy.data.materials.get(name)
            icon_val = mat.preview.icon_id if (mat and mat.preview) else 0
            if icon_val:
                op = row.operator("material.load_template_to_source", text=f"★ {name}", icon_value=icon_val)
            else:
                op = row.operator("material.load_template_to_source", text=f"★ {name}", icon='MATERIAL')
            op.template_name = name


class MATERIAL_OT_SyncShaderData(Operator):
    bl_idname = "material.sync_shader_pro"
    bl_label = "同步材质数据"
    bl_options = {'REGISTER', 'UNDO'}

    target_material_name: StringProperty(default="")  # 目标材质名称属性
    backup_data = {}  # 类变量存储备份数据

    @staticmethod
    def _clean_node_name(name):
        while name.startswith('SYNC_'):
            name = name[5:]
        return name

    def backup_material(self, material):
        """备份指定材质的链接关系和节点信息，并持久化写入材质自定义属性中"""
        if not material or not material.use_nodes or not material.node_tree:
            return

        # 若已经存在备份（说明正处于预览同步状态），绝不覆盖原始备份
        if material.name in self.backup_data or "um_sync_backup" in material:
            return
        
        backup = {
            'links': [],
            'use_nodes': material.use_nodes,
            'original_nodes': [node.name for node in material.node_tree.nodes if not node.name.startswith('SYNC_') and not node.get('um_synced_node', False)]
        }
        
        for link in material.node_tree.links:
            # 仅备份非 SYNC 原始节点之间的链接
            if not link.from_node.name.startswith('SYNC_') and not link.to_node.name.startswith('SYNC_'):
                backup['links'].append({
                    'from_node': link.from_node.name,
                    'from_socket': link.from_socket.identifier,
                    'to_node': link.to_node.name,
                    'to_socket': link.to_socket.identifier
                })
        
        self.backup_data[material.name] = backup
        try:
            material["um_sync_backup"] = json.dumps(backup)
        except Exception:
            pass

    def execute(self, context):
        src_mat = context.scene.um_source_material
        if not src_mat:
            self.report({'WARNING'}, "请先选择源材质")
            return {'CANCELLED'}

        if not src_mat.use_nodes or not src_mat.node_tree:
            self.report({'WARNING'}, f"源材质 {src_mat.name} 未启用节点")
            return {'CANCELLED'}

        # 收集需要同步的目标材质
        target_mats = set()
        for obj in context.selected_objects:
            if obj.type == 'MESH':
                for slot in obj.material_slots:
                    t_mat = slot.material
                    if t_mat and t_mat != src_mat:
                        if not self.target_material_name or t_mat.name == self.target_material_name:
                            target_mats.add(t_mat)

        if not target_mats:
            self.report({'WARNING'}, "未找到需要同步的目标材质")
            return {'CANCELLED'}

        # 1. 备份原始材质状态
        for t_mat in target_mats:
            self.backup_material(t_mat)

        # 2. 收集源材质中要复制的真实原始节点（过滤掉所有 SYNC_ 节点和输出节点，防止级联复制）
        src_nodes_to_copy = [
            n for n in src_mat.node_tree.nodes 
            if n.type != 'OUTPUT_MATERIAL' 
            and not n.name.startswith('SYNC_') 
            and not n.get('um_synced_node', False)
        ]

        if not src_nodes_to_copy:
            self.report({'WARNING'}, f"源材质 {src_mat.name} 没有可同步的着色器节点")
            return {'CANCELLED'}

        processed = 0
        for target_mat in target_mats:
            try:
                target_mat.use_nodes = True
                if not target_mat.node_tree:
                    target_mat.node_tree = bpy.data.node_groups.new("NodeTree", 'ShaderNodeTree')

                # 找到目标材质的输出节点
                output_node = next((n for n in target_mat.node_tree.nodes if n.type == 'OUTPUT_MATERIAL'), None)
                if not output_node:
                    output_node = target_mat.node_tree.nodes.new('ShaderNodeOutputMaterial')
                    output_node.location = (300, 0)

                # 清理目标材质中残留的任何历史 SYNC_ 节点
                for node in list(target_mat.node_tree.nodes):
                    if node.name.startswith('SYNC_') or node.get('um_synced_node', False) or 'SYNC_' in node.name:
                        if node != output_node:
                            target_mat.node_tree.nodes.remove(node)

                # 断开输出节点的输入链接
                for link in list(target_mat.node_tree.links):
                    if link.to_node == output_node:
                        target_mat.node_tree.links.remove(link)

                # 复制源材质的着色器节点到目标材质
                node_mapping = {}
                for src_node in src_nodes_to_copy:
                    new_node = target_mat.node_tree.nodes.new(src_node.bl_idname)
                    base_name = self._clean_node_name(src_node.name)
                    new_node.name = f"SYNC_{base_name}"
                    new_node['um_synced_node'] = True
                    new_node.location = src_node.location
                    node_mapping[src_node.name] = new_node

                    # 复制可写属性
                    for prop in src_node.bl_rna.properties:
                        if prop.is_readonly or prop.identifier in ['inputs', 'outputs']:
                            continue
                        try:
                            setattr(new_node, prop.identifier, getattr(src_node, prop.identifier))
                        except Exception:
                            pass

                    # 复制输入值
                    for src_input in src_node.inputs:
                        dst_input = new_node.inputs.get(src_input.identifier)
                        if dst_input and hasattr(src_input, 'default_value'):
                            try:
                                if hasattr(dst_input, 'default_value'):
                                    dst_input.default_value = src_input.default_value
                            except Exception:
                                pass

                # 复制节点之间的连线关系
                for link in src_mat.node_tree.links:
                    if (link.from_node.type == 'OUTPUT_MATERIAL' or link.to_node.type == 'OUTPUT_MATERIAL' or
                        link.from_node.name.startswith('SYNC_') or link.to_node.name.startswith('SYNC_')):
                        continue

                    from_node = node_mapping.get(link.from_node.name)
                    to_node = node_mapping.get(link.to_node.name)
                    if from_node and to_node:
                        from_socket = from_node.outputs.get(link.from_socket.identifier)
                        to_socket = to_node.inputs.get(link.to_socket.identifier)
                        if from_socket and to_socket:
                            try:
                                target_mat.node_tree.links.new(from_socket, to_socket)
                            except Exception:
                                pass

                # 寻找源材质中连接到输出的节点
                src_final_node = None
                for link in src_mat.node_tree.links:
                    if link.to_node.type == 'OUTPUT_MATERIAL':
                        src_final_node = link.from_node
                        break

                if not src_final_node and src_nodes_to_copy:
                    src_final_node = next((n for n in src_nodes_to_copy if n.type == 'BSDF_PRINCIPLED'), src_nodes_to_copy[0])

                if src_final_node:
                    target_final_node = node_mapping.get(src_final_node.name)
                    if target_final_node and target_final_node.outputs:
                        surface_input = output_node.inputs.get('Surface')
                        if surface_input:
                            target_mat.node_tree.links.new(target_final_node.outputs[0], surface_input)

                target_mat.node_tree.update_tag()
                processed += 1

            except Exception as e:
                self.report({'ERROR'}, f"同步材质 {target_mat.name} 失败: {str(e)}")
                import traceback
                traceback.print_exc()

        context.view_layer.update()
        self.report({'INFO'}, f"成功同步 {processed} 个材质")
        return {'FINISHED'}


class MATERIAL_OT_RevertShaderData(Operator):
    bl_idname = "material.revert_shader_pro"
    bl_label = "恢复材质数据"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def restore_material(cls, material, data=None):
        """恢复材质的原始链接关系并删除所有复制的临时节点，清理孤立节点"""
        if not material or not material.use_nodes or not material.node_tree:
            return

        if not data:
            data = MATERIAL_OT_SyncShaderData.backup_data.get(material.name)
        if not data and "um_sync_backup" in material:
            try:
                data = json.loads(material["um_sync_backup"])
            except Exception:
                data = None

        try:
            output_node = next((n for n in material.node_tree.nodes if n.type == 'OUTPUT_MATERIAL'), None)
            if not output_node:
                return

            # 断开输出节点的所有链接
            for link in list(material.node_tree.links):
                if link.to_node == output_node:
                    material.node_tree.links.remove(link)

            # 彻底删除所有以 SYNC_ 开头或标记为 um_synced_node 的节点
            for node in list(material.node_tree.nodes):
                if node.name.startswith('SYNC_') or node.get('um_synced_node', False) or 'SYNC_' in node.name:
                    material.node_tree.nodes.remove(node)

            # 如果有备份数据，精确恢复原始连线
            if data and 'links' in data:
                for link_data in data['links']:
                    from_node = material.node_tree.nodes.get(link_data.get('from_node'))
                    to_node = material.node_tree.nodes.get(link_data.get('to_node'))
                    if from_node and to_node:
                        from_socket = from_node.outputs.get(link_data.get('from_socket'))
                        to_socket = to_node.inputs.get(link_data.get('to_socket'))
                        if from_socket and to_socket:
                            try:
                                material.node_tree.links.new(from_socket, to_socket)
                            except Exception:
                                pass

                # 智能清理之前历史遗留的、不在原始备份中且没有任何连线的孤立无用 BSDF 节点
                orig_names = set(data.get('original_nodes', []))
                if orig_names:
                    for node in list(material.node_tree.nodes):
                        if node != output_node and node.name not in orig_names:
                            if not any(out.is_linked for out in node.outputs):
                                material.node_tree.nodes.remove(node)
            else:
                # 容错：将现存的主着色器重新连上输出节点
                orig_bsdf = next((n for n in material.node_tree.nodes if n.type != 'OUTPUT_MATERIAL' and len(n.outputs) > 0), None)
                if orig_bsdf and output_node.inputs.get('Surface'):
                    material.node_tree.links.new(orig_bsdf.outputs[0], output_node.inputs['Surface'])

            material.node_tree.update_tag()

            # 清理持久化属性和内存标记
            if "um_sync_backup" in material:
                del material["um_sync_backup"]
            MATERIAL_OT_SyncShaderData.backup_data.pop(material.name, None)
            MATERIAL_OT_ToggleSyncRevert.sync_states.pop(material.name, None)
        except Exception as e:
            print(f"恢复材质 {material.name} 失败: {e}")

    def execute(self, context):
        mats_to_revert = set()
        for mat_name in MATERIAL_OT_SyncShaderData.backup_data:
            if mat := bpy.data.materials.get(mat_name):
                mats_to_revert.add(mat)
        for mat in bpy.data.materials:
            if "um_sync_backup" in mat or (mat.use_nodes and mat.node_tree and any(n.name.startswith("SYNC_") for n in mat.node_tree.nodes)):
                mats_to_revert.add(mat)

        for mat in mats_to_revert:
            self.restore_material(mat)

        MATERIAL_OT_ToggleSyncRevert.global_sync_state = False
        MATERIAL_OT_ToggleSyncRevert.sync_states.clear()
        self.report({'INFO'}, f"已恢复 {len(mats_to_revert)} 个材质数据")
        return {'FINISHED'}

class MATERIAL_OT_ToggleSyncRevert(Operator):
    """切换同步/恢复材质数据操作符
    第一次点击执行同步操作，再次点击执行恢复操作，实现循环切换
    """
    bl_idname = "material.toggle_sync_revert_pro"
    bl_label = "切换同步/恢复"
    bl_options = {'REGISTER', 'UNDO'}

    mat_name: StringProperty()

    sync_states = {}
    global_sync_state = False

    def execute(self, context):
        try:
            if MATERIAL_OT_ToggleSyncRevert.global_sync_state:
                return {'CANCELLED'}

            mat_name = self.mat_name
            props = getattr(context.scene, 'um_props', None)
            if not mat_name:
                if props and props.material_collection and 0 <= props.material_list_index < len(props.material_collection):
                    item = props.material_collection[props.material_list_index]
                    if item.material:
                        mat_name = item.material.name
                if not mat_name and props and getattr(props, 'selected_material_name', ''):
                    mat_name = props.selected_material_name
                if not mat_name and context.active_object and context.active_object.type == 'MESH':
                    if context.active_object.active_material:
                        mat_name = context.active_object.active_material.name

            if not mat_name:
                self.report({'WARNING'}, "请先选择要操作的材质")
                return {'CANCELLED'}

            mat = bpy.data.materials.get(mat_name)
            is_synced = self.sync_states.get(mat_name, False) or (mat and "um_sync_backup" in mat) or (mat and mat.use_nodes and mat.node_tree and any(n.name.startswith("SYNC_") for n in mat.node_tree.nodes))

            if not is_synced:
                result = bpy.ops.material.sync_shader_pro(target_material_name=mat_name)
                if result == {'FINISHED'}:
                    self.sync_states[mat_name] = True
                    self.report({'INFO'}, f"已同步材质: {mat_name}")
                    return {'FINISHED'}
                else:
                    self.report({'ERROR'}, "同步操作失败")
                    return {'CANCELLED'}
            else:
                result = bpy.ops.material.revert_single_shader_pro(mat_name=mat_name)
                if result == {'FINISHED'}:
                    self.sync_states.pop(mat_name, None)
                    self.report({'INFO'}, f"已恢复材质: {mat_name}")
                    return {'FINISHED'}
                else:
                    self.report({'ERROR'}, "恢复操作失败")
                    return {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, f"切换操作失败: {str(e)}")
            return {'CANCELLED'}
        finally:
            if context.area:
                context.area.tag_redraw()


class MATERIAL_OT_GlobalToggleSyncRevert(Operator):
    """全局切换同步/恢复材质数据操作符
    第一次点击执行全局同步操作，再次点击执行全局恢复操作，实现循环切换
    """
    bl_idname = "material.global_toggle_sync_revert"
    bl_label = "全局切换同步/恢复"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            current_state = MATERIAL_OT_ToggleSyncRevert.global_sync_state
            if not current_state:
                if len(MATERIAL_OT_ToggleSyncRevert.sync_states) > 0:
                    return {'CANCELLED'}

                result = bpy.ops.material.sync_shader_pro(target_material_name="")
                if result == {'FINISHED'}:
                    MATERIAL_OT_ToggleSyncRevert.global_sync_state = True
                    self.report({'INFO'}, "已执行全局同步操作")
                    return {'FINISHED'}
            else:
                result = bpy.ops.material.revert_shader_pro()
                if result == {'FINISHED'}:
                    MATERIAL_OT_ToggleSyncRevert.global_sync_state = False
                    self.report({'INFO'}, "已执行全局恢复操作")
                    return {'FINISHED'}

            return {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, f"全局切换操作失败: {str(e)}")
            return {'CANCELLED'}
        finally:
            if context.area:
                context.area.tag_redraw()


class MATERIAL_OT_RevertSingleShaderData(Operator):
    bl_idname = "material.revert_single_shader_pro"
    bl_label = "恢复当前材质数据"
    bl_options = {'REGISTER', 'UNDO'}

    mat_name: StringProperty()

    def execute(self, context):
        mat = bpy.data.materials.get(self.mat_name)
        if not mat:
            self.report({'WARNING'}, "材质不存在")
            return {'CANCELLED'}

        MATERIAL_OT_RevertShaderData.restore_material(mat)
        self.report({'INFO'}, f"已恢复材质: {mat.name}")
        return {'FINISHED'}

class MATERIAL_OT_Rename(Operator):
    bl_idname = "material.rename_pro"
    bl_label = "重命名材质"
    bl_options = {'REGISTER', 'UNDO'}
    
    material_name: StringProperty()
    new_name: StringProperty(name="新名称")

    def execute(self, context):
        if mat := bpy.data.materials.get(self.material_name):
            mat.name = self.new_name
        return {'FINISHED'}
    
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

class MATERIAL_OT_HandleClick(Operator):
    bl_idname = "material.handle_click_pro"
    bl_label = "材质操作"
    bl_options = {'REGISTER', 'UNDO'}
    
    mat_name: StringProperty()
    
    def execute(self, context):
        state = StateManager.get()
        current_time = time.time()
        props = context.scene.um_props
        props.selected_material_name = self.mat_name
        for idx, item in enumerate(props.material_collection):
            if item.material and item.material.name == self.mat_name:
                props.material_list_index = idx
                break

        if state.pending_click:
            bpy.app.timers.unregister(state.pending_click)
            state.pending_click = None
        
        # 检查双击条件
        if (current_time - state.last_click["mat"] < 0.15 and 
            self.mat_name == state.last_target["mat"]):
            # 执行双击操作（重命名）
            bpy.ops.material.rename_pro('INVOKE_DEFAULT', material_name=self.mat_name)
            state.last_click["mat"] = 0
            state.last_target["mat"] = ""
            return {'FINISHED'}
        else:
            # 记录当前点击并设置延迟单击
            state.last_click["mat"] = current_time
            state.last_target["mat"] = self.mat_name
            
            # 定义延迟单击函数
            def delayed_click():
                current_mat = state.last_target["mat"]
                # 检查是否仍为同一材质且超过延迟时间
                if current_mat and (time.time() - state.last_click["mat"] >= 0.15):
                    if context.mode == 'EDIT_MESH':
                        bpy.ops.material.select_faces_pro(mat_name=current_mat)
                    else:
                        select_material_objects_by_name(current_mat)
                    state.last_click["mat"] = 0
                    state.last_target["mat"] = ""
                state.pending_click = None
                return None
            
            # 注册定时器
            state.pending_click = delayed_click
            bpy.app.timers.register(delayed_click, first_interval=0.15)
            return {'FINISHED'}

class MATERIAL_OT_SelectFaces(Operator):
    bl_idname = "material.select_faces_pro"
    bl_label = "选择材质面"
    bl_options = {'REGISTER', 'UNDO'}

    mat_name: StringProperty()

    @classmethod
    def poll(cls, context):
        return context.mode in {'EDIT_MESH', 'OBJECT'} and len(context.selected_objects) > 0

    def execute(self, context):
        target_material = bpy.data.materials.get(self.mat_name)
        if not target_material:
            self.report({'WARNING'}, f"材质 '{self.mat_name}' 不存在")
            return {'CANCELLED'}

        # 记录初始模式以恢复状态
        original_mode = context.mode
        processed_objs = 0
        total_faces = 0

        # 遍历所有选中且可编辑的网格物体
        for obj in context.selected_objects:
            if obj.type != 'MESH' or not obj.visible_get():
                continue

            # 确保材质存在于该物体
            material_indices = [
                idx for idx, slot in enumerate(obj.material_slots) 
                if slot.material == target_material
            ]
            if not material_indices:
                continue

            # 进入编辑模式
            bpy.context.view_layer.objects.active = obj
            if original_mode != 'EDIT_MESH':
                bpy.ops.object.mode_set(mode='EDIT')

            # 获取bmesh数据
            bm = bmesh.from_edit_mesh(obj.data)
            bm.faces.ensure_lookup_table()

            # 清除当前选择（仅在首次处理时）
            if processed_objs == 0:
                bpy.ops.mesh.select_all(action='DESELECT')

            # 选择对应材质面
            for face in bm.faces:
                if face.material_index in material_indices:
                    face.select = True
                    total_faces += 1

            # 更新网格数据
            bmesh.update_edit_mesh(obj.data)
            processed_objs += 1

        # 恢复原始模式
        if original_mode == 'OBJECT' and processed_objs > 0:
            bpy.ops.object.mode_set(mode='OBJECT')

        self.report({'INFO'}, 
            f"在 {processed_objs} 个物体中选中 {total_faces} 个面")
        return {'FINISHED'}

class MATERIAL_OT_AssignToSelectedFaces(Operator):
    """将材质赋予选中的面或物体"""
    bl_idname = "material.assign_to_selected_faces_pro"
    bl_label = "赋予材质"
    bl_description = "将当前选中的材质赋予选中的物体或网格面"
    bl_options = {'REGISTER', 'UNDO'}

    mat_name: StringProperty(default="")

    @classmethod
    def poll(cls, context):
        return context.mode in {'OBJECT', 'EDIT_MESH'} and len(context.selected_objects) > 0

    def execute(self, context):
        mat_name = self.mat_name
        props = getattr(context.scene, 'um_props', None)
        if not mat_name:
            if props and props.material_collection and 0 <= props.material_list_index < len(props.material_collection):
                item = props.material_collection[props.material_list_index]
                if item.material:
                    mat_name = item.material.name
            if not mat_name and props and getattr(props, 'selected_material_name', ''):
                mat_name = props.selected_material_name
            if not mat_name and context.active_object and context.active_object.type == 'MESH':
                if context.active_object.active_material:
                    mat_name = context.active_object.active_material.name

        if not mat_name:
            self.report({'WARNING'}, "请先选择要赋予的材质")
            return {'CANCELLED'}

        target_material = bpy.data.materials.get(mat_name)
        if not target_material:
            self.report({'WARNING'}, f"材质 '{mat_name}' 不存在")
            return {'CANCELLED'}

        processed_objs = 0

        for obj in context.selected_objects:
            if obj.type != 'MESH' or not obj.visible_get():
                continue

            # 确保材质槽中包含目标材质
            if target_material.name not in [mat.name for mat in obj.data.materials if mat]:
                obj.data.materials.append(target_material)

            # 获取材质索引
            mat_index = obj.data.materials.find(target_material.name)

            if context.mode == 'EDIT_MESH':
                # 编辑模式下赋予选中的面
                bpy.context.view_layer.objects.active = obj
                bpy.ops.object.mode_set(mode='EDIT')

                bm = bmesh.from_edit_mesh(obj.data)
                for face in bm.faces:
                    if face.select:
                        face.material_index = mat_index

                bmesh.update_edit_mesh(obj.data)
            else:
                # 物体模式下赋予整个物体
                for poly in obj.data.polygons:
                    poly.material_index = mat_index

            processed_objs += 1

        self.report({'INFO'}, f"材质 '{mat_name}' 已赋予 {processed_objs} 个物体")
        return {'FINISHED'}


class MATERIAL_OT_ItemActionsPopup(Operator):
    """材质操作菜单"""
    bl_idname = "material.item_actions_popup"
    bl_label = "材质操作"
    
    mat_name: StringProperty(default="")
    
    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=120)
        
    def execute(self, context):
        return {'FINISHED'}
        
    def draw(self, context):
        layout = self.layout
        mat = bpy.data.materials.get(self.mat_name)
        if not mat:
            return
            
        # 使用 4 列等宽网格流布局，确保 4 个按钮宽度比例严格一致 (1:1:1:1)
        grid = layout.grid_flow(row_major=True, columns=4, even_columns=True, even_rows=True, align=True)
        
        # 1. 赋予材质按钮
        assign_btn = grid.operator("material.assign_to_selected_faces_pro", text="", icon='FORWARD')
        assign_btn.mat_name = self.mat_name
        
        # 2. 颜色选择器
        has_color = False
        if mat.use_nodes and mat.node_tree:
            principled = None
            output = next((n for n in mat.node_tree.nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output), None)
            if not output:
                output = next((n for n in mat.node_tree.nodes if n.type == 'OUTPUT_MATERIAL'), None)
            
            if output and 'Surface' in output.inputs and output.inputs['Surface'].is_linked:
                linked_node = output.inputs['Surface'].links[0].from_node
                if linked_node.type == 'BSDF_PRINCIPLED':
                    principled = linked_node
                    
            if not principled:
                principled = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
                
            if principled:
                color_input = principled.inputs.get('Base Color')
                if color_input:
                    grid.prop(color_input, "default_value", text="")
                    has_color = True
                    
        if not has_color:
            grid.label(text="", icon='BLANK1')

        # 3. 切换同步/恢复按钮
        is_synced = MATERIAL_OT_ToggleSyncRevert.sync_states.get(mat.name, False) or (mat and "um_sync_backup" in mat) or (mat and mat.use_nodes and mat.node_tree and any(n.name.startswith("SYNC_") for n in mat.node_tree.nodes))
        toggle_icon = 'LOOP_BACK' if is_synced else 'FILE_REFRESH'
        global_sync_state = getattr(context.scene.um_props, "global_sync_active", False)
        
        toggle_sub = grid.row(align=True)
        toggle_sub.enabled = not global_sync_state
        toggle_btn = toggle_sub.operator("material.toggle_sync_revert_pro", text="", icon=toggle_icon)
        toggle_btn.mat_name = self.mat_name
        
        # 4. 删除按钮
        delete_btn = grid.operator("material.delete_pro", text="", icon='TRASH')
        delete_btn.mat_name = self.mat_name

class MATERIAL_UL_CustomList(bpy.types.UIList):
    """自定义材质列表UI组件"""
    bl_idname = "MATERIAL_UL_CustomListPro"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        mat = item.material
        um_props = context.scene.um_props
        is_selected = mat and (mat.name in um_props.selected_material_names.split(";")) if um_props.edit_mode_selection else False

        row = layout.row(align=True)

        # 1. 预览图标（自动调度后台渲染并实时刷新）
        prev = mat.preview_ensure() if (mat and hasattr(mat, 'preview_ensure')) else (mat.preview if mat else None)
        if prev and prev.icon_id:
            row.template_icon(prev.icon_id, scale=1.0)
        else:
            row.label(icon='MATERIAL', text="")

        template_names = MaterialTemplateManager.get_template_names()
        is_template = mat and (mat.name in template_names)
        display_name = f"★ {mat.name}" if is_template else (mat.name if mat else "")

        # 2. 材质名称（自适应占满整行全部剩余空间，点击选中，双击重命名）
        name_sub = row.row(align=True)
        name_sub.active = is_selected
        op = name_sub.operator("material.handle_click_pro", text=display_name, emboss=False)
        op.mat_name = mat.name if mat else ""


class MATERIAL_OT_UpdateList(Operator):
    """更新材质列表的Operator"""
    bl_idname = "material.update_list_pro"
    bl_label = "更新材质列表"
    
    def execute(self, context):
        props = context.scene.um_props
        props.material_collection.clear()
        
        materials = set()
        for obj in context.selected_objects:
            if obj.type == 'MESH':
                for slot in obj.material_slots:
                    if slot.material:
                        materials.add(slot.material)
        
        for mat in sorted(materials, key=lambda x: x.name):
            item = props.material_collection.add()
            item.material = mat
            
        return {'FINISHED'}

# 注册函数
def register():
    bpy.utils.register_class(MATERIAL_OT_ApplyMaterial)
    bpy.utils.register_class(MATERIAL_OT_DeleteMaterial)
    bpy.utils.register_class(MATERIAL_OT_CreateNew)
    bpy.utils.register_class(MATERIAL_OT_SaveSourceTemplate)
    bpy.utils.register_class(MATERIAL_OT_DeleteSourceTemplate)
    bpy.utils.register_class(MATERIAL_OT_LoadTemplateToSource)
    bpy.utils.register_class(MATERIAL_OT_ExportTemplateLibrary)
    bpy.utils.register_class(MATERIAL_OT_ImportTemplateLibrary)
    bpy.utils.register_class(MATERIAL_OT_OpenTemplateLibraryFolder)
    bpy.utils.register_class(MATERIAL_OT_ReloadAllTextures)
    bpy.utils.register_class(MATERIAL_MT_TemplateLibraryMenu)
    bpy.utils.register_class(MATERIAL_PT_TemplateLibraryPopover)
    bpy.utils.register_class(MATERIAL_OT_SyncShaderData)
    bpy.utils.register_class(MATERIAL_OT_RevertShaderData)
    bpy.utils.register_class(MATERIAL_OT_RevertSingleShaderData)
    bpy.utils.register_class(MATERIAL_OT_ToggleSyncRevert)
    bpy.utils.register_class(MATERIAL_OT_GlobalToggleSyncRevert)
    bpy.utils.register_class(MATERIAL_OT_Rename)
    bpy.utils.register_class(MATERIAL_OT_HandleClick)
    bpy.utils.register_class(MATERIAL_OT_SelectFaces)
    bpy.utils.register_class(MATERIAL_OT_AssignToSelectedFaces)
    bpy.utils.register_class(MATERIAL_OT_ItemActionsPopup)
    bpy.utils.register_class(MATERIAL_UL_CustomList)
    bpy.utils.register_class(MATERIAL_OT_UpdateList)

# 注销函数
def unregister():
    bpy.utils.unregister_class(MATERIAL_OT_UpdateList)
    bpy.utils.unregister_class(MATERIAL_UL_CustomList)
    bpy.utils.unregister_class(MATERIAL_OT_ItemActionsPopup)
    bpy.utils.unregister_class(MATERIAL_OT_AssignToSelectedFaces)
    bpy.utils.unregister_class(MATERIAL_OT_SelectFaces)
    bpy.utils.unregister_class(MATERIAL_OT_HandleClick)
    bpy.utils.unregister_class(MATERIAL_OT_Rename)
    bpy.utils.unregister_class(MATERIAL_OT_GlobalToggleSyncRevert)
    bpy.utils.unregister_class(MATERIAL_OT_ToggleSyncRevert)
    bpy.utils.unregister_class(MATERIAL_OT_RevertSingleShaderData)
    bpy.utils.unregister_class(MATERIAL_OT_RevertShaderData)
    bpy.utils.unregister_class(MATERIAL_OT_SyncShaderData)
    bpy.utils.unregister_class(MATERIAL_PT_TemplateLibraryPopover)
    bpy.utils.unregister_class(MATERIAL_MT_TemplateLibraryMenu)
    bpy.utils.unregister_class(MATERIAL_OT_ReloadAllTextures)
    bpy.utils.unregister_class(MATERIAL_OT_OpenTemplateLibraryFolder)
    bpy.utils.unregister_class(MATERIAL_OT_ImportTemplateLibrary)
    bpy.utils.unregister_class(MATERIAL_OT_ExportTemplateLibrary)
    bpy.utils.unregister_class(MATERIAL_OT_LoadTemplateToSource)
    bpy.utils.unregister_class(MATERIAL_OT_DeleteSourceTemplate)
    bpy.utils.unregister_class(MATERIAL_OT_SaveSourceTemplate)
    bpy.utils.unregister_class(MATERIAL_OT_CreateNew)
    bpy.utils.unregister_class(MATERIAL_OT_DeleteMaterial)
    bpy.utils.unregister_class(MATERIAL_OT_ApplyMaterial)