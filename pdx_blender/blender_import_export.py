"""Mesh and animation import/export. Author: ross-g."""

import math
import os
import xml.etree.ElementTree as Xml
from collections import OrderedDict, defaultdict, namedtuple
from operator import itemgetter
from pathlib import Path
from time import perf_counter

import bmesh
import bpy
from mathutils import Matrix, Quaternion, Vector

from .. import IO_PDX_LOG, pdx_data
from ..library import (
    PDX_ANIMATION,
    PDX_DECIMALPTS,
    PDX_IGNOREJOINT,
    PDX_MAXSKININFS,
    PDX_MAXUVSETS,
    PDX_MESHINDEX,
    PDX_ROUND_ROT,
    PDX_ROUND_SCALE,
    PDX_ROUND_TRANS,
    PDX_SHADER,
    allow_debug_logging,
    get_lod_level,
)


NORMAL_TOLERANCE = 1e-2

# The game is Y-up with -Z forward, Blender is Z-up with -Y forward. Swapping Y and Z mirrors the scene, which is
# harmless as long as every matrix goes through swap_coord_space.
SPACE_MATRIX = Matrix((
    (1, 0, 0, 0),
    (0, 0, 1, 0),
    (0, 1, 0, 0),
    (0, 0, 0, 1)
))
SPACE_MATRIX_INV = SPACE_MATRIX.inverted_safe()
BONESPACE_MATRIX = Matrix((
    (0, 1, 0, 0),
    (-1, 0, 0, 0),
    (0, 0, 1, 0),
    (0, 0, 0, 1)
))


def util_round(data, ndigits=0):
    return tuple(round(x, ndigits) for x in data)


def clean_imported_name(name):
    clean_name = name.split(":")[-1]

    clean_name = clean_name.replace("|", "_")

    return clean_name


def get_bmesh(mesh_data, **kwargs):
    bm = bmesh.new()
    bm.from_mesh(mesh_data, **kwargs)

    return bm


def get_rig_from_bone_name(bone_name):
    scene_rigs = [obj for obj in bpy.data.objects if isinstance(obj.data, bpy.types.Armature)]

    for rig in scene_rigs:
        armt = rig.data
        if bone_name in [b.name for b in armt.bones]:
            return rig


def get_rig_from_mesh(blender_obj):
    skin_modifier = [mod for mod in blender_obj.modifiers if isinstance(mod, bpy.types.ArmatureModifier)]

    if skin_modifier:
        skin = skin_modifier[0]
        rig = skin.object
    else:
        rig = None

    return rig


def list_scene_pdx_meshes():
    return [
        obj for obj in bpy.context.scene.objects if isinstance(obj.data, bpy.types.Mesh) and check_mesh_material(obj)
    ]


def set_local_axis_display(state, data_type):
    type_dict = {"EMPTY": type(None), "ARMATURE": bpy.types.Armature}
    object_list = [obj for obj in bpy.data.objects if isinstance(obj.data, type_dict[data_type])]

    for node in object_list:
        try:
            node.show_axis = state
            if node.data:
                node.data.show_axes = state
        except Exception as err:
            IO_PDX_LOG.warning(f"Could not display local axis for node - {node.name}")
            IO_PDX_LOG.error(err)


def set_ignore_joints(state):
    sel_pose_bones = bpy.context.selected_pose_bones or []
    sel_edit_bones = bpy.context.selected_editable_bones or []

    bone_list = [posebone.bone for posebone in sel_pose_bones] + sel_edit_bones

    for bone in bone_list:
        bone[PDX_IGNOREJOINT] = state


def set_mesh_index(blender_mesh, i):
    if PDX_MESHINDEX not in blender_mesh.keys():
        blender_mesh["_RNA_UI"] = {}
        blender_mesh["_RNA_UI"][PDX_MESHINDEX] = {"min": 0, "max": 255, "soft_min": 0, "soft_max": 255, "step": 1}

    blender_mesh[PDX_MESHINDEX] = i


def get_mesh_index(blender_mesh):
    return blender_mesh.get(PDX_MESHINDEX, 255)


def check_mesh_material(blender_obj):
    result = False

    materials = [slot.material for slot in blender_obj.material_slots]
    for material in materials:
        if material:
            result = result or (PDX_SHADER in material.keys())

    return result


def get_material_shader(blender_material):
    return blender_material.get(PDX_SHADER, None)


def get_material_textures(blender_material):
    texture_dict = {}

    node_tree = blender_material.node_tree
    nodes = node_tree.nodes

    try:
        material_output = next(
            n for n in nodes if isinstance(n, bpy.types.ShaderNodeOutputMaterial) and n.inputs["Surface"].is_linked
        )
    except StopIteration:
        raise RuntimeError(f"No connected 'Material Output' found for material: {blender_material.name}")

    surface_input = material_output.inputs["Surface"].links[0]
    shader_root = surface_input.from_node
    if shader_root.type != "BSDF_PRINCIPLED":
        shader_root = next((n for n in nodes if n.type == "BSDF_PRINCIPLED"), shader_root)
    if not isinstance(surface_input.from_socket, bpy.types.NodeSocketShader):
        raise RuntimeError(
            f"No BSDF shader connected to 'Material Output > Surface' for material: {blender_material.name}"
        )

    for bsdf_input, pdxmaterial_slot in zip(["Base Color", "Roughness", "Normal"], ["diff", "spec", "n"]):
        if shader_root.inputs[bsdf_input].is_linked:
            try:
                input_node = shader_root.inputs[bsdf_input].links[0].from_node
                while not isinstance(input_node, bpy.types.ShaderNodeTexImage):
                    first_link = next(i for i in input_node.inputs if i.is_linked)
                    input_node = first_link.links[0].from_node

                if input_node.image is not None:
                    tex_filepath = input_node.image.filepath_from_user()
                    texture_dict[pdxmaterial_slot] = tex_filepath

            except StopIteration:
                IO_PDX_LOG.warning(f"No connected '{bsdf_input}' image texture found for - {blender_material.name}")

    return texture_dict


def get_mesh_info(blender_obj, mat_id, split_criteria=None, split_all=False, sort_vertices=True):
    mesh = blender_obj.data.copy()
    mesh.name = blender_obj.data.name + "_export"
    mesh.transform(blender_obj.matrix_world)
    mesh.calc_loop_triangles()
    try:
        mesh.calc_normals_split()
    except AttributeError:
        pass

    uv_setnames = [uv_set.name for uv_set in mesh.uv_layers if len(uv_set.data)][:PDX_MAXUVSETS]
    if uv_setnames:
        mesh.calc_tangents(uvmap=uv_setnames[0])

    mesh_dict = {x: [] for x in ["p", "n", "ta", "u0", "u1", "u2", "u3", "tri", "min", "max"]}

    split_criteria = split_criteria or ["id", "p", "n", "uv"]
    UniqueVertex = namedtuple("UniqueVertex", split_criteria)

    export_verts = []
    unique_verts = set()
    vert_lookup = {}

    for tri in mesh.loop_triangles:
        if tri.material_index != mat_id:
            continue

        tri_loops = [mesh.loops[i] for i in tri.loops]
        if sort_vertices is not None:
            _sorted = sorted(enumerate(tri_loops), key=lambda x: x[1].vertex_index, reverse=not sort_vertices)
            indices = [i[0] for i in _sorted]
            tri_loops = [i[1] for i in _sorted]
        else:
            indices = [0, 1, 2]

        dict_vert_idx = []

        for loop in tri_loops:
            vert_id = loop.vertex_index

            _position = mesh.vertices[vert_id].co
            _position = tuple(swap_coord_space(_position))

            _normal = loop.normal
            _normal = tuple(swap_coord_space(_normal))

            _uv_coords = ()
            for _, uv_set in enumerate(uv_setnames):
                uv_layer = mesh.uv_layers[uv_set]
                uv = uv_layer.data[loop.index].uv
                uv = tuple(swap_coord_space(tuple(uv)))
                _uv_coords += (uv,)

            if uv_setnames:
                _bitangent_sign = loop.bitangent_sign
                _tangent = loop.tangent
                _tangent = tuple(swap_coord_space(_tangent))

            vdata = {"id": vert_id, "p": _position, "n": _normal, "uv": _uv_coords}
            new_vert = UniqueVertex(*[vdata.get(x) for x in split_criteria])

            i = None
            lookup_key = tuple(value for name, value in zip(split_criteria, new_vert) if name != "n")
            if not split_all:
                for stored_normal, stored_idx in vert_lookup.get(lookup_key, ()):
                    if "n" not in split_criteria or all(abs(a - b) <= NORMAL_TOLERANCE for a, b in zip(_normal, stored_normal)):
                        i = stored_idx
                        break

            if i is None:
                vert_lookup.setdefault(lookup_key, []).append((_normal, len(export_verts)))
                unique_verts.add(new_vert)
                export_verts.append(new_vert)

                mesh_dict["p"].extend(_position)
                mesh_dict["n"].extend(_normal)
                for i, _uv_set in enumerate(uv_setnames):
                    mesh_dict[f"u{i}"].extend(_uv_coords[i])
                if uv_setnames:
                    mesh_dict["ta"].extend(_tangent)
                    mesh_dict["ta"].append(_bitangent_sign)
                i = len(export_verts) - 1

            dict_vert_idx.append(i)

        mesh_dict["tri"].extend(
            [dict_vert_idx[indices[0]], dict_vert_idx[indices[2]], dict_vert_idx[indices[1]]]
        )

    if not export_verts:
        return {}, []

    x_vtx_pos = set(mesh_dict["p"][::3])
    y_vtx_pos = set(mesh_dict["p"][1::3])
    z_vtx_pos = set(mesh_dict["p"][2::3])
    mesh_dict["min"] = [min(x_vtx_pos), min(y_vtx_pos), min(z_vtx_pos)]
    mesh_dict["max"] = [max(x_vtx_pos), max(y_vtx_pos), max(z_vtx_pos)]
    centre = Vector([(mesh_dict["max"][axis] + mesh_dict["min"][axis]) * 0.5 for axis in range(3)])
    radius = max(((Vector(vertex.p) - centre) for vertex in unique_verts))
    mesh_dict["boundingsphere"] = [centre.x, centre.y, centre.z, radius.length]

    vert_id_list = [vert.id for vert in export_verts]

    bpy.data.meshes.remove(mesh)

    return mesh_dict, vert_id_list


def get_mesh_skin_info(blender_obj, vertex_ids=None):
    skin_mod = [mod for mod in blender_obj.modifiers if isinstance(mod, bpy.types.ArmatureModifier)]
    if not skin_mod:
        return None

    skin = skin_mod[0]
    rig = skin.object
    if rig is None:
        return None

    skin_dict = {x: [] for x in ["bones", "ix", "w"]}

    skin_dict["bones"].append(PDX_MAXSKININFS)

    bone_names = [bone.name for bone in get_skeleton_hierarchy(rig)]
    group_names = [group.name for group in blender_obj.vertex_groups]

    mesh = blender_obj.data
    if vertex_ids is None:
        vertex_ids = range(len(mesh.vertices))

    vert_weights = {v: {} for v in vertex_ids}
    for vert_id, vtx in enumerate(mesh.vertices):
        for vtx_group in vtx.groups:
            group_index = vtx_group.group
            try:
                bone_index = bone_names.index(group_names[group_index])
            except ValueError:
                raise RuntimeError(
                    f"Mesh {mesh.name} has vertices skinned to a group ({group_names[group_index]}) targeting a missing"
                    f" or excluded armature bone! Check any bones with the '{PDX_IGNOREJOINT}' property."
                )

            if group_index < len(blender_obj.vertex_groups):
                if vert_id in vertex_ids:
                    weight = vtx_group.weight
                    if weight != 0.0:
                        vert_weights[vert_id][bone_index] = vtx_group.weight

    for vtx in vertex_ids:
        if len(vert_weights[vtx]) > PDX_MAXSKININFS:
            IO_PDX_LOG.warning(f"Mesh '{mesh.name}' has vertices skinned to more than {PDX_MAXSKININFS} vertex groups.")
            inf_weights = sorted(vert_weights[vtx].items(), key=itemgetter(1), reverse=True)
            inf_weights = dict(inf_weights[:PDX_MAXSKININFS])
            total = sum(inf_weights.values())

            vert_weights[vtx] = {inf: weight / total for inf, weight in inf_weights.items()}

        for influence, weight in vert_weights[vtx].items():
            skin_dict["ix"].append(influence)
            skin_dict["w"].append(weight)

        if len(vert_weights[vtx]) <= PDX_MAXSKININFS:
            padding = PDX_MAXSKININFS - len(vert_weights[vtx])
            skin_dict["ix"].extend([-1] * padding)
            skin_dict["w"].extend([0.0] * padding)

    return skin_dict


def get_mesh_skeleton_info(blender_obj):
    rig = get_rig_from_mesh(blender_obj)
    if rig is None:
        return []

    rig_bones = get_skeleton_hierarchy(rig)

    return get_bones_info(rig_bones)


def get_bones_info(blender_bones):
    bone_list = [{"name": x.name} for x in blender_bones]

    for i, bone in enumerate(blender_bones):
        bone_list[i]["ix"] = [i]

        if bone.parent:
            bone_list[i]["pa"] = [blender_bones.index(bone.parent)]

        armature = bone.id_data
        rig = [obj for obj in bpy.data.objects if isinstance(obj.data, bpy.types.Armature) and obj.data == armature][0]
        mat = swap_coord_space(rig.matrix_world @ bone.matrix_local).inverted_safe()
        mat.transpose()
        mat = [i for vector in mat for i in vector]
        bone_list[i]["tx"] = []
        bone_list[i]["tx"].extend(mat[0:3])
        bone_list[i]["tx"].extend(mat[4:7])
        bone_list[i]["tx"].extend(mat[8:11])
        bone_list[i]["tx"].extend(mat[12:15])

    return bone_list


def get_locators_info(blender_empties):
    locator_list = [{"name": x.name} for x in blender_empties]

    for i, obj in enumerate(blender_empties):
        _transform = obj.matrix_world

        if obj.parent and obj.parent_type == "BONE":
            locator_list[i]["pa"] = [obj.parent_bone]
            rig = obj.parent
            bone_matrix = rig.matrix_world @ rig.data.bones[obj.parent_bone].matrix_local
            _transform = bone_matrix.inverted_safe() @ obj.matrix_world

        _position, _rotation, _scale = swap_coord_space(_transform).decompose()

        locator_list[i]["p"] = list(_position)
        locator_list[i]["q"] = list([_rotation.x, _rotation.y, _rotation.z, _rotation.w])

        is_scaled = util_round(list(_scale), PDX_ROUND_SCALE) != (1.0, 1.0, 1.0)
        if is_scaled:
            transform = swap_coord_space(_transform)
            locator_list[i]["tx"] = [
                transform[0][0],
                transform[1][0],
                transform[2][0],
                transform[3][0],
                transform[0][1],
                transform[1][1],
                transform[2][1],
                transform[3][1],
                transform[0][2],
                transform[1][2],
                transform[2][2],
                transform[3][2],
                transform[0][3],
                transform[1][3],
                transform[2][3],
                transform[3][3],
            ]

    return locator_list


def get_skeleton_hierarchy(rig):
    root_bone = rig.data.bones[0]

    def get_recursive_children(bone, hierarchy):
        hierarchy.append(bone)
        children = [jnt for jnt in bone.children if not jnt.get(PDX_IGNOREJOINT)]

        for bone in children:
            get_recursive_children(bone, hierarchy)

        return hierarchy

    valid_bones = []
    get_recursive_children(root_bone, valid_bones)

    return valid_bones


def get_scene_animdata(rig, export_bones, startframe, endframe):
    frames_data = defaultdict(list)

    for f in range(startframe, endframe + 1):
        bpy.context.scene.frame_set(f)
        for bone in export_bones:
            pose_bone = rig.pose.bones[bone.name]

            parent_matrix = Matrix()
            if pose_bone.parent:
                parent_matrix = rig.convert_space(
                    pose_bone=pose_bone.parent, matrix=pose_bone.parent.matrix, from_space="POSE", to_space="WORLD"
                )

            pose_matrix = rig.convert_space(
                pose_bone=pose_bone, matrix=pose_bone.matrix, from_space="POSE", to_space="WORLD"
            )
            offset_matrix = parent_matrix.inverted_safe() @ pose_matrix
            _translation, _rotation, _scale = swap_coord_space(offset_matrix).decompose()

            frames_data[bone.name].append((_translation, _rotation, _scale))

    frames_data = dict(frames_data)

    all_bone_keyframes = OrderedDict()
    for bone in export_bones:
        all_bone_keyframes[bone.name] = dict()

    for bone in export_bones:
        t_list, q_list, s_list = zip(*frames_data[bone.name])
        t_list = [t.freeze() for t in t_list]
        q_list = [q.freeze() for q in q_list]
        s_list = [s.freeze() for s in s_list]

        q_list = [(q.x, q.y, q.z, q.w) for q in q_list]

        for attr, attr_list in zip(["t", "q", "s"], [t_list, q_list, s_list]):
            if len(set(attr_list)) != 1:
                all_bone_keyframes[bone.name][attr] = attr_list

    return all_bone_keyframes


def swap_coord_space(data, space_mat=SPACE_MATRIX, space_mat_inv=SPACE_MATRIX_INV):
    if isinstance(data, Matrix):
        return space_mat @ data.to_4x4() @ space_mat_inv
    elif isinstance(data, Quaternion):
        mat = data.to_matrix()
        return (space_mat @ mat.to_4x4() @ space_mat_inv).to_quaternion()
    elif isinstance(data, Vector) or len(data) == 3:
        vec = Vector(data)
        return vec @ space_mat
    elif len(data) == 2:
        return data[0], 1 - data[1]
    else:
        raise NotImplementedError("Unknown data type encountered.")


def mesh_settings_for(overrides, node_tag, mesh_index):
    short_tag = node_tag.split("|")[-1]
    merged = {}
    for name, index, settings in overrides:
        if name in (None, node_tag, short_tag) and index in (None, mesh_index):
            merged.update(settings)
    return merged


def create_node_texture(node_tree, tex_filepath, as_data=False):
    teximage_node = node_tree.nodes.new("ShaderNodeTexImage")

    if tex_filepath is not None:
        texture_name = os.path.basename(tex_filepath)

        try:
            new_image = bpy.data.images.load(tex_filepath, check_existing=True)
        except RuntimeError:
            if texture_name in bpy.data.images:
                new_image = bpy.data.images[texture_name]
            else:
                new_image = bpy.data.images.new(texture_name, 32, 32)
                new_image.source = "FILE"
            teximage_node.color = (1, 0, 0)
            teximage_node.use_custom_color = True

        if not os.path.isfile(tex_filepath):
            IO_PDX_LOG.warning(f"Unable to find texture filepath - '{tex_filepath}'")

        new_image.use_fake_user = True
        new_image.alpha_mode = "CHANNEL_PACKED"
        new_image.colorspace_settings.is_data = as_data

        teximage_node.name = texture_name
        teximage_node.image = new_image

    return teximage_node


def shader_traits(shader, lookup=None):
    info = lookup(shader) if lookup else None
    if info is not None:
        return info.blend, info.emissive
    name = shader.lower()
    blend = "additive" if "additive" in name else "blend" if "alpha" in name else None
    return blend, "ship" in name or "emissive" in name


def create_shader(PDX_material, shader_name, texture_dir, template_only=False, shader_info=None):
    def texture_file(name):
        return texture_dir(name) if callable(texture_dir) else os.path.join(texture_dir, name)

    new_shader = bpy.data.materials.new(shader_name)
    new_shader[PDX_SHADER] = PDX_material.shader[0]
    new_shader.use_fake_user = True
    if bpy.app.version < (5, 0, 0):
        new_shader.use_nodes = True

    blend, emissive = shader_traits(PDX_material.shader[0], shader_info)
    albedo_texture = normal_blue = None

    new_shader.use_backface_culling = True
    new_shader.blend_method = "BLEND" if blend == "additive" else "CLIP"
    if blend == "additive" and hasattr(new_shader, "surface_render_method"):
        new_shader.surface_render_method = "BLENDED"

    try:
        new_shader.shadow_method = "CLIP"
    except AttributeError:
        pass  

    def set_node_pos(node, x, y):
        node.location = Vector((x * 300.0, y * -300.0))

    node_tree = new_shader.node_tree
    nodes, links = node_tree.nodes, node_tree.links

    shader_root = nodes.get("Principled BSDF")
    if shader_root is None:
        nodes.clear()
        output = nodes.new(type="ShaderNodeOutputMaterial")
        shader_root = nodes.new(type="ShaderNodeBsdfPrincipled")

        links.new(shader_root.outputs["BSDF"], output.inputs["Surface"])
        set_node_pos(output, 1, 0)

    if getattr(PDX_material, "diff", None) or template_only:
        texture_path = None if template_only else texture_file(PDX_material.diff[0])

        albedo_texture = create_node_texture(node_tree, texture_path)
        set_node_pos(albedo_texture, -5, 0)

        links.new(albedo_texture.outputs["Color"], shader_root.inputs["Base Color"])
        if blend in ("blend", "cutout"):
            links.new(albedo_texture.outputs["Alpha"], shader_root.inputs["Alpha"])

    if getattr(PDX_material, "spec", None) or template_only:
        texture_path = None if template_only else texture_file(PDX_material.spec[0])

        material_texture = create_node_texture(node_tree, texture_path, as_data=True)
        set_node_pos(material_texture, -5, 1)

        separate_color = node_tree.nodes.new(type="ShaderNodeSeparateColor")
        separate_color.mode = "RGB"
        set_node_pos(separate_color, -4, 1)

        links.new(material_texture.outputs["Color"], separate_color.inputs["Color"])
        try:
            links.new(separate_color.outputs["Green"], shader_root.inputs["Specular IOR Level"])
        except KeyError:
            links.new(separate_color.outputs["Green"], shader_root.inputs["Specular"])
        links.new(separate_color.outputs["Blue"], shader_root.inputs["Metallic"])
        links.new(material_texture.outputs["Alpha"], shader_root.inputs["Roughness"])

    if getattr(PDX_material, "n", None) or template_only:
        texture_path = None if template_only else texture_file(PDX_material.n[0])

        normal_texture = create_node_texture(node_tree, texture_path, as_data=True)
        set_node_pos(normal_texture, -5, 2)

        separate_color = node_tree.nodes.new(type="ShaderNodeSeparateColor")
        separate_color.mode = "RGB"
        set_node_pos(separate_color, -4, 2)
        combine_color = node_tree.nodes.new(type="ShaderNodeCombineColor")
        combine_color.mode = "RGB"
        combine_color.inputs["Blue"].default_value = 1.0
        set_node_pos(combine_color, -3, 2)

        normal_map = node_tree.nodes.new("ShaderNodeNormalMap")
        set_node_pos(normal_map, -2, 2)

        links.new(normal_texture.outputs["Color"], separate_color.inputs["Color"])
        links.new(separate_color.outputs["Green"], combine_color.inputs["Red"])
        normal_blue = separate_color.outputs["Blue"]
        links.new(normal_texture.outputs["Alpha"], combine_color.inputs["Green"])
        links.new(combine_color.outputs["Color"], normal_map.inputs["Color"])
        links.new(normal_map.outputs["Normal"], shader_root.inputs["Normal"])

    if emissive and albedo_texture and normal_blue:
        links.new(albedo_texture.outputs["Color"], shader_root.inputs["Emission Color"])
        links.new(normal_blue, shader_root.inputs["Emission Strength"])

    if blend == "additive" and albedo_texture:
        transparent = nodes.new("ShaderNodeBsdfTransparent")
        emission = nodes.new("ShaderNodeEmission")
        add = nodes.new("ShaderNodeAddShader")
        output = next(n for n in nodes if n.type == "OUTPUT_MATERIAL")
        links.new(albedo_texture.outputs["Color"], emission.inputs["Color"])
        links.new(albedo_texture.outputs["Alpha"], emission.inputs["Strength"])
        links.new(transparent.outputs["BSDF"], add.inputs[0])
        links.new(emission.outputs["Emission"], add.inputs[1])
        links.new(add.outputs["Shader"], output.inputs["Surface"])
        set_node_pos(transparent, -2, -2)
        set_node_pos(emission, -2, -1)
        set_node_pos(add, -1, -1.5)
        set_node_pos(output, 0, -1.5)
        set_node_pos(shader_root, 0, 0.3)
        shader_root.mute = True
        shader_root.label = "Kept for export (not rendered)"

    return new_shader


def create_material(PDX_material, mesh, texture_path, shader_info=None):
    shader_name = "PDXmat_" + mesh.name
    shader = create_shader(PDX_material, shader_name, texture_path, shader_info=shader_info)

    mesh.materials.append(shader)


def create_locator(PDX_locator, PDX_bone_dict, rig=None, collection=None):
    new_loc = bpy.data.objects.new(PDX_locator.name, None)
    new_loc.empty_display_type = "PLAIN_AXES"
    new_loc.empty_display_size = 0.4
    new_loc.show_axis = False

    (collection or bpy.context.scene.collection).objects.link(new_loc)

    parent = getattr(PDX_locator, "pa", None)
    parent_Xform = None

    if parent is not None:
        parent_rig = rig if rig is not None else get_rig_from_bone_name(parent[0])
        if parent_rig:
            new_loc.parent = parent_rig
            new_loc.parent_bone = parent[0]
            new_loc.parent_type = "BONE"
            new_loc.matrix_world = Matrix()

        if parent[0] in PDX_bone_dict:
            transform = PDX_bone_dict[parent[0]]
            parent_Xform = Matrix(
                (
                    (transform[0], transform[3], transform[6], transform[9]),
                    (transform[1], transform[4], transform[7], transform[10]),
                    (transform[2], transform[5], transform[8], transform[11]),
                    (0.0, 0.0, 0.0, 1.0),
                )
            )
            loc, rot, scale = parent_Xform.decompose()
            try:
                parent_Xform = Matrix.Scale(1.0 / scale[0], 4) @ parent_Xform
            except ZeroDivisionError:
                parent_Xform = Matrix.Translation(loc) @ rot.to_matrix().to_4x4() @ Matrix.Scale(1.0, 4)
        else:
            IO_PDX_LOG.warning(
                f"Unable to create locator '{PDX_locator.name}' (missing parent '{parent[0]}' in file data)"
            )
            bpy.data.objects.remove(new_loc)
            return

    if hasattr(PDX_locator, "tx"):
        loc_matrix = Matrix((
            (PDX_locator.tx[0], PDX_locator.tx[4], PDX_locator.tx[8], PDX_locator.tx[12]),
            (PDX_locator.tx[1], PDX_locator.tx[5], PDX_locator.tx[9], PDX_locator.tx[13]),
            (PDX_locator.tx[2], PDX_locator.tx[6], PDX_locator.tx[10], PDX_locator.tx[14]),
            (PDX_locator.tx[3], PDX_locator.tx[7], PDX_locator.tx[11], PDX_locator.tx[15]),
        ))
    else:
        _scale = Matrix.Scale(1, 4)
        _rotation = (
            Quaternion((PDX_locator.q[3], PDX_locator.q[0], PDX_locator.q[1], PDX_locator.q[2])).to_matrix().to_4x4()
        )
        _translation = Matrix.Translation(PDX_locator.p)

        loc_matrix = _translation @ _rotation @ _scale

    if parent_Xform is not None:
        loc_matrix = (loc_matrix.transposed() @ parent_Xform.inverted_safe().transposed()).transposed()

    new_loc.matrix_world = swap_coord_space(loc_matrix)
    new_loc.rotation_mode = "XYZ"

    bpy.context.view_layer.update()

    return new_loc


def create_skeleton(PDX_bone_list, convert_bonespace=False, collection=None, reuse=True):
    bone_list = [None for _ in range(0, len(PDX_bone_list))]

    matching_rigs = [get_rig_from_bone_name(clean_imported_name(bone.name)) for bone in PDX_bone_list]
    matching_rigs = list(set(rig for rig in matching_rigs if rig))
    if reuse and len(matching_rigs) == 1:
        IO_PDX_LOG.debug("matching armature already found in scene")
        return matching_rigs[0]

    tmp_rig_name = "io_pdx_rig"

    armt = bpy.data.armatures.new("armature")
    armt.name = "imported_armature"
    armt.display_type = "STICK"

    new_rig = bpy.data.objects.new(tmp_rig_name, armt)
    (collection or bpy.context.scene.collection).objects.link(new_rig)
    bpy.context.view_layer.objects.active = new_rig
    new_rig.show_in_front = True
    new_rig.select_set(state=True)

    bpy.ops.object.mode_set(mode="EDIT")
    for bone in PDX_bone_list:
        index = bone.ix[0]
        transform = bone.tx
        parent = getattr(bone, "pa", None)

        unique_name = clean_imported_name(bone.name)

        new_bone = armt.edit_bones.new(name=unique_name)
        new_bone.select = True
        bone_list[index] = new_bone

        if parent is not None:
            parent_bone = bone_list[parent[0]]
            new_bone.parent = parent_bone
            new_bone.use_connect = False

        mat = Matrix((
            (transform[0], transform[3], transform[6], transform[9]),
            (transform[1], transform[4], transform[7], transform[10]),
            (transform[2], transform[5], transform[8], transform[11]),
            (0.0, 0.0, 0.0, 1.0),
        ))
        loc, rot, scale = mat.decompose()
        try:
            safemat = Matrix.Scale(1.0 / scale[0], 4) @ mat
        except ZeroDivisionError:
            IO_PDX_LOG.warning(f"Bad transform found on bone '{unique_name}' (defaulting to bone scale 1.0)")
            safemat = Matrix.Translation(loc) @ rot.to_matrix().to_4x4() @ Matrix.Scale(1.0, 4)

        bone_children = [b for b in PDX_bone_list if getattr(b, "pa", [None]) == bone.ix]
        bone_dists = []
        for child in bone_children:
            child_transform = child.tx
            c_mat = Matrix((
                (child_transform[0], child_transform[3], child_transform[6], child_transform[9]),
                (child_transform[1], child_transform[4], child_transform[7], child_transform[10]),
                (child_transform[2], child_transform[5], child_transform[8], child_transform[11]),
                (0.0, 0.0, 0.0, 1.0),
            ))
            c_dist = c_mat.to_translation() - safemat.to_translation()
            bone_dists.append(math.sqrt(c_dist.x**2 + c_dist.y**2 + c_dist.z**2))

        avg_dist = 5.0
        if bone_children:
            avg_dist = sum(bone_dists) / len(bone_dists)
        avg_dist = min(max(1.0, avg_dist), 10.0) * 0.05

        new_bone.tail = Vector((0.0, 0.0, avg_dist))
        new_bone.matrix = swap_coord_space(safemat.inverted_safe())
        if convert_bonespace:
            new_bone.matrix = swap_coord_space(safemat.inverted_safe()) @ BONESPACE_MATRIX

        IO_PDX_LOG.debug(f"New bone created - '{new_bone.name}'")

    for bone in bone_list:
        if bone.length == 0:
            bone.tail += Vector((0, 0, 0.1))

    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.context.view_layer.update()

    return new_rig


def create_skin(PDX_skin, PDX_bones, obj, rig, max_infs=None):
    if max_infs is None:
        max_infs = PDX_MAXSKININFS

    skin_dict = dict()

    num_infs = PDX_skin.bones[0]
    armt_bones = rig.data.bones

    for vtx in range(0, int(len(PDX_skin.ix) / max_infs)):
        skin_dict[vtx] = dict(joints=[], weights=[])

    for vtx, j in enumerate(range(0, len(PDX_skin.ix), max_infs)):
        skin_dict[vtx]["joints"] = PDX_skin.ix[j : j + num_infs]
        skin_dict[vtx]["weights"] = PDX_skin.w[j : j + num_infs]

    for bone in armt_bones:
        obj.vertex_groups.new(name=bone.name)

    for v in range(len(skin_dict.keys())):
        joints = [PDX_bones[j].name for j in skin_dict[v]["joints"]]
        weights = skin_dict[v]["weights"]
        try:
            norm_weights = [float(w) / sum(weights) for w in weights]
        except Exception as err:
            norm_weights = weights
            IO_PDX_LOG.error(err)
        joint_weights = [(j, w) for j, w in zip(joints, norm_weights) if w != 0.0]

        for joint, weight in joint_weights:
            obj.vertex_groups[clean_imported_name(joint)].add([v], weight, "REPLACE")

    skin_mod = obj.modifiers.new(rig.name + "_skin", "ARMATURE")
    skin_mod.object = rig
    skin_mod.use_bone_envelopes = False
    skin_mod.use_vertex_groups = True


def create_mesh(PDX_mesh, name=None, collection=None):
    tmp_mesh_name = "io_pdx_mesh"

    verts = PDX_mesh.p

    norms = None
    if hasattr(PDX_mesh, "n"):
        norms = PDX_mesh.n

    tris = PDX_mesh.tri

    uv_Ch = dict()
    for i, uv in enumerate(["u0", "u1", "u2", "u3"]):
        if hasattr(PDX_mesh, uv):
            uv_Ch[i] = getattr(PDX_mesh, uv)

    vertexArray = []
    for i in range(0, len(verts), 3):
        v = swap_coord_space([verts[i], verts[i + 1], verts[i + 2]])
        vertexArray.append(v)

    faceArray = []
    for i in range(0, len(tris), 3):
        f = [tris[i + 2], tris[i + 1], tris[i]]
        if len(set(f)) < 3:
            IO_PDX_LOG.warning(f"Skipping a face using a vertex twice in '{name or tmp_mesh_name}'")
            continue
        faceArray.append(f)

    new_mesh = bpy.data.meshes.new(tmp_mesh_name)

    new_mesh.from_pydata(vertexArray, [], faceArray)
    new_mesh.update()

    if name is None:
        mesh_name = tmp_mesh_name
    else:
        mesh_name = clean_imported_name(name)

    new_obj = bpy.data.objects.new(mesh_name, new_mesh)
    (collection or bpy.context.scene.collection).objects.link(new_obj)
    new_mesh.name = mesh_name
    new_obj.name = mesh_name.replace("Shape", "")

    if norms:
        normals = []
        for i in range(0, len(norms), 3):
            n = swap_coord_space([norms[i], norms[i + 1], norms[i + 2]])
            normals.append(n)

        new_mesh.polygons.foreach_set("use_smooth", [True] * len(new_mesh.polygons))
        new_mesh.normals_split_custom_set_from_vertices(normals)
        if hasattr(new_mesh, "use_auto_smooth"):
            new_mesh.use_auto_smooth = True

    for idx in uv_Ch:
        uvSetName = f"map{idx + 1}"
        new_mesh.uv_layers.new(name=uvSetName)

        uvArray = []
        uv_data = uv_Ch[idx]
        for i in range(0, len(uv_data), 2):
            uv = [uv_data[i], 1 - uv_data[i + 1]]
            uvArray.append(uv)

        bm = get_bmesh(new_mesh)
        uv_layer = bm.loops.layers.uv[uvSetName]

        for face in bm.faces:
            for loop in face.loops:
                i = loop.vert.index
                loop[uv_layer].uv = uvArray[i]

        bm.to_mesh(new_mesh)
        bm.free()

    bpy.ops.object.select_all(action="DESELECT")
    bpy.context.view_layer.objects.active = new_obj
    new_obj.select_set(state=True)

    return new_mesh, new_obj


def create_fcurve(armature, bone_name, data_type, array_idx):
    if armature.animation_data is None:
        armature.animation_data_create()
    anim_data = armature.animation_data

    if anim_data.action is None:
        anim_data.action = bpy.data.actions.new(armature.name + "_action")
    action = anim_data.action

    if hasattr(action, "slots") and anim_data.action_slot is None:
        anim_data.action_slot = action.slots[0] if len(action.slots) else action.slots.new("OBJECT", armature.name)

    if hasattr(action, "fcurves"):
        fcurves, groups, group_arg = action.fcurves, action.groups, "action_group"
    else:
        from bpy_extras import anim_utils

        channelbag = anim_utils.action_ensure_channelbag_for_slot(action, anim_data.action_slot)
        fcurves, groups, group_arg = channelbag.fcurves, channelbag.groups, "group_name"

    data_path = f'pose.bones["{bone_name}"].{data_type}'

    for curve in fcurves:
        if curve.data_path != data_path:
            continue
        if array_idx < 0 or curve.array_index == array_idx:
            return curve

    if bone_name not in groups:
        groups.new(bone_name)
    f_curve = fcurves.new(data_path, index=array_idx, **{group_arg: bone_name})

    return f_curve


def write_bone_curve(armature, bone_name, data_type, array_idx, frames, values):
    points = create_fcurve(armature, bone_name, data_type, array_idx).keyframe_points
    points.add(len(frames))
    co = [0.0] * (2 * len(frames))
    co[0::2], co[1::2] = frames, values
    points.foreach_set("co", co)
    for point in points:
        point.interpolation = "LINEAR"


def import_meshfile(meshpath, imp_mesh=True, imp_skel=True, imp_locs=True, join_materials=True, **kwargs):
    bonespace = kwargs.get("bonespace", False)
    collection = kwargs.get("collection")
    new_rig = kwargs.get("new_rig", False)
    texture_finder = kwargs.get("texture_finder")
    shader_info = kwargs.get("shader_info")
    texture_overrides = kwargs.get("texture_overrides") or []
    skip_collision = kwargs.get("skip_collision", False)

    start = perf_counter()
    IO_PDX_LOG.info(f"Importing - '{meshpath}'")
    result = {"objects": [], "rig": None, "locators": {}}
    own_rigs = []

    asset_elem = pdx_data.read_meshfile(meshpath)

    shapes = asset_elem.find("object")
    locators = asset_elem.find("locator")

    scene_bone_dict = dict()

    for i, node in enumerate(shapes):
        IO_PDX_LOG.info(f"Creating node [{i + 1}/{len(shapes)}] - '{node.tag}'")

        rig = None
        skeleton = node.find("skeleton")
        if skeleton:
            pdx_bone_list = list()
            for b in skeleton:
                pdx_bone = pdx_data.PDXData(b)
                pdx_bone_list.append(pdx_bone)
                scene_bone_dict[pdx_bone.name] = pdx_bone.tx

            if imp_skel:
                IO_PDX_LOG.info(f"Creating skeleton - {len(pdx_bone_list)} bones")
                bone_names = {clean_imported_name(b.name) for b in pdx_bone_list}
                rig = next((r for r in own_rigs if bone_names <= {b.name for b in r.data.bones}), None)
                if rig is None:
                    rig = create_skeleton(
                        pdx_bone_list, convert_bonespace=bonespace, collection=collection, reuse=not new_rig
                    )
                    own_rigs.append(rig)
                result["rig"] = result["rig"] or rig

        meshes = node.findall("mesh")
        if imp_mesh and meshes:
            created, collision_objects = [], []
            for mat_idx, m in enumerate(meshes):
                if "p" not in m.attrib or "tri" not in m.attrib:
                    IO_PDX_LOG.warning(f"Skipping mesh {mat_idx} of '{node.tag}', it has no geometry")
                    continue
                pdx_mesh = pdx_data.PDXData(m)
                pdx_material = getattr(pdx_mesh, "material", None)
                pdx_skin = getattr(pdx_mesh, "skin", None)
                mesh_settings = mesh_settings_for(texture_overrides, node.tag, mat_idx)
                shader = mesh_settings.get("shader") or (pdx_material.shader[0] if pdx_material else "")
                is_collision = shader.lower() == "collision"
                if is_collision and skip_collision:
                    IO_PDX_LOG.info(f"Skipping collision mesh {mat_idx} of '{node.tag}'")
                    continue
                IO_PDX_LOG.info(f"Creating mesh - {mat_idx}")

                if join_materials:
                    meshmaterial_name = node.tag if mat_idx == 0 else f"{node.tag}-{mat_idx:0>3}"
                else:
                    meshmaterial_name = f"{node.tag}-{mat_idx:0>3}"
                mesh, obj = create_mesh(pdx_mesh, name=meshmaterial_name, collection=collection)
                if is_collision:
                    obj["pdx_collision"], obj.display_type, obj.hide_render = True, "WIRE", True
                    collision_objects.append(obj)
                else:
                    created.append(obj)

                set_mesh_index(mesh, i)

                if pdx_material:
                    IO_PDX_LOG.info(f"Creating material - '{pdx_material.shader[0]}'")
                    for slot, name in mesh_settings.items():
                        setattr(pdx_material, slot, [name])
                    create_material(pdx_material, mesh, texture_finder or os.path.split(meshpath)[0], shader_info)

                if rig and pdx_skin:
                    IO_PDX_LOG.info("Creating skinning data -")
                    create_skin(pdx_skin, pdx_bone_list, obj, rig)

            if join_materials and len(created) > 1:
                ctx = bpy.context.copy()
                ctx["active_object"] = created[0]
                ctx["selected_editable_objects"] = created
                with bpy.context.temp_override(**ctx):
                    bpy.ops.object.join()
                del ctx
                created = created[:1]
            result["objects"].extend(created + collision_objects)

    if imp_locs and locators:
        for i, loc in enumerate(locators):
            IO_PDX_LOG.info(f"Creating locator [{i + 1}/{len(locators)}] - '{loc.tag}'")
            pdx_locator = pdx_data.PDXData(loc)
            obj = create_locator(
                pdx_locator, scene_bone_dict, rig=result["rig"] if new_rig else None, collection=collection
            )
            if obj:
                result["locators"][loc.tag] = obj

    bpy.ops.object.select_all(action="DESELECT")
    IO_PDX_LOG.info(f"Import finished! ({perf_counter() - start:0.4f} sec)")
    return result


@allow_debug_logging
def export_meshfile(meshpath, exp_mesh=True, exp_skel=True, exp_locs=True, exp_selected=False, **kwargs):
    as_blendshape = kwargs.get("as_blendshape", False)
    split_by = ["id", "p", "uv"] if as_blendshape else None
    split_verts = kwargs.get("split_verts", False)
    sort_verts = {"+": True, "~": None, "-": False}.get(kwargs.get("sort_verts", "+"))
    plain_txt = kwargs.get("plain_txt", False)

    start = perf_counter()
    IO_PDX_LOG.info(f"Exporting - '{meshpath}'")

    root_xml = Xml.Element("File")
    root_xml.set("pdxasset", [1, 0])

    object_xml = Xml.SubElement(root_xml, "object")

    if exp_mesh:
        blender_meshes = list_scene_pdx_meshes()
        if exp_selected:
            blender_meshes = [obj for obj in blender_meshes if obj.select_get()]

        if len(blender_meshes) == 0:
            raise RuntimeError("Mesh export is selected, but found no meshes with PDX materials applied.")

        blender_meshes.sort(key=lambda obj: get_mesh_index(obj.data))

        for i, obj in enumerate(blender_meshes):
            obj_name = obj.data.name

            IO_PDX_LOG.info(f"Writing node [{i + 1}/{len(blender_meshes)}] - '{obj_name}'")
            objnode_xml = Xml.SubElement(object_xml, obj_name)

            lod_match = get_lod_level(obj_name)
            if lod_match is not None:
                IO_PDX_LOG.info(f"Writing lod index - {lod_match}")
                objnode_xml.set("lod", [lod_match])

            materials = list(obj.data.materials)

            for mat_idx, blender_mat in enumerate(materials):
                if blender_mat is None or PDX_SHADER not in blender_mat.keys():
                    continue

                IO_PDX_LOG.info(f"Writing mesh - {mat_idx}")
                meshnode_xml = Xml.SubElement(objnode_xml, "mesh")

                mesh_info_dict, vert_ids = get_mesh_info(
                    obj, mat_idx, split_criteria=split_by, split_all=split_verts, sort_vertices=sort_verts
                )
                if not (mesh_info_dict and vert_ids):
                    continue

                for key in ["p", "n", "ta", "u0", "u1", "u2", "u3", "tri", "boundingsphere"]:
                    if key in mesh_info_dict and mesh_info_dict[key]:
                        meshnode_xml.set(key, mesh_info_dict[key])

                aabbnode_xml = Xml.SubElement(meshnode_xml, "aabb")
                for key in ["min", "max"]:
                    if key in mesh_info_dict and mesh_info_dict[key]:
                        aabbnode_xml.set(key, mesh_info_dict[key])

                IO_PDX_LOG.info("Writing material -")
                materialnode_xml = Xml.SubElement(meshnode_xml, "material")
                materialnode_xml.set("shader", [get_material_shader(blender_mat)])
                mat_texture_dict = get_material_textures(blender_mat)
                for slot, texture in mat_texture_dict.items():
                    materialnode_xml.set(slot, [os.path.split(texture)[1]])

                skin_info_dict = get_mesh_skin_info(obj, vert_ids)
                if exp_skel and skin_info_dict:
                    IO_PDX_LOG.info("Writing skinning data -")
                    skinnode_xml = Xml.SubElement(meshnode_xml, "skin")
                    for key in ["bones", "ix", "w"]:
                        if key in skin_info_dict and skin_info_dict[key]:
                            skinnode_xml.set(key, skin_info_dict[key])

            bone_info_list = get_mesh_skeleton_info(obj)
            if exp_skel and bone_info_list:
                IO_PDX_LOG.info("Writing skeleton -")
                skeletonnode_xml = Xml.SubElement(objnode_xml, "skeleton")

                for bone_info_dict in bone_info_list:
                    bonenode_xml = Xml.SubElement(skeletonnode_xml, bone_info_dict["name"])
                    for key in ["ix", "pa", "tx"]:
                        if key in bone_info_dict and bone_info_dict[key]:
                            bonenode_xml.set(key, bone_info_dict[key])

    if exp_skel and not exp_mesh:
        obj_name = "skel_frame"
        IO_PDX_LOG.info(f"Writing node - '{obj_name}'")
        objnode_xml = Xml.SubElement(object_xml, obj_name)

        blender_rigs = [obj for obj in bpy.data.objects if isinstance(obj.data, bpy.types.Armature)]
        if exp_selected:
            blender_rigs = [obj for obj in blender_rigs if obj.select_get()]

        if len(blender_rigs) > 1:
            raise RuntimeError(f"Unable to resolve a single armature for export. ({blender_rigs})")

        rig_bones = get_skeleton_hierarchy(blender_rigs[0])

        if len(rig_bones) == 0:
            raise RuntimeError("Skeleton only export is selected, but found no bones.")

        bone_info_list = get_bones_info(rig_bones)
        if exp_skel and bone_info_list:
            IO_PDX_LOG.info("Writing skeleton -")
            skeletonnode_xml = Xml.SubElement(objnode_xml, "skeleton")

            for bone_info_dict in bone_info_list:
                bonenode_xml = Xml.SubElement(skeletonnode_xml, bone_info_dict["name"])
                for key in ["ix", "pa", "tx"]:
                    if key in bone_info_dict and bone_info_dict[key]:
                        bonenode_xml.set(key, bone_info_dict[key])

    locator_xml = Xml.SubElement(root_xml, "locator")

    if exp_locs:
        blender_empties = [obj for obj in bpy.context.scene.objects if obj.data is None]
        if exp_selected:
            blender_empties = [obj for obj in blender_empties if obj.select_get()]

        loc_info_list = get_locators_info(blender_empties)
        IO_PDX_LOG.info("Writing locators -")

        for loc_info_dict in loc_info_list:
            locnode_xml = Xml.SubElement(locator_xml, loc_info_dict["name"])
            for key in ["p", "q", "pa", "tx"]:
                if key in loc_info_dict and loc_info_dict[key]:
                    locnode_xml.set(key, loc_info_dict[key])

    IO_PDX_LOG.info("Writing .mesh file -")
    pdx_data.write_meshfile(meshpath, root_xml)
    if plain_txt:
        IO_PDX_LOG.info("Writing .txt file -")
        Path(meshpath).with_suffix(".txt").write_text(f"{pdx_data.PDXData(root_xml)}\n")

    if bpy.ops.object.mode_set.poll():
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    IO_PDX_LOG.info(f"Export finished! ({perf_counter() - start:0.4f} sec)")


@allow_debug_logging
def import_animfile(animpath, frame_start=1, **kwargs):
    rig = kwargs.get("rig")
    action_name = kwargs.get("action_name") or Path(animpath).stem
    set_scene = kwargs.get("set_scene", True)

    start = perf_counter()
    IO_PDX_LOG.info(f"Importing - '{animpath}'")

    asset_elem = pdx_data.read_meshfile(animpath)

    info = asset_elem.find("info")
    samples = asset_elem.find("samples")
    framecount = info.attrib["sa"][0]
    fps = int(info.attrib["fps"][0])

    if set_scene:
        IO_PDX_LOG.info(f"Setting playback speed - {fps}")
        try:
            bpy.context.scene.render.fps = fps
        except Exception as err:
            IO_PDX_LOG.error(err)
            raise RuntimeError(f"Unsupported animation speed. ({fps})")
        bpy.context.scene.render.fps_base = 1.0

        IO_PDX_LOG.info(f"Setting playback range - ({frame_start},{frame_start + framecount - 1})")
        bpy.context.scene.frame_start = frame_start
        bpy.context.scene.frame_end = frame_start + framecount - 1

    if rig is None:
        IO_PDX_LOG.info("Finding armature -")
        matching_rigs = [get_rig_from_bone_name(clean_imported_name(bone.tag)) for bone in info]
        matching_rigs = list(set(rig for rig in matching_rigs if rig))

        if len(matching_rigs) != 1:
            raise RuntimeError(f"Missing unique armature required for animation: {matching_rigs}")
        rig = matching_rigs[0]

    IO_PDX_LOG.info("Finding bones -")
    scale_length = set()
    bone_errors = []
    for bone in info:
        scale_length.add(len(bone.attrib["s"]))
        bone_name = clean_imported_name(bone.tag)
        if bone_name not in rig.pose.bones:
            bone_errors.append(bone_name)
            IO_PDX_LOG.warning(f"Failed to find bone - {bone_name}")

    if bone_errors:
        raise RuntimeError(f"Missing bones required for animation: {bone_errors}")

    if len(scale_length) != 1:
        raise NotImplementedError(f"Mixed length scale vectors ({scale_length}) are not supported")
    scale_length = scale_length.pop()
    scale_padding = 4 - scale_length
    IO_PDX_LOG.info(f"Animation supports {'non-' if scale_length > 1 else ''}uniform scale -")

    all_bone_keyframes = OrderedDict()
    for bone in info:
        bone_name = clean_imported_name(bone.tag)
        all_bone_keyframes[bone_name] = {sample_type: [] for sample_type in bone.attrib["sa"][0]}

    s_idx, q_idx, t_idx = 0, 0, 0
    s_len, q_len, t_len = scale_length, 4, 3
    for _ in range(0, framecount):
        for bone_name in all_bone_keyframes:
            bone_key_data = all_bone_keyframes[bone_name]
            if "s" in bone_key_data:
                frame_bone_scale = samples.attrib["s"][s_idx : s_idx + s_len] * scale_padding
                bone_key_data["s"].append(frame_bone_scale)
                s_idx += s_len
            if "q" in bone_key_data:
                frame_bone_quat = samples.attrib["q"][q_idx : q_idx + q_len]
                bone_key_data["q"].append(frame_bone_quat)
                q_idx += q_len
            if "t" in bone_key_data:
                frame_bone_trans = samples.attrib["t"][t_idx : t_idx + t_len]
                bone_key_data["t"].append(frame_bone_trans)
                t_idx += t_len

    action = bpy.data.actions.new(action_name)
    action.use_fake_user = True
    action["pdx_armature"] = rig.data.name
    if rig.animation_data is None:
        rig.animation_data_create()
    rig.animation_data.action = action

    frames = list(range(int(frame_start), int(frame_start) + framecount))
    for bone in info:
        bone_name = clean_imported_name(bone.tag)
        pose_bone = rig.pose.bones[bone_name]
        pose_bone.rotation_mode = "QUATERNION"
        edit_bone = pose_bone.bone

        parent_rest = edit_bone.parent.matrix_local if edit_bone.parent else Matrix()
        rest_inverse = (parent_rest.inverted_safe() @ edit_bone.matrix_local).inverted_safe()
        sampled = all_bone_keyframes[bone_name]
        duration = set(len(keyframes) for keyframes in sampled.values())
        if len(duration) > 1:
            raise RuntimeError(f"Inconsistent keyframe animation lengths across attributes (on '{bone_name}')")

        initial_s = bone.attrib["s"] * scale_padding
        initial_q, initial_t = bone.attrib["q"], bone.attrib["t"]

        def local_channels(t, q, s):
            rotation = Quaternion((q[3], q[0], q[1], q[2])).to_matrix().to_4x4()
            offset = swap_coord_space(Matrix.Translation(t) @ rotation @ Matrix.Diagonal(s).to_4x4())
            return (rest_inverse @ offset).decompose()

        rest_pose = local_channels(initial_t, initial_q, initial_s)
        locations, rotations, scales = [], [], []
        for k in range(duration.pop() if duration else 0):
            location, rotation, scale = local_channels(
                sampled["t"][k] if "t" in sampled else initial_t,
                sampled["q"][k] if "q" in sampled else initial_q,
                sampled["s"][k] if "s" in sampled else initial_s,
            )
            if rotations and rotations[-1].dot(rotation) < 0:
                rotation.negate()
            locations.append(location)
            rotations.append(rotation)
            scales.append(scale)

        for data_type, attr, size, series, static in (
            ("location", "t", 3, locations, rest_pose[0]),
            ("rotation_quaternion", "q", 4, rotations, rest_pose[1]),
            ("scale", "s", 3, scales, rest_pose[2]),
        ):
            for i in range(size):
                if attr in sampled:
                    write_bone_curve(rig, bone_name, data_type, i, frames, [v[i] for v in series])
                else:
                    write_bone_curve(rig, bone_name, data_type, i, frames[:1], [static[i]])

        first = (locations[0], rotations[0], scales[0]) if locations else rest_pose
        pose_bone.location, pose_bone.rotation_quaternion, pose_bone.scale = first

    if set_scene:
        bpy.context.scene.frame_set(frame_start)
        bpy.context.view_layer.update()
        bpy.ops.object.select_all(action="DESELECT")
    IO_PDX_LOG.info(f"Import finished! ({perf_counter() - start:0.4f} sec)")
    return {"action": action, "rig": rig, "fps": fps, "frames": framecount}


@allow_debug_logging
def export_animfile(animpath, frame_start=1, frame_end=10, **kwargs):
    uniform_scale = kwargs.get("uniform_scale", True)
    plain_txt = kwargs.get("plain_txt", False)

    start = perf_counter()
    IO_PDX_LOG.info(f"Exporting - {animpath}")

    curr_frame = bpy.context.scene.frame_start
    if frame_start != int(frame_start) or frame_end != int(frame_end):
        raise RuntimeError(
            f"Invalid animation range selected ({frame_start},{frame_end}). Only whole frames are supported."
        )
    frame_start = int(frame_start)
    frame_end = int(frame_end)

    root_xml = Xml.Element("File")
    root_xml.set("pdxasset", [1, 0])

    info_xml = Xml.SubElement(root_xml, "info")

    IO_PDX_LOG.info("Gathering animation info -")
    fps = bpy.context.scene.render.fps
    info_xml.set("fps", [float(fps)])

    frame_samples = (frame_end + 1) - frame_start
    info_xml.set("sa", [frame_samples])

    rig = None

    rig = bpy.context.active_object
    if rig is None:
        raise RuntimeError("Please select a specific armature before exporting.")

    export_bones = get_skeleton_hierarchy(rig)
    info_xml.set("j", [len(export_bones)])

    all_bone_keyframes = get_scene_animdata(rig, export_bones, frame_start, frame_end)

    IO_PDX_LOG.info("Gathering initial bone transforms -")
    bpy.context.scene.frame_set(frame_start)
    for bone in export_bones:
        pose_bone = rig.pose.bones[bone.name]
        bone_xml = Xml.SubElement(info_xml, pose_bone.name)

        sample_types = ""
        for attr in ["t", "q", "s"]:
            if attr in all_bone_keyframes[pose_bone.name]:
                sample_types += attr
        bone_xml.set("sa", [sample_types])

        parent_matrix = Matrix()
        if pose_bone.parent:
            parent_matrix = pose_bone.parent.matrix.copy()

        offset_matrix = parent_matrix.inverted_safe() @ pose_bone.matrix
        _translation, _rotation, _scale = swap_coord_space(offset_matrix).decompose()

        _rotation = [_rotation.x, _rotation.y, _rotation.z, _rotation.w]
        if uniform_scale:
            _scale = [_scale[0]]

        bone_xml.set("t", list(_translation))
        bone_xml.set("q", list(_rotation))
        bone_xml.set("s", list(_scale))

    samples_xml = Xml.SubElement(root_xml, "samples")
    IO_PDX_LOG.info("Gathering keyframes -")
    for bone_name in all_bone_keyframes:
        bone_keys = all_bone_keyframes[bone_name]
        if bone_keys:
            IO_PDX_LOG.info(f"Gathering {list(bone_keys)} keyframes for bone - '{bone_name}'")

    t_packed, q_packed, s_packed = [], [], []
    for _ in range(frame_samples):
        for bone in all_bone_keyframes:
            bone_key_data = all_bone_keyframes[bone]
            if "t" in bone_key_data:
                t_packed.extend(bone_key_data["t"].pop(0))
            if "q" in bone_key_data:
                q_packed.extend(bone_key_data["q"].pop(0))
            if "s" in bone_key_data:
                if uniform_scale:
                    s_packed.append(bone_key_data["s"].pop(0)[0])
                else:
                    s_packed.extend(bone_key_data["s"].pop(0))

    if t_packed:
        samples_xml.set("t", t_packed)
    if q_packed:
        samples_xml.set("q", q_packed)
    if s_packed:
        samples_xml.set("s", s_packed)

    IO_PDX_LOG.info("Writing .anim file")
    pdx_data.write_animfile(animpath, root_xml)
    if plain_txt:
        IO_PDX_LOG.info("Writing .txt file -")
        Path(animpath).with_suffix(".txt").write_text(f"{pdx_data.PDXData(root_xml)}\n")

    bpy.context.scene.frame_set(curr_frame)

    bpy.ops.object.select_all(action="DESELECT")
    IO_PDX_LOG.info(f"Export finished! ({perf_counter() - start:0.4f} sec)")
