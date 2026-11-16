# Nikita Akimov
# interplanety@interplanety.org
#
# GitHub
#   https://github.com/Korchy/blender_material_cleaner

from collections import Counter
from math import isclose
import re
import bpy
from bpy.props import BoolProperty
from mathutils import Vector, Color, Euler
from bpy.types import Operator


def _cleanup_preferences():
    try:
        return bpy.context.preferences.addons[__package__].preferences
    except (AttributeError, KeyError, ReferenceError):
        return None


def _comparison_threshold():
    preferences = _cleanup_preferences()
    return preferences.threshold if preferences else 0.01


class M_Cleaner:

    @staticmethod
    def clear_duplicate_material_slots(objects, context):
        """Remove repeated slots that reference the exact same material datablock."""
        removed = 0
        processed_meshes = set()
        for obj in objects:
            if obj.type != 'MESH' or obj.library or obj.data is None:
                continue
            mesh = obj.data
            object_link_duplicates = []
            data_link_duplicates = []
            seen = {}
            for index, slot in enumerate(obj.material_slots):
                material = slot.material
                if material is None:
                    continue
                key = (slot.link, material.as_pointer())
                if key in seen:
                    (object_link_duplicates if slot.link == 'OBJECT' else data_link_duplicates).append(index)
                else:
                    seen[key] = index

            # Mesh material slots are shared by every object using this mesh.
            # Remove them once and let Blender remap polygon material indices.
            mesh_key = mesh.as_pointer()
            if mesh_key not in processed_meshes and data_link_duplicates:
                for index in reversed(data_link_duplicates):
                    mesh.materials.pop(index=index)
                    for polygon in mesh.polygons:
                        if polygon.material_index >= index:
                            polygon.material_index = max(0, polygon.material_index - 1)
                    removed += 1
                processed_meshes.add(mesh_key)

            # Object-linked slots are per-object and must be removed through Blender's
            # slot operator so its polygon material indices are updated correctly.
            for index in reversed(object_link_duplicates):
                override = {
                    'active_object': obj,
                    'object': obj,
                    'selected_objects': [obj],
                    'selected_editable_objects': [obj],
                }
                if context.window:
                    override['window'] = context.window
                    override['screen'] = context.window.screen
                if context.area:
                    override['area'] = context.area
                with context.temp_override(**override):
                    obj.active_material_index = index
                    result = bpy.ops.object.material_slot_remove()
                if 'FINISHED' in result:
                    removed += 1
        return removed

    @staticmethod
    def clear_numbered_duplicate_materials(materials):
        """Merge numbered copies, restoring the base name when it is free."""
        duplicate_pattern = re.compile(r'^(?P<base>.+)\.(?P<suffix>\d{3,})$')
        removed_count = 0
        restored_names = 0
        for duplicate in sorted(list(materials), key=lambda mat: mat.name):
            if duplicate.library:
                continue
            match = duplicate_pattern.match(duplicate.name)
            if not match:
                continue
            base = materials.get(match.group('base'))
            if base is None:
                duplicate.name = match.group('base')
                restored_names += 1
                continue
            if base is duplicate:
                continue
            if duplicate.use_fake_user:
                base.use_fake_user = True
            duplicate.user_remap(base)
            materials.remove(duplicate)
            removed_count += 1
        return removed_count, restored_names

    @staticmethod
    def merge_by_name(objects, materials):
        # merge materials with the same name
        materials_by_name = sorted([material for material in materials], key=lambda material: material.name)
        raw_cond = re.compile('.*\.\d{3}$') # xxx.001, xxx.002, ...
        # for objects material slots
        for obj in objects:
            material_slots = [slot for slot in obj.material_slots if slot.material is not None and not slot.is_property_readonly('material')]
            for material_slot in material_slots:
                material = material_slot.material
                material_name_truncated = re.escape(material.name[:-4] if re.match(raw_cond, material.name) else material.name)
                material_cond = re.compile('^' + material_name_truncated + '$|^' + material_name_truncated + '\.\d{3}$')    # material_name.001, material_name.002, ...
                material_to_change = next(iter([material for material in materials_by_name if re.match(material_cond, material.name)]), None)
                if material_to_change and material != material_to_change:
                    material_slot.material = material_to_change

    @staticmethod
    def remove_unused_materials(materials):
        # remove materials with 0 users from data
        for mat in list(materials):
            if mat.users == 0:
                materials.remove(mat)

    @staticmethod
    def clear_duplicate_images():
        """按名称清除重复图像并修复命名"""
        unique_images = {}
        removed_count = 0
        renamed_count = 0
        sorted_images = sorted(list(bpy.data.images), key=lambda img: img.name)
        for img in sorted_images:
            if img.library:
                continue
            base_name = re.sub(r'\.\d{3,}$', '', img.name)
            if base_name not in unique_images:
                unique_images[base_name] = img
            else:
                orig_img = unique_images[base_name]
                if img == orig_img:
                    continue
                if img.use_fake_user:
                    orig_img.use_fake_user = True
                img.user_remap(orig_img)
                bpy.data.images.remove(img)
                removed_count += 1
        for img in unique_images.values():
            clean_name = re.sub(r'\.\d{3,}$', '', img.name)
            if img.name != clean_name:
                img.name = clean_name
                renamed_count += 1
        return removed_count, renamed_count


# wrappers for material/node structures
class MaterialWrapper:

    def __init__(self, material):
        self.material = material
        self.node_tree_wrapper = NodeTreeWrapper(node_tree=material.node_tree) if material.node_tree else None

    def __repr__(self):
        return '{}'.format(self.material)

    def __hash__(self):
        return hash(
            (
                self.material.bl_rna.name,
                self.node_tree_wrapper.__hash__(),
            )
        )

    def __eq__(self, other):
        return \
            bool(self.node_tree_wrapper) == bool(other.node_tree_wrapper) \
            and self.node_tree_wrapper == other.node_tree_wrapper


class NodeTreeWrapper:

    def __init__(self, node_tree):
        self.node_tree = node_tree
        self.nodes = [NodeWrapper(node=node) for node in node_tree.nodes]
        self.links = [LinkWrapper(link=link) for link in node_tree.links]

    def __repr__(self):
        return '{}'.format(self.node_tree)

    def __hash__(self):
        return hash(
            (
                self.node_tree.type,
                tuple(node.__hash__() for node in self.nodes),
                tuple(link.__hash__() for link in self.links)
            )
        )

    def __eq__(self, other):
        return \
            self.node_tree.type == other.node_tree.type \
            and Counter(self.nodes) == Counter(other.nodes) \
            and Counter(self.links) == Counter(other.links)


class NodeWrapper:

    excluded_attributes = (
        'bl_idname',
        'color',
        'dimensions',
        'height',
        'hide',
        'inputs',
        'interface',
        'internal_links',
        'is_active_output',
        'label',
        'location',
        'name',
        'outputs',
        'panel_states',
        'parent',
        'rna_type',
        'select',
        'shading_compatibility',
        'show_options',
        'show_preview',
        'show_texture',
        'use_custom_color',
        'width',
        'width_hidden'
    )

    def __init__(self, node):
        self.node = node
        if self.node.bl_idname == 'ShaderNodeGroup':
            preferences = _cleanup_preferences()
            if preferences and preferences.node_groups_as_nodes:
                # node group as node
                self.attributes = [NodeAttributeWrapper(attribute, getattr(node, attribute)) for attribute in dir(node) if
                                   not attribute.startswith('__')
                                   and not attribute.startswith('bl_')
                                   and attribute not in self.excluded_attributes
                                   and attribute != 'node_tree'
                                   and hasattr(node, attribute)
                                   and not callable(getattr(node, attribute))
                                   ]
                self.attributes.append(NodeAttributeWrapper('node_tree_name', (node.node_tree.name if node.node_tree else None)))
            else:
                # node tree
                self.node_tree = NodeTreeWrapper(node_tree=node.node_tree) if node.node_tree else None
        else:
            self.attributes = [NodeAttributeWrapper(attribute, getattr(node, attribute)) for attribute in dir(node) if
                               not attribute.startswith('__')
                               and not attribute.startswith('bl_')
                               and attribute not in self.excluded_attributes
                               and hasattr(node, attribute)
                               and not callable(getattr(node, attribute))
                               ]
        self.node_inputs = [NodeInputOutputWrapper(node_io=node_input) for node_input in node.inputs if hasattr(node_input, 'default_value')]
        self.node_outputs = [NodeInputOutputWrapper(node_io=node_output) for node_output in node.outputs if hasattr(node_output, 'default_value')]

    def __repr__(self):
        return '{},{}'.format(self.node, self.node.bl_idname)

    def __hash__(self):
        node_wrapper_hash_data = (self.node.bl_idname,)
        if self.node.bl_idname == 'ShaderNodeGroup' \
                and not (_cleanup_preferences() and _cleanup_preferences().node_groups_as_nodes):
            node_wrapper_hash_data += (self.node_tree.__hash__(),)
        else:
            node_wrapper_hash_data += tuple(wrapped_attribute.__hash__() for wrapped_attribute in self.attributes)
        node_wrapper_hash_data += tuple(wrapped_input.__hash__() for wrapped_input in self.node_inputs)
        node_wrapper_hash_data += tuple(wrapped_output.__hash__() for wrapped_output in self.node_outputs)
        return hash(node_wrapper_hash_data)

    def __eq__(self, other):
        # level 1: type
        rez = self.node.bl_idname == other.node.bl_idname
        if rez:
            # level 2: for node group - node trees, for nodes - attributes
            if self.node.bl_idname == 'ShaderNodeGroup' \
                    and not (_cleanup_preferences() and _cleanup_preferences().node_groups_as_nodes):
                rez = \
                    bool(self.node_tree) == bool(other.node_tree) \
                    and self.node_tree == other.node_tree
            else:
                rez = Counter(self.attributes) == Counter(other.attributes)  # same count of the same attributes
            # level 3: inputs
            if rez:
                rez = self.node_inputs == other.node_inputs
            # level 4: outputs
            if rez:
                rez = self.node_outputs == other.node_outputs
        return rez


class NodeAttributeWrapper:

    def __init__(self, node_attribute_name, node_attribute_value):
        self.node_attribute_name = node_attribute_name
        self.node_attribute_value = node_attribute_value
        if type(node_attribute_value) in [Vector, Color]:
            self.node_attribute = TupleLikeAttributeWrapper(attribute=node_attribute_value)
        elif type(self.node_attribute_value) is bpy.types.ColorMapping:
            self.node_attribute = ColorMappingWrapper(color_mapping=node_attribute_value)
        elif type(self.node_attribute_value) is bpy.types.ColorRamp:
            self.node_attribute = ColorRampWrapper(color_ramp=node_attribute_value)
        elif type(self.node_attribute_value) is bpy.types.TexMapping:
            self.node_attribute = TextureMappingWrapper(texture_mapping=node_attribute_value)
        elif type(self.node_attribute_value) is bpy.types.ImageUser:
            self.node_attribute = ImageUserWrapper(image_user=node_attribute_value)
        elif type(self.node_attribute_value) is bpy.types.CurveMapping:
            self.node_attribute = CurveMappingWrapper(curve_mapping=node_attribute_value)
        else:
            self.node_attribute = node_attribute_value

    def __repr__(self):
        return '{}: {}'.format(self.node_attribute_name, self.node_attribute_value)

    def __hash__(self):
        if type(self.node_attribute_value) in (Vector, Color, bpy.types.ColorMapping, bpy.types.ColorRamp, bpy.types.TexMapping, bpy.types.ImageUser, bpy.types.CurveMapping):
            return hash((self.node_attribute_name, self.node_attribute.__hash__()))
        else:
            return hash((self.node_attribute_name, self.node_attribute))

    def __eq__(self, other):
        rez = self.node_attribute_name == other.node_attribute_name
        if rez:
            rez = self.node_attribute == other.node_attribute
        return rez


class ColorMappingWrapper:

    def __init__(self, color_mapping):
        self.color_mapping = color_mapping
        self.blend_color = TupleLikeAttributeWrapper(attribute=color_mapping.blend_color)
        self.blend_factor = color_mapping.blend_factor
        self.blend_type = color_mapping.blend_type
        self.brightness = color_mapping.brightness
        self.color_ramp = ColorRampWrapper(color_ramp=color_mapping.color_ramp)
        self.contrast = color_mapping.contrast
        self.saturation = color_mapping.saturation
        self.use_color_ramp = color_mapping.use_color_ramp

    def __repr__(self):
        return '{}'.format(self.color_mapping)

    def __hash__(self):
        return hash(
            (
                self.blend_color.__hash__(),
                self.blend_factor,
                self.blend_type,
                self.brightness,
                self.color_ramp.__hash__(),
                self.contrast,
                self.saturation,
                self.use_color_ramp
            )
        )

    def __eq__(self, other):
        return \
            self.blend_color == other.blend_color \
            and isclose(self.blend_factor, other.blend_factor, abs_tol=_comparison_threshold()) \
            and self.blend_type == other.blend_type \
            and isclose(self.brightness, other.brightness, abs_tol=_comparison_threshold()) \
            and self.color_ramp == other.color_ramp \
            and isclose(self.contrast, other.contrast, abs_tol=_comparison_threshold()) \
            and isclose(self.saturation, other.saturation, abs_tol=_comparison_threshold()) \
            and self.use_color_ramp == other.use_color_ramp


class ColorRampWrapper:

    def __init__(self, color_ramp):
        self.color_ramp = color_ramp
        self.elements = [ColorRampElementWrapper(element) for element in color_ramp.elements]

    def __repr__(self):
        return '{}'.format(self.color_ramp)

    def __hash__(self):
        return hash(
            (
                self.color_ramp.color_mode,
                tuple(element.__hash__() for element in self.elements),
                self.color_ramp.hue_interpolation,
                self.color_ramp.interpolation
             )
        )

    def __eq__(self, other):
        return \
            self.color_ramp.color_mode == other.color_ramp.color_mode \
            and Counter(self.elements) == Counter(other.elements) \
            and self.color_ramp.hue_interpolation == other.color_ramp.hue_interpolation \
            and self.color_ramp.interpolation == other.color_ramp.interpolation


class ColorRampElementWrapper:

    def __init__(self, color_ramp_element):
        self.color_ramp_element = color_ramp_element
        self.alpha = color_ramp_element.alpha
        self.color = TupleLikeAttributeWrapper(color_ramp_element.color)
        self.position = color_ramp_element.position

    def __repr__(self):
        return '{}'.format(self.color_ramp_element)

    def __hash__(self):
        return hash(
            (
                self.alpha,
                self.color.__hash__(),
                self.position
             )
        )

    def __eq__(self, other):
        return \
            isclose(self.alpha, other.alpha, abs_tol=_comparison_threshold()) \
            and self.color == other.color \
            and isclose(self.position, other.position, abs_tol=_comparison_threshold())


class CurveMappingWrapper:

    def __init__(self, curve_mapping):
        self.curve_mapping = curve_mapping
        self.black_level = TupleLikeAttributeWrapper(attribute=curve_mapping.black_level)
        self.clip_max_x = curve_mapping.clip_max_x
        self.clip_max_y = curve_mapping.clip_max_y
        self.clip_min_x = curve_mapping.clip_min_x
        self.clip_min_y = curve_mapping.clip_min_y
        self.curves = [CurveMapWrapper(curve) for curve in curve_mapping.curves]
        self.tone = curve_mapping.tone
        self.use_clip = curve_mapping.use_clip
        self.white_level = TupleLikeAttributeWrapper(attribute=curve_mapping.white_level)

    def __repr__(self):
        return '{}'.format(self.curve_mapping)

    def __hash__(self):
        return hash(
            (
                self.black_level.__hash__(),
                self.clip_max_x,
                self.clip_max_y,
                self.clip_min_x,
                self.clip_min_y,
                tuple(curve.__hash__() for curve in self.curves),
                self.tone,
                self.use_clip,
                self.white_level.__hash__()
             )
        )

    def __eq__(self, other):
        return \
            self.black_level == other.black_level \
            and isclose(self.clip_max_x, other.clip_max_x, abs_tol=_comparison_threshold()) \
            and isclose(self.clip_max_y, other.clip_max_y, abs_tol=_comparison_threshold()) \
            and isclose(self.clip_min_x, other.clip_min_x, abs_tol=_comparison_threshold()) \
            and isclose(self.clip_min_y, other.clip_min_y, abs_tol=_comparison_threshold()) \
            and Counter(self.curves) == Counter(other.curves) \
            and self.tone == other.tone \
            and self.use_clip == other.use_clip \
            and self.white_level == other.white_level


class CurveMapWrapper:

    def __init__(self, curve):
        self.curve = curve
        self.points = [CurveMapPointWrapper(point) for point in curve.points]

    def __repr__(self):
        return '{}'.format(self.curve)

    def __hash__(self):
        return hash(
            (
                self.curve.extend if hasattr(self.curve, 'extend') else '',
                tuple(point.__hash__() for point in self.points)
            )
        )

    def __eq__(self, other):
        return \
            (self.curve.extend if hasattr(self.curve, 'extend') else '') == (other.curve.extend if hasattr(other.curve, 'extend') else '') \
            and Counter(self.points) == Counter(other.points)


class CurveMapPointWrapper:

    def __init__(self, curve_map_point):
        self.curve_map_point = curve_map_point
        self.handle_type = curve_map_point.handle_type
        self.location = TupleLikeAttributeWrapper(attribute=curve_map_point.location)

    def __repr__(self):
        return '{}'.format(self.curve_map_point)

    def __hash__(self):
        return hash(
            (
                self.handle_type,
                self.location.__hash__()
            )
        )

    def __eq__(self, other):
        return \
            self.handle_type == other.handle_type \
            and self.location == other.location


class TextureMappingWrapper:

    def __init__(self, texture_mapping):
        self.texture_mapping = texture_mapping
        self.mapping = texture_mapping.mapping
        self.mapping_x = texture_mapping.mapping_x
        self.mapping_y = texture_mapping.mapping_y
        self.mapping_z = texture_mapping.mapping_z
        self.max = TupleLikeAttributeWrapper(texture_mapping.max)
        self.min = TupleLikeAttributeWrapper(texture_mapping.min)
        self.rotation = EulerWrapper(euler=texture_mapping.rotation)
        self.scale = TupleLikeAttributeWrapper(texture_mapping.scale)
        self.translation = TupleLikeAttributeWrapper(texture_mapping.translation)
        self.use_max = texture_mapping.use_max
        self.use_min = texture_mapping.use_min
        self.vector_type = texture_mapping.vector_type

    def __repr__(self):
        return '{}'.format(self.texture_mapping)

    def __hash__(self):
        return hash(
            (
                self.mapping,
                self.mapping_x,
                self.mapping_y,
                self.mapping_z,
                self.max.__hash__(),
                self.min.__hash__(),
                self.rotation.__hash__(),
                self.scale.__hash__(),
                self.translation.__hash__(),
                self.use_max,
                self.use_min,
                self.vector_type
             )
        )

    def __eq__(self, other):
        return \
            self.mapping == other.mapping \
            and self.mapping_x == other.mapping_x \
            and self.mapping_y == other.mapping_y \
            and self.mapping_z == other.mapping_z \
            and self.max == other.max \
            and self.min == other.min \
            and self.rotation == other.rotation \
            and self.scale == other.scale \
            and self.translation == other.translation \
            and self.use_max == other.use_max \
            and self.use_min == other.use_min \
            and self.vector_type == other.vector_type


class ImageUserWrapper:

    def __init__(self, image_user):
        self.image_user = image_user

    def __repr__(self):
        return '{}'.format(self.image_user)

    def __hash__(self):
        return hash((self.image_user.frame_current,
                     self.image_user.frame_duration,
                     self.image_user.frame_offset,
                     self.image_user.frame_start,
                     self.image_user.multilayer_layer,
                     self.image_user.multilayer_pass,
                     self.image_user.multilayer_view,
                     self.image_user.use_auto_refresh,
                     self.image_user.use_cyclic
                     ))

    def __eq__(self, other):
        return self.image_user.frame_current == other.image_user.frame_current \
               and self.image_user.frame_duration == other.image_user.frame_duration \
               and self.image_user.frame_offset == other.image_user.frame_offset \
               and self.image_user.frame_start == other.image_user.frame_start \
               and self.image_user.multilayer_layer == other.image_user.multilayer_layer \
               and self.image_user.multilayer_pass == other.image_user.multilayer_pass \
               and self.image_user.multilayer_view == other.image_user.multilayer_view \
               and self.image_user.use_auto_refresh == other.image_user.use_auto_refresh \
               and self.image_user.use_cyclic == other.image_user.use_cyclic


class EulerWrapper:

    def __init__(self, euler):
        self.euler = euler

    def __repr__(self):
        return '{}'.format(self.euler)

    def __hash__(self):
        return hash(tuple(self.euler) + (self.euler.order,))

    def __eq__(self, other):
        return \
            self.euler.order == other.euler.order \
            and False not in (map(lambda t1, t2: isclose(t1, t2, abs_tol=_comparison_threshold()), tuple(self.euler), tuple(other.euler)))


class TupleLikeAttributeWrapper:

    def __init__(self, attribute):
        self.attribute = attribute

    def __repr__(self):
        return '{}'.format(self.attribute)

    def __hash__(self):
        threshold_digits = len(str(int(0.1 / _comparison_threshold())))
        return hash(
            tuple(map(lambda x: round(x, threshold_digits), tuple(self.attribute)))
        )

    def __eq__(self, other):
        return False not in (map(lambda t1, t2: isclose(t1, t2, abs_tol=_comparison_threshold()), tuple(self.attribute), tuple(other.attribute)))


class NodeInputOutputWrapper:

    def __init__(self, node_io):
        self.node_io = node_io
        if type(node_io.default_value) in [Vector, Color, bpy.types.bpy_prop_array]:
            self.default_value = TupleLikeAttributeWrapper(attribute=node_io.default_value)
        elif type(node_io.default_value) is Euler:
            self.default_value = EulerWrapper(euler=node_io.default_value)
        elif type(node_io.default_value) is float:
            threshold_digits = len(str(int(0.1 / _comparison_threshold())))
            self.default_value = round(node_io.default_value, threshold_digits)
        else:
            self.default_value = node_io.default_value

    def __repr__(self):
        return '{}: {}'.format(self.node_io, self.node_io.default_value)

    def __hash__(self):
        if type(self.node_io.default_value) in [Vector, Color, bpy.types.bpy_prop_array]:
            return hash(
                (
                    type(self.node_io.default_value),
                    self.default_value.__hash__()
                )
            )
        elif type(self.node_io.default_value) is Euler:
            return self.default_value.__hash__()
        else:
            return hash(self.default_value)

    def __eq__(self, other):
        return self.default_value == other.default_value


class LinkWrapper:

    def __init__(self, link):
        self.link = link
        self.from_node = NodeWrapper(link.from_node)
        self.to_node = NodeWrapper(link.to_node)

    def __repr__(self):
        return '{}'.format(self.link)

    def __hash__(self):
        return hash(
            (
                self.from_node.__hash__(),
                self.to_node.__hash__(),
                self.link.from_node.outputs.values().index(self.link.from_socket),
                self.link.to_node.inputs.values().index(self.link.to_socket)
            )
        )

    def __eq__(self, other):
        return \
            self.from_node == other.from_node \
            and self.to_node == other.to_node \
            and self.link.from_node.outputs.values().index(self.link.from_socket) == other.link.from_node.outputs.values().index(other.link.from_socket) \
            and self.link.to_node.inputs.values().index(self.link.to_socket) == other.link.to_node.inputs.values().index(other.link.to_socket)


class UVMM_OT_CleanDuplicateMaterials(Operator):
    bl_idname = 'uvmm.clean_duplicate_materials'
    bl_label = '清理材质'
    bl_description = '合并同名数字后缀材质；原名称空缺时去掉 .001 等后缀，恢复原名'
    bl_options = {'REGISTER', 'UNDO'}

    remove_unused: BoolProperty(
        name='清理未使用材质',
        description='合并重复材质后，再删除所有没有用户引用的材质数据块',
        default=False,
    )

    def draw(self, context):
        self.layout.prop(self, 'remove_unused', text='清理未使用材质')

    def execute(self, context):
        materials = bpy.data.materials
        removed_slots = M_Cleaner.clear_duplicate_material_slots(
            objects=bpy.data.objects,
            context=context,
        )
        removed_duplicates, restored_names = M_Cleaner.clear_numbered_duplicate_materials(materials=materials)
        removed_unused = 0
        if self.remove_unused:
            before = len(materials)
            M_Cleaner.remove_unused_materials(materials=materials)
            removed_unused = before - len(materials)
        self.report(
            {'INFO'},
            f'清理材质完成：移除 {removed_slots} 个重复槽、{removed_duplicates} 个重复副本，'
            f'去掉数字后缀 {restored_names} 个，'
            f'清理未使用材质 {removed_unused} 个',
        )
        return {'FINISHED'}


class UVMM_OT_MergeMaterialsByName(Operator):
    bl_idname = 'uvmm.merge_materials_by_name'
    bl_label = '按名称合并材质'
    bl_description = '按 M-Cleaner 原有名称及 Blender 数字后缀规则合并材质'
    bl_options = {'REGISTER', 'UNDO'}

    remove_unused: BoolProperty(
        name='清理未使用材质',
        description='按名称合并后，再删除所有没有用户引用的材质数据块',
        default=False,
    )

    def draw(self, context):
        self.layout.prop(self, 'remove_unused', text='清理未使用材质')

    def execute(self, context):
        materials = bpy.data.materials
        M_Cleaner.merge_by_name(objects=bpy.data.objects, materials=materials)
        if self.remove_unused:
            M_Cleaner.remove_unused_materials(materials=materials)
        return {'FINISHED'}


class UVMM_OT_CleanDuplicateImages(Operator):
    bl_idname = 'uvmm.clean_duplicate_images'
    bl_label = '清理重复贴图'
    bl_description = '按 M-Cleaner 原有图片名称后缀规则合并重复图片并修复命名'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        removed, renamed = M_Cleaner.clear_duplicate_images()
        self.report({'INFO'}, f'重复贴图清理完成：移除 {removed} 个副本，修正 {renamed} 个名称')
        return {'FINISHED'}


_OPERATOR_CLASSES = (
    UVMM_OT_CleanDuplicateMaterials,
    UVMM_OT_MergeMaterialsByName,
    UVMM_OT_CleanDuplicateImages,
)


def register():
    for cls in _OPERATOR_CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_OPERATOR_CLASSES):
        bpy.utils.unregister_class(cls)

