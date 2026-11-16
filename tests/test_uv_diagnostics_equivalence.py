# -*- coding: utf-8 -*-
"""UV 诊断颜色与旧版逐三角实现的一致性测试。

新版用 numpy 向量化计算，必须与 scalar 参考实现逐通道一致；
同时验证棋盘格预览改为共享节点组/材质后没有回退。
"""

import unittest

import bpy
import bmesh
from mathutils import Vector

from uv_material_manager.model_checker import diagnostic


def _prefs():
    return bpy.context.preferences.addons["uv_material_manager"].preferences


def _set_mode(stretch=False, orientation=False, checker=False, display_mode='CLASSIC'):
    prefs = _prefs()
    prefs.uv_display_mode = display_mode
    prefs.uv_show_checker = checker
    prefs.uv_show_stretch = stretch
    prefs.uv_show_orientation = orientation


def _prepare(ctx, *, distort=False, flip=False):
    ctx.reset_scene()
    obj = ctx.add_grid(x_segments=6, y_segments=5, size=2.0)
    obj.location = (1.3, -0.7, 0.4)
    obj.rotation_euler = (0.2, 0.1, 0.5)
    obj.scale = (1.0, 1.7, 0.8)
    uv_layer = obj.data.uv_layers.active
    for loop in uv_layer.data:
        u, v = loop.uv
        if distort:
            u = u * u * 2.0
        v *= 0.55
        if flip:
            u = -u
        loop.uv = (u, v)
    return obj


def _scalar_reference(obj, prefs):
    """Original per-triangle implementation, kept here as ground truth."""
    mesh = obj.data
    mesh.calc_loop_triangles()
    uv_layer = mesh.uv_layers.active
    max_stretch = [0.0] * len(mesh.polygons)
    flipped_votes = [0] * len(mesh.polygons)
    triangle_counts = [0] * len(mesh.polygons)

    for triangle in mesh.loop_triangles:
        loop_indices = tuple(triangle.loops)
        coords = tuple(
            obj.matrix_world @ mesh.vertices[mesh.loops[index].vertex_index].co
            for index in loop_indices
        )
        uvs = tuple(Vector(uv_layer.data[index].uv) for index in loop_indices)
        polygon_index = triangle.polygon_index
        max_stretch[polygon_index] = max(
            max_stretch[polygon_index],
            diagnostic._triangle_stretch(coords, uvs),
        )
        uv_a = uvs[1] - uvs[0]
        uv_b = uvs[2] - uvs[0]
        if uv_a.x * uv_b.y - uv_a.y * uv_b.x < -1e-12:
            flipped_votes[polygon_index] += 1
        triangle_counts[polygon_index] += 1

    face_colors = [None] * len(mesh.polygons)
    for polygon in mesh.polygons:
        index = polygon.index
        flipped = (
            triangle_counts[index] > 0
            and flipped_votes[index] * 2 >= triangle_counts[index]
        )
        face_colors[index] = diagnostic._face_base_color(max_stretch[index], flipped, prefs)

    reference = [None] * len(mesh.loops)
    if prefs.uv_show_stretch:
        point_sums = [[0.0, 0.0, 0.0] for _ in mesh.vertices]
        point_counts = [0] * len(mesh.vertices)
        for polygon in mesh.polygons:
            color = face_colors[polygon.index]
            for vertex_index in polygon.vertices:
                for channel in range(3):
                    point_sums[vertex_index][channel] += color[channel]
                point_counts[vertex_index] += 1

        point_colors = []
        for vertex_index, sums in enumerate(point_sums):
            count = point_counts[vertex_index]
            point_colors.append(
                tuple(value / count for value in sums)
                if count
                else (0.38, 0.38, 0.38)
            )
        for loop in mesh.loops:
            reference[loop.index] = (*point_colors[loop.vertex_index], 1.0)
    else:
        for polygon in mesh.polygons:
            base = face_colors[polygon.index]
            for loop_index in polygon.loop_indices:
                reference[loop_index] = (*base, 1.0)
    return reference


def _actual_colors(obj):
    mesh = obj.data
    name = mesh.get("_model_checker_display_attribute")
    assert name, "诊断颜色属性没有创建"
    attribute = mesh.color_attributes.get(name)
    assert attribute is not None
    if attribute.domain == 'POINT':
        return [
            tuple(attribute.data[loop.vertex_index].color)
            for loop in mesh.loops
        ]
    return [tuple(attribute.data[index].color) for index in range(len(mesh.loops))]


def _assert_matches(obj, tolerance=3e-5):
    prefs = _prefs()
    diagnostic._ensure_preview(obj, prefs, force_update=True)
    expected = _scalar_reference(obj, prefs)
    actual = _actual_colors(obj)
    assert len(actual) == len(expected)
    worst = 0.0
    for loop_index, (got, want) in enumerate(zip(actual, expected)):
        for channel in range(4):
            delta = abs(got[channel] - want[channel])
            worst = max(worst, delta)
            assert delta <= tolerance, (
                f"loop {loop_index} channel {channel}: {got[channel]:.6f} != {want[channel]:.6f}"
            )
    return worst


def test_stretch_matches_scalar_reference(ctx):
    _set_mode(stretch=True)
    try:
        obj = _prepare(ctx, distort=True)
        _assert_matches(obj)
    finally:
        _set_mode()


def test_stretch_display_attribute_is_point_domain_and_smooth(ctx):
    _set_mode(stretch=True)
    try:
        obj = _prepare(ctx, distort=True)
        diagnostic._ensure_preview(obj, _prefs(), force_update=True)
        name = obj.data.get("_model_checker_display_attribute")
        attribute = obj.data.color_attributes.get(name)
        assert attribute is not None
        assert attribute.domain == 'POINT'
        point_colors = {
            tuple(round(channel, 4) for channel in item.color[:3])
            for item in attribute.data
        }
        assert len(point_colors) > 2, "拉伸颜色应在相邻点之间形成渐变"
    finally:
        _set_mode()


def test_stretch_switches_corner_attribute_to_point_in_edit_mode(ctx):
    _set_mode(orientation=True)
    obj = _prepare(ctx, distort=True)
    prefs = _prefs()
    try:
        diagnostic._ensure_preview(obj, prefs, force_update=True)
        name = obj.data.get("_model_checker_display_attribute")
        attribute = obj.data.color_attributes.get(name)
        assert attribute is not None and attribute.domain == 'CORNER'

        ctx.activate(obj)
        ctx.set_mode('EDIT')
        prefs.uv_show_orientation = False
        prefs.uv_show_stretch = True
        diagnostic._ensure_preview(obj, prefs, force_update=True)

        bm = bmesh.from_edit_mesh(obj.data)
        point_layer = bm.verts.layers.float_color.get(name)
        assert point_layer is not None, "编辑模式下应创建点域浮点颜色层"
        point_colors = {
            tuple(round(float(channel), 4) for channel in vert[point_layer][:3])
            for vert in bm.verts
        }
        assert len(point_colors) > 2, "编辑模式的拉伸颜色应在顶点间形成渐变"
    finally:
        ctx.object_mode()
        _set_mode()

    attribute = obj.data.color_attributes.get(name)
    assert attribute is not None and attribute.domain == 'POINT'


def test_orientation_matches_scalar_reference(ctx):
    _set_mode(orientation=True)
    try:
        obj = _prepare(ctx, distort=True, flip=True)
        _assert_matches(obj)
    finally:
        _set_mode()


def test_orientation_opacity_changes_display_color(ctx):
    _set_mode(orientation=True)
    prefs = _prefs()
    original_opacity = prefs.uv_orientation_opacity
    try:
        obj = _prepare(ctx, distort=True)
        prefs.uv_orientation_opacity = 1.0
        diagnostic._ensure_preview(obj, prefs, force_update=True)
        name = obj.data.get("_model_checker_display_attribute")
        attribute = obj.data.color_attributes.get(name)
        assert attribute is not None and attribute.domain == 'CORNER'
        full_strength = tuple(attribute.data[0].color)

        prefs.uv_orientation_opacity = 0.25
        diagnostic._ensure_preview(obj, prefs, force_update=False)
        reduced_strength = tuple(attribute.data[0].color)
        assert reduced_strength != full_strength, "正反颜色透明度滑块应改变显示颜色强度"
    finally:
        prefs.uv_orientation_opacity = original_opacity
        _set_mode()


def test_both_features_match_scalar_reference(ctx):
    _set_mode(stretch=True, orientation=True)
    try:
        obj = _prepare(ctx, distort=True, flip=True)
        _assert_matches(obj)
    finally:
        _set_mode()


def test_degenerate_uv_maps_to_full_stretch(ctx):
    _set_mode(stretch=True)
    try:
        obj = _prepare(ctx)
        uv_layer = obj.data.uv_layers.active
        for loop in obj.data.polygons[0].loop_indices:
            uv_layer.data[loop].uv = (0.5, 0.5)
        _assert_matches(obj)
    finally:
        _set_mode()


def test_checker_preview_is_shared(ctx):
    _set_mode(checker=True, display_mode='MATERIAL')
    try:
        ctx.reset_scene()
        objects = [ctx.add_grid(name=f"checker_{index}") for index in range(4)]
        diagnostic.stop_refresh_timer()
        state = bpy.context.scene.model_checker_state
        diagnostic.mark_full_refresh(state)
        interval = diagnostic.refresh_timer()
        assert interval is None, "checker 刷新结束后不应返回 0.25 秒的循环间隔"

        groups = {
            modifier.node_group.name
            for obj in objects
            for modifier in obj.modifiers
            if modifier.type == "NODES" and modifier.node_group is not None
        }
        assert len(groups) == 1, f"期望共享 1 个节点组，实际 {len(groups)}"

        materials = {
            obj.modifiers[0].node_group.nodes["Set Material"].inputs["Material"].default_value.name
            for obj in objects
            if obj.modifiers
        }
        assert len(materials) == 1, f"期望共享 1 个材质，实际 {len(materials)}"

        # 刷新完成后不应再以 0.25s 周期反复重算（watchdog 低频兜底即可）。
        assert bpy.app.timers.is_registered(diagnostic._checker_watchdog)
    finally:
        _set_mode()
        diagnostic.refresh_timer()


def test_checker_only_preview_skips_mesh_signature(ctx):
    from uv_material_manager.model_checker import native_preview

    prefs = _prefs()
    _set_mode(checker=True)
    try:
        ctx.reset_scene()
        obj = ctx.add_grid(name="checker_only")
        original_signature = diagnostic.mesh_source_signature

        def unexpected_signature(_mesh):
            raise AssertionError("checker-only preview must not scan mesh diagnostic inputs")

        diagnostic.mesh_source_signature = unexpected_signature
        try:
            assert native_preview.ensure_preview(obj, prefs, dirty=True)
        finally:
            diagnostic.mesh_source_signature = original_signature
    finally:
        _set_mode()


def test_material_style_change_updates_shader_without_full_refresh(ctx):
    from uv_material_manager.model_checker import native_preview

    prefs = _prefs()
    _set_mode(stretch=True, orientation=True, checker=True, display_mode='MATERIAL')
    try:
        ctx.reset_scene()
        obj = ctx.add_grid(name="material_style")
        source = bpy.data.materials.new("material_style_source")
        obj.data.materials.append(source)
        assert native_preview.ensure_preview(obj, prefs, dirty=True)

        state = bpy.context.scene.model_checker_state
        from uv_material_manager.model_checker import _sync_scene_overlay
        _sync_scene_overlay(bpy.context.scene, prefs, force_refresh=False)
        state.uv_display_dirty = False
        state.uv_display_full_refresh = False

        old_strength = prefs.uv_stretch_strength
        prefs.uv_stretch_strength = old_strength + 0.25

        assert not state.uv_display_dirty, "材质显示参数变化不应触发整场景刷新"
        strength_nodes = [
            node for node in bpy.data.materials
            if node.get('uvmm_native_preview', False) and node.use_nodes
            for node in node.node_tree.nodes
            if node.get('uvmm_role') == 'strength'
        ]
        assert strength_nodes, "测试材质中应有拉伸强度节点"
        assert abs(strength_nodes[0].inputs[1].default_value - 1.0 / prefs.uv_stretch_strength) < 1e-6
    finally:
        _set_mode()


def test_complex_scene_preview_reuses_material_pipeline(ctx):
    from uv_material_manager.model_checker import native_preview

    _set_mode(stretch=True, orientation=True, checker=True, display_mode='MATERIAL')
    try:
        ctx.reset_scene()
        source = bpy.data.materials.new("complex_scene_shared_source")
        objects = []
        for index in range(12):
            obj = ctx.add_grid(name=f"complex_scene_{index:02d}",
                               x_segments=64, y_segments=64, size=2.0)
            obj.data.materials.append(source)
            obj.location.x = index * 2.5
            objects.append(obj)

        diagnostic.stop_refresh_timer()
        state = bpy.context.scene.model_checker_state
        diagnostic.mark_full_refresh(state)
        native_preview.prepare_display(bpy.context, _prefs())
        for _attempt in range(500):
            interval = diagnostic.refresh_timer()
            if interval is None and not state.uv_display_dirty:
                break

        preview_modifiers = [native_preview.modifier_for(obj) for obj in objects]
        assert all(preview_modifiers), "复杂场景中的每个可见模型都应有预览绑定"
        groups = {modifier.node_group.as_pointer() for modifier in preview_modifiers}
        assert len(groups) == 1, "相同材质组合应共享节点组，避免每个模型重复建立评估节点树"
        assert not state.uv_display_dirty, "复杂场景刷新应完成且不进入轮询重算"
    finally:
        _set_mode()


def test_mesh_change_refreshes_only_that_mesh(ctx):
    from uv_material_manager.model_checker import native_preview

    _set_mode(stretch=True, orientation=True, checker=True, display_mode='MATERIAL')
    try:
        ctx.reset_scene()
        first = ctx.add_grid(name="changed_mesh")
        second = ctx.add_grid(name="untouched_mesh")
        diagnostic.stop_refresh_timer()
        state = bpy.context.scene.model_checker_state
        diagnostic.mark_full_refresh(state)
        native_preview.prepare_display(bpy.context, _prefs())
        for _attempt in range(50):
            interval = diagnostic.refresh_timer()
            if interval is None and not state.uv_display_dirty:
                break

        unchanged_write_time = second.data.get(diagnostic._COLOR_WRITE_TIME_KEY)
        uv = first.data.uv_layers.active.data[0].uv.copy()
        first.data.uv_layers.active.data[0].uv = (uv.x + 0.05, uv.y)
        diagnostic.mark_mesh_dirty(state, first.data, force=True)
        diagnostic.refresh_timer()

        assert first.data.get(diagnostic._COLOR_WRITE_TIME_KEY) != unchanged_write_time
        assert second.data.get(diagnostic._COLOR_WRITE_TIME_KEY) == unchanged_write_time, (
            "未变化的网格不应被重新写入诊断数据"
        )
    finally:
        _set_mode()


def test_full_refresh_leaves_no_attribute_when_disabled(ctx):
    _set_mode(stretch=True)
    obj = _prepare(ctx)
    diagnostic._ensure_preview(obj, _prefs(), force_update=True)
    assert obj.data.get("_model_checker_display_attribute")

    _set_mode()
    diagnostic._ensure_preview(obj, _prefs(), force_update=True)
    assert not obj.data.get("_model_checker_display_attribute")
    assert obj.data.color_attributes.get("__ModelChecker_UVDisplay") is None


def test_stats_cache_is_reused_for_style_change(ctx):
    _set_mode(stretch=True)
    try:
        obj = _prepare(ctx, distort=True)
        prefs = _prefs()
        diagnostic._ensure_preview(obj, prefs, force_update=True)
        assert obj.data.attributes.get("_model_checker_stats_stretch") is not None

        prefs.uv_stretch_opacity = 0.42
        diagnostic._ensure_preview(obj, prefs, force_update=False)
        expected = _scalar_reference(obj, prefs)
        actual = _actual_colors(obj)
        for loop_index, (got, want) in enumerate(zip(actual, expected)):
            for channel in range(4):
                assert abs(got[channel] - want[channel]) <= 3e-5, (
                    f"cached recolor mismatch at loop {loop_index}"
                )
    finally:
        _set_mode()


def test_stats_cache_invalidated_by_nonuniform_scale(ctx):
    _set_mode(stretch=True)
    try:
        obj = _prepare(ctx, distort=True)
        prefs = _prefs()
        diagnostic._ensure_preview(obj, prefs, force_update=True)
        obj.scale = (obj.scale.x * 2.0, obj.scale.y, obj.scale.z)
        bpy.context.view_layer.update()
        diagnostic._ensure_preview(obj, prefs, force_update=False)
        expected = _scalar_reference(obj, prefs)
        actual = _actual_colors(obj)
        for loop_index, (got, want) in enumerate(zip(actual, expected)):
            for channel in range(4):
                assert abs(got[channel] - want[channel]) <= 3e-5, (
                    f"non-uniform scale cache mismatch at loop {loop_index}"
                )
    finally:
        _set_mode()
