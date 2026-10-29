# -*- coding: utf-8 -*-
"""UV 诊断颜色与旧版逐三角实现的一致性测试。

新版用 numpy 向量化计算，必须与 scalar 参考实现逐通道一致；
同时验证棋盘格预览改为共享节点组/材质后没有回退。
"""

import unittest

import bpy
from mathutils import Vector

from uv_material_manager.model_checker import diagnostic


def _prefs():
    return bpy.context.preferences.addons["uv_material_manager"].preferences


def _set_mode(stretch=False, orientation=False, checker=False):
    prefs = _prefs()
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

    reference = [None] * len(mesh.loops)
    for polygon in mesh.polygons:
        index = polygon.index
        flipped = (
            triangle_counts[index] > 0
            and flipped_votes[index] * 2 >= triangle_counts[index]
        )
        base = diagnostic._face_base_color(max_stretch[index], flipped, prefs)
        for loop_index in polygon.loop_indices:
            reference[loop_index] = (*base, 1.0)
    return reference


def _actual_colors(obj):
    mesh = obj.data
    name = mesh.get("_model_checker_display_attribute")
    assert name, "诊断颜色属性没有创建"
    attribute = mesh.color_attributes.get(name)
    assert attribute is not None
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


def test_orientation_matches_scalar_reference(ctx):
    _set_mode(orientation=True)
    try:
        obj = _prepare(ctx, distort=True, flip=True)
        _assert_matches(obj)
    finally:
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
    _set_mode(checker=True)
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
