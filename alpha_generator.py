"""Generate a grayscale depth alpha from the active orthographic 3D view."""

from array import array
from pathlib import Path
import re
import uuid

import bpy
import bpy_extras.view3d_utils
from bpy.props import EnumProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup
from mathutils import Matrix, Vector


def _alpha_output_dir(settings):
    configured = settings.output_directory.strip()
    if configured:
        return Path(bpy.path.abspath(configured)).expanduser()
    if bpy.data.filepath:
        return Path(bpy.path.abspath("//Alpha"))
    return Path(bpy.app.tempdir) / "Alpha"


_ALPHA_FILE_FORMATS = {
    'PNG': ('.png', '16'),
    'TIFF': ('.tif', '16'),
    'OPEN_EXR': ('.exr', '32'),
}


def _unique_output_path(directory, object_name, file_name='', file_format='PNG'):
    directory.mkdir(parents=True, exist_ok=True)
    extension = _ALPHA_FILE_FORMATS[file_format][0]
    requested_name = file_name.strip()
    if requested_name:
        base = Path(requested_name).name
        if Path(base).suffix.lower() in {'.png', '.tif', '.tiff', '.exr'}:
            base = Path(base).stem
    else:
        base = f"{object_name}_Alpha"
    safe_name = re.sub(
        r'[<>:"/\\|?*\x00-\x1f]+', '_', base, flags=re.UNICODE
    ).strip(" ._") or "Alpha"
    path = directory / f"{safe_name}{extension}"
    index = 1
    while path.exists():
        path = directory / f"{safe_name}_{index:03d}{extension}"
        index += 1
    return path


def _view_rectangle(region, region_3d):
    center_hint = region_3d.view_location.copy()
    coords = (
        (0.0, 0.0),
        (float(region.width), 0.0),
        (0.0, float(region.height)),
        (float(region.width), float(region.height)),
    )
    points = [
        bpy_extras.view3d_utils.region_2d_to_location_3d(
            region, region_3d, coord, center_hint
        )
        for coord in coords
    ]
    width = (points[1] - points[0]).length
    height = (points[2] - points[0]).length
    if width < 1e-6 or height < 1e-6:
        raise ValueError("无法读取当前视口的正交取景范围")
    center = sum(points, Vector()) / len(points)
    return center, width, height


def _read_depth_image(path, resolution, clip_start, clip_end):
    import numpy as np

    depth_image = bpy.data.images.load(str(path), check_existing=False)
    try:
        if tuple(depth_image.size) != (resolution, resolution):
            raise RuntimeError("Z 深度图尺寸与 Alpha 输出分辨率不一致")

        channel_count = depth_image.channels
        depth_data = array('f', [0.0]) * (resolution * resolution * channel_count)
        depth_image.pixels.foreach_get(depth_data)
        depths = np.frombuffer(depth_data, dtype=np.float32).reshape(
            resolution * resolution,
            channel_count,
        )[:, 0]
        visible_mask = (
            np.isfinite(depths)
            & (depths >= clip_start)
            & (depths < clip_end)
        )
        visible_depths = depths[visible_mask]
        if visible_depths.size == 0:
            raise ValueError("当前视角没有检测到模型的可见表面")
        return (
            depth_image,
            depths,
            visible_mask,
            float(visible_depths.min()),
            float(visible_depths.max()),
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        bpy.data.images.remove(depth_image)
        raise


def _save_depth_alpha(
    temp_scene,
    output_path,
    resolution,
    depth_data,
    visible_mask,
    near_depth,
    far_depth,
    file_format='PNG',
):
    import numpy as np

    depth_range = far_depth - near_depth
    is_flat = depth_range < 1e-6
    if is_flat:
        depth_range = 1.0

    image = bpy.data.images.new(
        name=output_path.stem,
        width=resolution,
        height=resolution,
        alpha=True,
        float_buffer=True,
    )
    pixel_values = np.zeros((resolution * resolution, 4), dtype=np.float32)
    visible_depths = depth_data[visible_mask]
    gray = np.ones_like(visible_depths) if is_flat else 1.0 - (
        (visible_depths - near_depth) / depth_range
    )
    pixel_values[visible_mask, :3] = gray[:, None]
    pixel_values[visible_mask, 3] = 1.0

    try:
        image.colorspace_settings.name = 'Non-Color'
        image.pixels.foreach_set(pixel_values.ravel())
        image.update()
        image.filepath_raw = str(output_path)
        image.file_format = file_format

        image_settings = temp_scene.render.image_settings
        image_settings.file_format = file_format
        image_settings.color_mode = 'BW'
        image_settings.color_depth = _ALPHA_FILE_FORMATS[file_format][1]
        if file_format == 'PNG':
            image_settings.compression = 15
        image.save_render(str(output_path), scene=temp_scene)
    except (OSError, RuntimeError, TypeError, ValueError):
        bpy.data.images.remove(image)
        raise
    return image


def _render_depth_alpha(
    source_object,
    view_rotation,
    view_center,
    ortho_scale,
    resolution,
    directory,
    file_name='',
    file_format='PNG',
):
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = source_object.evaluated_get(depsgraph)
    evaluated_mesh = evaluated.to_mesh()
    if evaluated_mesh is None or not evaluated_mesh.vertices or not evaluated_mesh.polygons:
        if evaluated_mesh is not None:
            evaluated.to_mesh_clear()
        raise ValueError("活动物体没有可生成 Alpha 的有效网格")

    mesh_copy = None
    temp_scene = None
    camera_data = None
    camera_object = None
    mesh_object = None
    source_mesh_data = None
    compositor_tree = None
    depth_image = None
    depth_path = None
    output_path = None
    image = None
    try:
        mesh_copy = evaluated_mesh.copy()
        world_matrix = evaluated.matrix_world.copy()
    finally:
        evaluated.to_mesh_clear()

    try:
        mesh_copy.transform(world_matrix)
        mesh_copy.update()
        world_matrix = Matrix.Identity(4)

        view_forward = view_rotation @ Vector((0.0, 0.0, -1.0))
        view_forward.normalize()
        vertex_depths = [
            (vertex.co - view_center).dot(view_forward)
            for vertex in mesh_copy.vertices
        ]
        near_from_center = min(vertex_depths)
        far_from_center = max(vertex_depths)
        depth_range = far_from_center - near_from_center
        margin = max(depth_range * 0.05, 0.001)
        camera_location = view_center + view_forward * (near_from_center - margin)
        far_depth = max(1e-6, depth_range + margin)
        output_path = _unique_output_path(
            directory, source_object.name, file_name, file_format
        )
        depth_basename = f".uvmm_depth_{uuid.uuid4().hex}_"
        depth_path = directory / f"{depth_basename}Z.exr"

        temp_scene = bpy.data.scenes.new("UVMM Alpha Temporary Scene")
        engine_items = bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items
        engine_ids = {item.identifier for item in engine_items}
        temp_scene.render.engine = (
            'BLENDER_EEVEE_NEXT'
            if 'BLENDER_EEVEE_NEXT' in engine_ids
            else 'BLENDER_EEVEE'
        )
        temp_scene.render.resolution_x = resolution
        temp_scene.render.resolution_y = resolution
        temp_scene.render.resolution_percentage = 100
        temp_scene.render.use_compositing = True
        temp_scene.render.dither_intensity = 0.0
        temp_scene.view_settings.view_transform = 'Standard'
        temp_scene.view_settings.look = 'None'

        temp_scene.view_layers[0].use_pass_z = True

        compositor_tree = bpy.data.node_groups.new(
            "UVMM Alpha Depth Compositor",
            "CompositorNodeTree",
        )
        compositor_tree.interface.new_socket(
            name="Image",
            in_out='OUTPUT',
            socket_type="NodeSocketColor",
        )
        temp_scene.compositing_node_group = compositor_tree
        nodes = compositor_tree.nodes
        nodes.clear()
        render_layers = nodes.new("CompositorNodeRLayers")
        render_layers.scene = temp_scene
        render_layers.layer = temp_scene.view_layers[0].name
        group_output = nodes.new("NodeGroupOutput")
        file_output = nodes.new("CompositorNodeOutputFile")
        file_output.directory = str(directory)
        file_output.file_name = depth_basename
        depth_item = file_output.file_output_items.new('FLOAT', "Z")
        file_output.format.media_type = 'IMAGE'
        file_output.format.file_format = 'OPEN_EXR'
        file_output.format.color_mode = 'BW'
        file_output.format.color_depth = '32'
        file_output.save_as_render = False
        compositor_tree.links.new(
            render_layers.outputs['Image'],
            group_output.inputs['Image'],
        )
        compositor_tree.links.new(
            render_layers.outputs['Depth'],
            file_output.inputs[depth_item.name],
        )

        camera_data = bpy.data.cameras.new("UVMM Alpha Camera")
        camera_data.type = 'ORTHO'
        camera_data.ortho_scale = max(ortho_scale, 1e-4)
        camera_data.clip_start = max(min(margin * 0.01, 0.1), 1e-4)
        camera_data.clip_end = max(far_depth + margin, camera_data.clip_start + 1.0)
        camera_object = bpy.data.objects.new("UVMM Alpha Camera", camera_data)
        camera_object.location = camera_location
        camera_object.rotation_mode = 'QUATERNION'
        camera_object.rotation_quaternion = view_rotation
        temp_scene.collection.objects.link(camera_object)
        temp_scene.camera = camera_object

        mesh_object = bpy.data.objects.new("UVMM Alpha Source", mesh_copy)
        source_mesh_data = mesh_copy
        mesh_copy = None
        mesh_object.matrix_world = world_matrix
        temp_scene.collection.objects.link(mesh_object)

        render_result = bpy.ops.render.render(write_still=False, scene=temp_scene.name)
        if 'FINISHED' not in render_result:
            raise RuntimeError("Blender 未能完成 Alpha 渲染")
        if not depth_path.is_file():
            raise RuntimeError("Blender 没有生成唯一的 Z 深度临时图")
        depth_image, depth_data, visible_mask, near_depth, far_depth = _read_depth_image(
            depth_path,
            resolution,
            camera_data.clip_start,
            camera_data.clip_end,
        )
        image = _save_depth_alpha(
            temp_scene,
            output_path,
            resolution,
            depth_data,
            visible_mask,
            near_depth,
            far_depth,
            file_format,
        )
        return image, output_path
    except Exception:
        if output_path is not None and output_path.exists():
            output_path.unlink()
        if image is not None and image.name in bpy.data.images:
            bpy.data.images.remove(image)
        raise
    finally:
        if depth_image is not None and depth_image.name in bpy.data.images:
            bpy.data.images.remove(depth_image)
        if depth_path is not None and depth_path.exists():
            depth_path.unlink()
        if temp_scene is not None:
            bpy.data.scenes.remove(temp_scene)
        if compositor_tree is not None and compositor_tree.users == 0:
            bpy.data.node_groups.remove(compositor_tree)
        if camera_object is not None and camera_object.name in bpy.data.objects:
            bpy.data.objects.remove(camera_object, do_unlink=True)
        if mesh_object is not None and mesh_object.name in bpy.data.objects:
            bpy.data.objects.remove(mesh_object, do_unlink=True)
        if camera_data is not None and camera_data.users == 0:
            bpy.data.cameras.remove(camera_data)
        if source_mesh_data is not None and source_mesh_data.users == 0:
            bpy.data.meshes.remove(source_mesh_data)
        if mesh_copy is not None and mesh_copy.users == 0:
            bpy.data.meshes.remove(mesh_copy)


class UVMM_PG_alpha_settings(PropertyGroup):
    resolution: EnumProperty(
        name="分辨率",
        description="生成 Alpha 图片的宽度和高度",
        items=(
            ('512', "512 × 512", ""),
            ('1024', "1024 × 1024", ""),
            ('2048', "2048 × 2048", ""),
            ('4096', "4096 × 4096", ""),
        ),
        default='2048',
    )
    output_directory: StringProperty(
        name="输出目录",
        description="留空时保存到 .blend 同级的 Alpha 文件夹；未保存的工程保存到 Blender 临时目录",
        subtype='DIR_PATH',
        default="",
    )
    file_name: StringProperty(
        name="文件名",
        description="留空时使用活动物体名称加 Alpha；扩展名由文件格式自动添加",
        default="",
    )
    file_format: EnumProperty(
        name="文件格式",
        description="选择无损灰度 Alpha 图片格式",
        items=(
            ('PNG', "PNG", "16 位灰度，兼容性好"),
            ('TIFF', "TIFF", "16 位灰度"),
            ('OPEN_EXR', "OpenEXR", "32 位浮点灰度"),
        ),
        default='PNG',
    )


class UVMM_OT_SelectAlphaOutputDirectory(Operator):
    bl_idname = "uvmm.select_alpha_output_directory"
    bl_label = "选择 Alpha 输出目录"
    bl_description = "选择生成的 Alpha 图片保存到哪个文件夹"
    bl_options = {'REGISTER', 'UNDO'}

    dirpath: StringProperty(name="输出目录", subtype='DIR_PATH')

    @classmethod
    def poll(cls, context):
        return context.scene is not None and hasattr(
            context.scene, "um_alpha_settings"
        )

    def invoke(self, context, event):
        settings = context.scene.um_alpha_settings
        if settings.output_directory.strip():
            initial_directory = Path(
                bpy.path.abspath(settings.output_directory)
            ).expanduser()
        elif bpy.data.filepath:
            initial_directory = Path(bpy.data.filepath).parent
        else:
            initial_directory = Path(bpy.app.tempdir)
        if not initial_directory.is_dir():
            initial_directory = initial_directory.parent
        self.dirpath = str(initial_directory)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        directory = Path(bpy.path.abspath(self.dirpath)).expanduser()
        if directory.exists() and not directory.is_dir():
            self.report({'ERROR'}, "输出路径不是文件夹")
            return {'CANCELLED'}
        context.scene.um_alpha_settings.output_directory = str(directory)
        return {'FINISHED'}


class VIEW3D_PT_UVMMAlphaSettings(Panel):
    bl_idname = "VIEW3D_PT_UVMMAlphaSettings"
    bl_label = "Alpha 生成设置"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_ui_units_x = 12

    def draw(self, context):
        settings = context.scene.um_alpha_settings
        layout = self.layout
        resolution_row = layout.row(align=True)
        resolution_split = resolution_row.split(factor=0.72, align=True)
        resolution_column = resolution_split.column()
        format_column = resolution_split.column()
        resolution_column.prop(settings, "resolution", text="分辨率")
        format_column.prop(settings, "file_format", text="")

        name_row = layout.row(align=True)
        name_row.prop(settings, "file_name", text="文件名")
        name_row.operator_context = 'INVOKE_DEFAULT'
        name_row.operator(
            "uvmm.select_alpha_output_directory", text="", icon='FILE_FOLDER'
        )


class UVMM_OT_GenerateAlpha(Operator):
    bl_idname = "uvmm.generate_alpha"
    bl_label = "生成 Alpha"
    bl_description = "按当前正交视角生成活动模型的灰度深度图"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (
            context.area is not None
            and context.area.type == 'VIEW_3D'
            and context.active_object is not None
            and context.active_object.type == 'MESH'
        )

    def execute(self, context):
        space = context.space_data
        region_3d = getattr(space, "region_3d", None)
        if region_3d is None or region_3d.view_perspective != 'ORTHO':
            self.report({'WARNING'}, "请先将 3D 视口切换到正交视图，再生成 Alpha")
            return {'CANCELLED'}

        window_region = next(
            (region for region in context.area.regions if region.type == 'WINDOW'),
            None,
        )
        if window_region is None:
            self.report({'WARNING'}, "找不到当前 3D 视口区域")
            return {'CANCELLED'}

        try:
            view_center, view_width, view_height = _view_rectangle(window_region, region_3d)
            settings = context.scene.um_alpha_settings
            resolution = int(settings.resolution)
            output_dir = _alpha_output_dir(settings)
            image, output_path = _render_depth_alpha(
                context.active_object,
                region_3d.view_rotation.copy(),
                view_center,
                max(view_width, view_height),
                resolution,
                output_dir,
                settings.file_name,
                settings.file_format,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.report({'ERROR'}, f"生成 Alpha 失败：{exc}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"已生成 {image.name}：{output_path}")
        return {'FINISHED'}


CLASSES = (
    UVMM_PG_alpha_settings,
    UVMM_OT_SelectAlphaOutputDirectory,
    VIEW3D_PT_UVMMAlphaSettings,
    UVMM_OT_GenerateAlpha,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.um_alpha_settings = bpy.props.PointerProperty(type=UVMM_PG_alpha_settings)


def unregister():
    if hasattr(bpy.types.Scene, "um_alpha_settings"):
        del bpy.types.Scene.um_alpha_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
