"""Regression checks for current-view grayscale Alpha generation."""

from pathlib import Path

import bpy
from mathutils import Quaternion, Vector

from uv_material_manager.alpha_generator import (
    _render_depth_alpha,
    _unique_output_path,
)


def test_alpha_operator_registered(ctx):
    assert hasattr(bpy.ops.uvmm, "generate_alpha")
    assert hasattr(bpy.ops.uvmm, "select_alpha_output_directory")
    assert hasattr(bpy.types, "VIEW3D_PT_UVMMAlphaSettings")


def test_depth_alpha_is_grayscale_near_white_far_black(ctx):
    mesh = bpy.data.meshes.new("Alpha Depth Ramp Mesh")
    mesh.from_pydata(
        ((-1.0, -1.0, 1.0), (1.0, -1.0, -1.0),
         (1.0, 1.0, -1.0), (-1.0, 1.0, 1.0)),
        (),
        ((0, 1, 2, 3),),
    )
    mesh.update()
    obj = bpy.data.objects.new("Alpha Depth Ramp", mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = (4.0, -3.0, 2.0)
    obj.rotation_euler[1] = 0.4
    bpy.context.view_layer.update()
    view_center = obj.matrix_world @ Vector((0.0, 0.0, 0.0))

    output_dir = Path(__file__).resolve().parent / ".alpha_test_output"
    image = None
    output_path = None
    temporary_data_counts = (
        len(bpy.data.scenes), len(bpy.data.objects), len(bpy.data.meshes),
        len(bpy.data.cameras), len(bpy.data.materials), len(bpy.data.worlds),
    )
    scene = bpy.context.scene
    original_scene_state = (
        scene.render.engine,
        scene.render.filepath,
        scene.render.resolution_x,
        scene.render.resolution_y,
        scene.camera,
        bpy.context.view_layer.objects.active,
        tuple(obj.name for obj in bpy.context.selected_objects),
    )
    try:
        image, output_path = _render_depth_alpha(
            obj,
            Quaternion(),
            view_center,
            3.0,
            64,
            output_dir,
        )
        assert output_path.is_file()
        assert tuple(image.size) == (64, 64)
        pixels = image.pixels

        visible_values = [
            pixels[index]
            for index in range(0, len(pixels), 4)
            if pixels[index + 3] > 0.5
        ]
        background_values = [
            pixels[index]
            for index in range(0, len(pixels), 4)
            if pixels[index + 3] <= 0.5
        ]
        assert visible_values
        assert min(visible_values) < 0.2, min(visible_values)
        assert max(visible_values) > 0.8, max(visible_values)
        assert background_values and max(background_values) < 0.02
        assert len({round(value, 4) for value in visible_values}) > 4
        with output_path.open('rb') as image_file:
            image_header = image_file.read(26)
        assert image_header[:8] == b'\x89PNG\r\n\x1a\n'
        assert image_header[24] == 16
        assert image_header[25] == 0
        assert temporary_data_counts == (
            len(bpy.data.scenes), len(bpy.data.objects), len(bpy.data.meshes),
            len(bpy.data.cameras), len(bpy.data.materials), len(bpy.data.worlds),
        )
        assert bpy.context.scene == scene
        assert original_scene_state == (
            scene.render.engine,
            scene.render.filepath,
            scene.render.resolution_x,
            scene.render.resolution_y,
            scene.camera,
            bpy.context.view_layer.objects.active,
            tuple(obj.name for obj in bpy.context.selected_objects),
        )
    finally:
        if image is not None:
            bpy.data.images.remove(image)
        if output_path is not None and output_path.exists():
            output_path.unlink()
        if output_dir.exists():
            output_dir.rmdir()
        bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.meshes.remove(mesh)


def test_output_filename_does_not_overwrite_existing_alpha(ctx):
    directory = Path(__file__).resolve().parent / ".alpha_path_test_output"
    directory.mkdir(parents=True, exist_ok=True)
    first = directory / "Ramp_Alpha.png"
    try:
        first.write_bytes(b"existing")
        result = _unique_output_path(directory, "Ramp")
        assert result.name == "Ramp_Alpha_001.png"
        assert first.read_bytes() == b"existing"
    finally:
        if first.exists():
            first.unlink()
        if directory.exists():
            directory.rmdir()


def test_custom_filename_and_format_are_applied(ctx):
    directory = Path(__file__).resolve().parent / ".alpha_format_test_output"
    try:
        result = _unique_output_path(
            directory, "Ramp", "My Detail.tif", "TIFF"
        )
        assert result.name == "My Detail.tif"
        result.touch()
        next_result = _unique_output_path(
            directory, "Ramp", "My Detail.tif", "TIFF"
        )
        assert next_result.name == "My Detail_001.tif"
    finally:
        if directory.exists():
            for output in directory.iterdir():
                output.unlink()
            directory.rmdir()


def test_output_directory_button_stores_selected_directory(ctx):
    directory = Path(__file__).resolve().parent / ".alpha_directory_test_output"
    directory.mkdir(parents=True, exist_ok=True)
    settings = bpy.context.scene.um_alpha_settings
    original_directory = settings.output_directory
    try:
        result = bpy.ops.uvmm.select_alpha_output_directory(
            dirpath=str(directory)
        )
        assert result == {'FINISHED'}
        assert Path(bpy.path.abspath(settings.output_directory)) == directory
    finally:
        settings.output_directory = original_directory
        directory.rmdir()
