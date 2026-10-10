"""Importing entities from .asset files."""

import math

import bpy
from mathutils import Matrix

from .. import IO_PDX_LOG
from ..clausewitz_text import to_float
from .blender_import_export import import_animfile, import_meshfile, swap_coord_space

TEXTURE_SLOTS = {"texture_diffuse": "diff", "texture_specular": "spec", "texture_normal": "n", "shader": "shader"}
NOT_FOUND = ("entities", "pdxmeshes", "mesh files", "attach points")


class Report:
    def __init__(self):
        self.instances = 0
        self.meshes = 0
        self.animations = 0
        self.unresolved = {}

    def missing(self, kind, name):
        self.unresolved.setdefault(kind, set()).add(name)

    def lines(self):
        lines = [f"{self.instances} entities placed, {self.meshes} meshes"]
        if self.animations:
            lines[0] += f", {self.animations} animations"
        for kind, names in sorted(self.unresolved.items()):
            label = f"{kind} not found" if kind in NOT_FOUND else kind
            lines.append(f"{len(names)} {label}, e.g. {sorted(names)[0]}")
        return lines


class Node:
    def __init__(self, name, root, parent=None):
        self.name = name
        self.root = root
        self.parent = parent
        self.rig = None
        self.targets = {}

    def lineage(self):
        node = self
        while node is not None:
            yield node.name
            node = node.parent


# rotation = { yaw pitch roll } in degrees, applied roll (Z), then yaw (Y), then pitch (X). Checked in game with
# tests/ingame/blender_io_rotation_test.asset.
def rotation_from_angles(rotation, size=4):
    yaw, pitch, roll = (math.radians(a) for a in rotation)
    return Matrix.Rotation(pitch, size, "X") @ Matrix.Rotation(yaw, size, "Y") @ Matrix.Rotation(roll, size, "Z")


def local_matrix(position, rotation, scale):
    return swap_coord_space(Matrix.Translation(position) @ rotation_from_angles(rotation) @ Matrix.Scale(scale, 4))


def new_empty(name, collection, size=0.4):
    empty = bpy.data.objects.new(name, None)
    empty.empty_display_type = "PLAIN_AXES"
    empty.empty_display_size = size
    collection.objects.link(empty)
    return empty


def texture_overrides(*layers):
    result = []
    for blocks in layers:
        entries = []
        for block in blocks:
            name, index = block.get("name"), to_float(block.get("index"))
            slots = {slot: block.get(key) for key, slot in TEXTURE_SLOTS.items() if isinstance(block.get(key), str)}
            if slots:
                entries.append((name, None if index is None else int(index), slots))
        entries.sort(key=lambda entry: (entry[0] is not None) + (entry[1] is not None))
        result += entries
    return result


class Template:
    def __init__(self, imported):
        self.rig = imported["rig"]
        self.actions = {}
        self.locators = imported["locators"]
        self.objects = ([imported["rig"]] if imported["rig"] else []) + list(imported["objects"])
        self.objects += list(self.locators.values())


class Playback:
    def __init__(self):
        self.fps = None
        self.frames = 0


def assign_action(rig, action):
    data = rig.animation_data or rig.animation_data_create()
    data.action = action
    if action is not None and getattr(data, "action_slot", 1) is None and len(action.slots):
        data.action_slot = action.slots[0]


def rig_of(obj):
    while obj is not None:
        if obj.type == "ARMATURE":
            return obj
        found = obj.find_armature()
        if found is not None:
            return found
        obj = obj.parent
    return None


def actions_for(rig):
    found = [action for action in bpy.data.actions if action.get("pdx_armature") == rig.data.name]
    return sorted(found, key=lambda action: action_label(action).lower())


def action_label(action):
    return action.get("pdx_label") or action.name


def find_layer_collection(collection, layer_collection=None):
    stack = [layer_collection or bpy.context.view_layer.layer_collection]
    while stack:
        found = stack.pop()
        if found.collection is collection:
            return found
        stack.extend(found.children)
    return None


def wanted_animations(entity, mode):
    default = next((anim for name, anim in entity.states if name == entity.default_state and anim), None)
    default = default or next((anim for _, anim in entity.states if anim), None)
    ids = [default] if default else []
    if mode == "all":
        ids += [anim for _, anim in entity.states if anim and anim not in ids]
    return ids, default


def apply_animations(index, entity, pdxmesh, node, template, report, playback, mode):
    if node.rig is None or template.rig is None:
        return
    ids, default = wanted_animations(entity, mode)
    active = None
    for anim_id in ids:
        path = index.animations.get(pdxmesh.animations.get(anim_id))
        if path is None or not path.is_file():
            report.missing("animations", f"{anim_id} of {entity.name}")
            continue
        if path not in template.actions:
            try:
                result = import_animfile(
                    str(path), rig=template.rig, action_name=f"{pdxmesh.name}.{anim_id}", set_scene=False
                )
            except Exception as err:
                IO_PDX_LOG.error(f"Failed to import '{path.name}': {err}")
                template.actions[path] = None
                report.missing("animations that failed to import", path.name)
            else:
                template.actions[path] = result["action"]
                result["action"]["pdx_label"] = anim_id
                report.animations += 1
                playback.fps = playback.fps or result["fps"]
        if anim_id == default:
            active = template.actions[path]
    assign_action(node.rig, active)
    if active is not None:
        playback.frames = max(playback.frames, int(active.frame_range[1]))


def attach(obj, target, matrix):
    parent, offset, bone = target
    obj.parent = parent
    if bone is None:
        obj.matrix_basis = offset @ matrix
    else:
        obj.parent_type, obj.parent_bone = "BONE", bone.name
        obj.matrix_parent_inverse = Matrix.Translation((0.0, -bone.length, 0.0))
        obj.matrix_basis = matrix


def collection_for(collection, obj):
    if not obj.get("pdx_collision"):
        return collection
    name = f"{collection.name} collision"[:63]
    inner = collection.children.get(name)
    if inner is None:
        inner = bpy.data.collections.new(name)
        collection.children.link(inner)
    return inner


def copy_template(objects, parent, collection):
    copies = {}
    for obj in objects:
        copy = bpy.data.objects.new(obj.name, obj.data)
        copy.empty_display_type, copy.empty_display_size = obj.empty_display_type, obj.empty_display_size
        copy.show_in_front, copy.rotation_mode = obj.show_in_front, obj.rotation_mode
        copy.matrix_basis = obj.matrix_basis
        if obj.get("pdx_collision"):
            copy["pdx_collision"], copy.display_type, copy.hide_render = True, obj.display_type, obj.hide_render
        collection_for(collection, obj).objects.link(copy)
        copies[obj] = copy
    for obj, copy in copies.items():
        if obj.parent in copies:
            copy.parent, copy.parent_type, copy.parent_bone = copies[obj.parent], obj.parent_type, obj.parent_bone
        else:
            copy.parent = parent
        copy.matrix_parent_inverse = obj.matrix_parent_inverse.copy()
        for modifier in obj.modifiers:
            if modifier.type == "ARMATURE":
                made = copy.modifiers.new(modifier.name, "ARMATURE")
                made.object = copies.get(modifier.object, modifier.object)
                made.use_vertex_groups = modifier.use_vertex_groups
                made.use_bone_envelopes = modifier.use_bone_envelopes
    return copies


def import_entity(index, entity_name, collection=None, textures=True, animations=None, collision=False):
    report = Report()
    playback = Playback()
    if index.entity(entity_name) is None:
        raise ValueError(f"Entity '{entity_name}' is not defined in the game or mod folders.")

    if collection is None:
        collection = bpy.data.collections.new(entity_name[:63])
        bpy.context.scene.collection.children.link(collection)

    templates = {}
    stack = [(entity_name, None, None)]
    first = None
    while stack:
        name, target, parent_node = stack.pop()
        entity = index.entity(name)
        if entity is None:
            report.missing("entities", name)
            continue
        if parent_node is not None and name in parent_node.lineage():
            report.missing("entities attached to themselves", name)
            continue

        root = new_empty(name, collection, 1.0)
        root["pdx_entity"], root["pdx_file"] = name, str(entity.source)
        scale = Matrix.Scale(entity.scale, 4)
        if target is None:
            root.matrix_basis = scale
        else:
            attach(root, target, scale)
        node = Node(name, root, parent_node)
        node.targets["root"] = (root, Matrix(), None)
        first = root if first is None else first
        report.instances += 1

        build_mesh(index, entity, node, collection, templates, report, textures, animations, playback, collision)

        for locator in entity.locators.values():
            made = new_empty(locator.name, collection)
            made["pdx_locator"] = locator.name
            matrix = local_matrix(locator.position, locator.rotation, locator.scale)
            if locator.parent_joint and node.rig and locator.parent_joint in node.rig.data.bones:
                attach(made, (node.rig, Matrix(), node.rig.data.bones[locator.parent_joint]), matrix)
            else:
                made.parent = root
                made.matrix_basis = matrix
            node.targets[locator.name] = (made, Matrix(), None)

        for key, child in reversed(entity.attach):
            if key not in node.targets:
                report.missing("attach points", f"{key} on {name}")
                continue
            stack.append((child, node.targets[key], node))

    if playback.fps:
        scene = bpy.context.scene
        scene.render.fps, scene.render.fps_base = playback.fps, 1.0
        scene.frame_start, scene.frame_end = 1, max(playback.frames, 1)
    inner = collection.children.get(f"{collection.name} collision"[:63])
    layer = find_layer_collection(inner) if inner is not None else None
    if layer is not None:
        layer.hide_viewport = True
    return collection, first, report


def build_mesh(index, entity, node, collection, templates, report, textures, animations, playback, collision):
    if not entity.pdxmesh:
        return
    pdxmesh = index.pdxmeshes.get(entity.pdxmesh)
    if pdxmesh is None:
        report.missing("pdxmeshes", entity.pdxmesh)
        return
    meshpath = index.resolve_file(pdxmesh.file or "")
    if meshpath is None:
        report.missing("mesh files", pdxmesh.file)
        return

    overrides = texture_overrides(pdxmesh.meshsettings, entity.meshsettings) if textures else []
    key = (meshpath, repr(overrides))
    first_use = key not in templates
    if first_use:
        kwargs = {"shader_info": index.shader_info, "skip_collision": not collision}
        if textures:
            kwargs["texture_finder"] = lambda name: str(
                index.find_texture(name, meshpath.parent) or meshpath.parent / name
            )
            kwargs["texture_overrides"] = overrides
        existing = set(collection.objects)
        try:
            templates[key] = Template(import_meshfile(str(meshpath), collection=collection, new_rig=True, **kwargs))
        except Exception as err:
            IO_PDX_LOG.error(f"Failed to import '{meshpath.name}': {err}")
            for obj in set(collection.objects) - existing:
                bpy.data.objects.remove(obj, do_unlink=True)
            templates[key] = None
            report.missing("mesh files that failed to import", meshpath.name)
    if templates[key] is None:
        return

    holder = node.root
    if pdxmesh.scale != 1.0:
        holder = new_empty(f"{pdxmesh.name}_scale", collection)
        holder.parent = node.root
        holder.scale = (pdxmesh.scale,) * 3

    if first_use:
        copies = {obj: obj for obj in templates[key].objects}
        for obj in templates[key].objects:
            if obj.parent is None:
                obj.parent = holder
            inner = collection_for(collection, obj)
            if inner is not collection:
                collection.objects.unlink(obj)
                inner.objects.link(obj)
    else:
        copies = copy_template(templates[key].objects, holder, collection)

    template = templates[key]
    report.meshes += sum(1 for obj in copies.values() if obj.type == "MESH")
    rigs = [obj for obj in copies.values() if obj.type == "ARMATURE"]
    if rigs:
        node.rig = rigs[0]
        for bone in node.rig.data.bones:
            node.targets[bone.name] = (node.rig, Matrix(), bone)
    for tag, obj in template.locators.items():
        node.targets[tag] = (copies[obj], Matrix(), None)
    if animations:
        apply_animations(index, entity, pdxmesh, node, template, report, playback, animations)
    IO_PDX_LOG.info(f"Placed '{entity.name}' ({meshpath.name})")
