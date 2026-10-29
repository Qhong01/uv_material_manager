"""Run with Blender 5.2: blender -b --python tests/test_focus_faces.py."""

import sys
from pathlib import Path

import bmesh
import bpy


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import focus_tools


def faces(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    return bm.faces


focus_tools.register()
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
bpy.ops.mesh.primitive_grid_add(x_subdivisions=3, y_subdivisions=3)
obj = bpy.context.object
bpy.ops.object.mode_set(mode='EDIT')
bpy.context.tool_settings.mesh_select_mode = (False, False, True)
bm = bmesh.from_edit_mesh(obj.data)
bm.faces.ensure_lookup_table()
for face in bm.faces:
    face.select_set(False)
bm.faces[0].select_set(True)
bmesh.update_edit_mesh(obj.data)

area = next(area for area in bpy.context.screen.areas if area.type == 'VIEW_3D')
region = next(region for region in area.regions if region.type == 'WINDOW')
with bpy.context.temp_override(area=area, region=region):
    assert bpy.ops.view3d.uvmm_restore_hidden_level() == {'FINISHED'}
    assert faces(obj)[0].hide
    assert bpy.context.mode == 'EDIT_MESH'

    faces(obj)[1].select_set(True)
    bmesh.update_edit_mesh(obj.data)
    assert bpy.ops.view3d.uvmm_restore_hidden_level() == {'FINISHED'}
    assert faces(obj)[0].hide and faces(obj)[1].hide

    assert bpy.ops.view3d.uvmm_hide_unselected() == {'FINISHED'}
    assert faces(obj)[0].hide and not faces(obj)[1].hide
    assert bpy.ops.view3d.uvmm_hide_unselected() == {'FINISHED'}
    assert not faces(obj)[0].hide

    for face in faces(obj):
        face.select_set(False)
    faces(obj)[0].select_set(True)
    bmesh.update_edit_mesh(obj.data)
    assert bpy.ops.view3d.uvmm_hide_unselected() == {'FINISHED'}
    assert not faces(obj)[0].hide
    assert all(face.hide for face in faces(obj) if face.index != 0)
    for face in faces(obj):
        face.select_set(False)
    bmesh.update_edit_mesh(obj.data)
    assert bpy.ops.view3d.uvmm_restore_hidden_level() == {'FINISHED'}
    assert all(not face.hide for face in faces(obj))

assert len(bpy.context.scene.uvmm_focus_face_stack.levels) == 0
bpy.ops.object.mode_set(mode='OBJECT')
with bpy.context.temp_override(area=area, region=region):
    assert bpy.ops.view3d.uvmm_restore_hidden_level() == {'FINISHED'}
    assert obj.hide_get(view_layer=bpy.context.view_layer)
    assert bpy.ops.view3d.uvmm_hide_unselected() == {'FINISHED'}
    assert not obj.hide_get(view_layer=bpy.context.view_layer)
assert len(bpy.context.scene.uvmm_focus_visibility_stack.levels) == 0
bpy.ops.mesh.primitive_grid_add(x_subdivisions=3, y_subdivisions=3)
other = bpy.context.object
obj.select_set(True)
bpy.context.view_layer.objects.active = obj
bpy.ops.object.mode_set(mode='EDIT')
for edit_obj in (obj, other):
    for face in faces(edit_obj):
        face.select_set(False)
    faces(edit_obj)[0].select_set(True)
    bmesh.update_edit_mesh(edit_obj.data)
with bpy.context.temp_override(area=area, region=region):
    assert bpy.ops.view3d.uvmm_restore_hidden_level() == {'FINISHED'}
    assert faces(obj)[0].hide and faces(other)[0].hide
    assert bpy.ops.view3d.uvmm_hide_unselected() == {'FINISHED'}
    assert not faces(obj)[0].hide and not faces(other)[0].hide
assert len(bpy.context.scene.uvmm_focus_face_stack.levels) == 0
focus_tools.unregister()
print('FOCUS_FACE_TEST_OK')
