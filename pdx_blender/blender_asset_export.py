"""Save Asset and Update Asset."""

import math
import re
from pathlib import Path

import bpy
from mathutils import Euler, Matrix

from ..asset_writer import AssetText, format_entity, format_locator, read_text, scan, write_text
from ..clausewitz_text import parse
from ..pdx_asset import find_mod_root
from ..pdx_data import PDXData, read_meshfile
from .blender_asset import attach, local_matrix, new_empty, rotation_from_angles
from .blender_import_export import SPACE_MATRIX, SPACE_MATRIX_INV, clean_imported_name, swap_coord_space

POSITION_TOLERANCE = 1e-3
ROTATION_TOLERANCE = 1e-4
SCALE_TOLERANCE = 1e-4
NAME_CHARACTERS = re.compile(r"[^\w.\-]")


# Children of a bone hang from its tail, the importer puts them back at the head with the parent inverse.
def local_of(obj):
    matrix = obj.matrix_parent_inverse @ obj.matrix_basis
    if obj.parent is not None and obj.parent_type == "BONE" and obj.parent_bone in obj.parent.data.bones:
        matrix = Matrix.Translation((0.0, obj.parent.data.bones[obj.parent_bone].length, 0.0)) @ matrix
    return matrix


def game_matrix(matrix):
    return swap_coord_space(matrix, SPACE_MATRIX_INV, SPACE_MATRIX)


def uniform_scale(matrix):
    axes = matrix.to_scale()
    mean = sum(axes) / 3
    return mean, max(axes) - min(axes) <= max(abs(mean), 1.0) * 1e-3


def locator_values(matrix, previous_rotation=None):
    game = game_matrix(matrix)
    scale, _ = uniform_scale(game)
    rotation = game.to_3x3().normalized()
    # Blender Euler orders list the axes in the order they are applied: roll, yaw, pitch.
    euler_order = "ZYX"
    compat = None
    if previous_rotation is not None:
        yaw, pitch, roll = (math.radians(a) for a in previous_rotation)
        compat = Euler((pitch, yaw, roll), euler_order)
    euler = rotation.to_euler(euler_order, compat) if compat is not None else rotation.to_euler(euler_order)
    angles = (math.degrees(euler.y), math.degrees(euler.x), math.degrees(euler.z))
    return tuple(game.translation), angles, scale


def rotation_matrix(rotation):
    return rotation_from_angles(rotation, 3)


def differences(matrix, locator):
    position, rotation, scale = locator_values(matrix, locator.rotation)
    moved = max(abs(a - b) for a, b in zip(position, locator.position)) > POSITION_TOLERANCE
    old = rotation_matrix(locator.rotation)
    new = rotation_matrix(rotation)
    turned = max(abs(old[i][j] - new[i][j]) for i in range(3) for j in range(3)) > ROTATION_TOLERANCE
    scaled = abs(scale - locator.scale) > SCALE_TOLERANCE
    return moved, turned, scaled, (position, rotation, scale)


def entity_scale(index, name):
    entity = index.entity(name) if index is not None else None
    return entity.scale if entity is not None else 1.0


def offset_of(obj, index, shift=None):
    placed = local_of(obj) if shift is None else local_of(obj) @ shift
    return placed @ Matrix.Scale(1.0 / (entity_scale(index, obj.get("pdx_entity")) or 1.0), 4)


def is_identity(matrix):
    position, rotation, scale = locator_values(matrix)
    return (
        max(abs(a) for a in position) <= POSITION_TOLERANCE
        and max(abs(rotation_matrix(rotation)[i][j] - (i == j)) for i in range(3) for j in range(3)) <= ROTATION_TOLERANCE
        and abs(scale - 1.0) <= SCALE_TOLERANCE
    )


def locator_matrix(locator, index):
    children = [c for c in locator.children if c.get("pdx_entity")]
    base = local_of(locator)
    if not children:
        return base, None
    candidates = [base @ offset_of(child, index) for child in children]
    first = candidates[0]
    for other in candidates[1:]:
        if not is_identity(first.inverted_safe() @ other):
            return base, f"the parts on '{locator.name}' are not in the same place, its own position is used"
    return first, None


def sanitize(name):
    return NAME_CHARACTERS.sub("_", name).strip("_") or "entity"


def locator_name(obj):
    return obj.get("pdx_locator") or sanitize(obj.name)


def outermost(obj):
    found = None
    while obj is not None:
        if obj.get("pdx_entity"):
            found = obj
        obj = obj.parent
    return found


def tops_of(objects):
    tops = []
    for obj in objects:
        found = outermost(obj)
        candidates = [found] if found is not None else []
        if found is None and obj.type == "EMPTY":
            candidates = [o for o in obj.children_recursive if o.get("pdx_entity") and outermost(o) is o]
        for top in candidates:
            if top not in tops:
                tops.append(top)
    return tops


def content_objects(inst):
    holders = [c for c in inst.children if c.type == "EMPTY" and not c.get("pdx_entity") and not c.get("pdx_locator") and c.name.split(".")[0].endswith("_scale")]
    found = []
    for parent in [inst] + holders:
        for obj in parent.children:
            if obj.get("pdx_entity"):
                continue
            if obj.type in ("MESH", "ARMATURE"):
                found.append(obj)
                if obj.type == "ARMATURE":
                    found += [c for c in obj.children if c.type == "MESH" and not c.get("pdx_entity")]
    return found


def mesh_facts(index, entity):
    cache = index.__dict__.setdefault("_mesh_facts", {})
    pdxmesh = index.pdxmeshes.get(entity.pdxmesh) if entity.pdxmesh else None
    path = index.resolve_file(pdxmesh.file) if pdxmesh is not None and pdxmesh.file else None
    if path is None:
        return False, set()
    if path not in cache:
        visible, parts = False, set()
        try:
            root = read_meshfile(str(path))
            for shape in root.find("object"):
                skeleton = shape.find("skeleton")
                if skeleton is not None:
                    parts |= {clean_imported_name(bone.tag) for bone in skeleton}
                for mesh in shape.findall("mesh"):
                    if "p" in mesh.attrib and "tri" in mesh.attrib:
                        material = getattr(PDXData(mesh), "material", None)
                        visible = visible or (material.shader[0] if material is not None else "").lower() != "collision"
            locators = root.find("locator")
            if locators is not None:
                parts |= {clean_imported_name(locator.tag) for locator in locators}
        except Exception:
            pass
        cache[path] = (visible, parts)
    return cache[path]


def shows_model(inst):
    return any(obj.type == "MESH" and not obj.get("pdx_collision") for obj in content_objects(inst))


def rest_world(obj):
    if obj.parent is None:
        return local_of(obj)
    frame = rest_world(obj.parent)
    if obj.parent_type == "BONE" and obj.parent_bone in obj.parent.data.bones:
        frame = frame @ obj.parent.data.bones[obj.parent_bone].matrix_local
    return frame @ local_of(obj)


def set_local(obj, matrix):
    if obj.parent is not None and obj.parent_type == "BONE" and obj.parent_bone in obj.parent.data.bones:
        matrix = Matrix.Translation((0.0, -obj.parent.data.bones[obj.parent_bone].length, 0.0)) @ matrix
    obj.matrix_basis = obj.matrix_parent_inverse.inverted_safe() @ matrix


# Meshes and rigs sit at the identity under their entity as imported, so a transform on them means the model was
# moved on its own (clicking a part in the viewport selects the mesh, not the entity's empty).
def content_shift(inst):
    holders = [c for c in inst.children if c.type == "EMPTY" and not c.get("pdx_entity") and not c.get("pdx_locator") and c.name.split(".")[0].endswith("_scale")]
    meshes, rigs = [], []
    for parent in [inst] + holders:
        frame = Matrix() if parent is inst else local_of(parent)
        inverse = frame.inverted_safe()
        for obj in parent.children:
            if obj.get("pdx_entity"):
                continue
            if obj.type == "MESH":
                meshes.append(frame @ local_of(obj) @ inverse)
            elif obj.type == "ARMATURE":
                rigs.append(frame @ local_of(obj) @ inverse)
                meshes += [frame @ local_of(obj) @ local_of(skinned) @ inverse for skinned in obj.children if skinned.type == "MESH" and not skinned.get("pdx_entity")]
    moved = [shift for shift in meshes + rigs if not is_identity(shift)]
    if not moved:
        return Matrix(), True
    candidates = meshes or rigs
    first = candidates[0]
    if not is_identity(first) and all(is_identity(first.inverted_safe() @ shift) for shift in candidates[1:]):
        if all(is_identity(shift) or is_identity(first.inverted_safe() @ shift) for shift in rigs):
            return first, True
    return moved[0], False


def owner_of(obj):
    while obj is not None:
        if obj.get("pdx_entity"):
            return obj
        obj = obj.parent
    return None


class Assembly:
    def __init__(self, name, scale):
        self.name = name
        self.scale = scale
        self.locators = []
        self.attach = []
        self.problems = []
        self.entities = []

    def lines(self):
        return [f"entity '{self.name}': {len(self.locators)} locators, {len(self.attach)} parts"] + self.problems


def unused_name(name, taken):
    candidate, number = name, 2
    while candidate in taken:
        candidate, number = f"{name}_{number}", number + 1
    return candidate


def read_assembly(root, index=None, name=None):
    if root.type != "EMPTY":
        raise ValueError(f"'{root.name}' is not an empty, select the empty that holds the parts")
    if root.get("pdx_entity"):
        raise ValueError(f"'{root.name}' is an imported entity, use Update Asset to change it")
    scale, uniform = uniform_scale(local_of(root))
    assembly = Assembly(sanitize(name or root.name), scale)
    if not uniform:
        assembly.problems.append(f"'{root.name}' is not scaled evenly, the average is used")
    taken, parts, others = set(), {}, []
    for child in sorted(root.children, key=lambda o: o.name):
        if child.get("pdx_entity"):
            parts.setdefault(child["pdx_entity"], []).append(child)
        elif child.type == "EMPTY":
            name_here = unused_name(locator_name(child), taken)
            taken.add(name_here)
            matrix, problem = locator_matrix(child, index)
            if problem:
                assembly.problems.append(problem)
            position, rotation, loc_scale = locator_values(matrix)
            assembly.locators.append((name_here, position, rotation, loc_scale))
            for part in sorted(child.children, key=lambda o: o.name):
                if part.get("pdx_entity"):
                    assembly.attach.append((name_here, part["pdx_entity"]))
                    assembly.entities.append(part["pdx_entity"])
        else:
            others.append(child.name)
    for entity_name, objects in sorted(parts.items()):
        base = entity_name[: -len("_entity")] if entity_name.endswith("_entity") and len(entity_name) > 7 else entity_name
        for obj in objects:
            number = 1
            while f"{sanitize(base)}_{number}" in taken:
                number += 1
            name_here = f"{sanitize(base)}_{number}"
            taken.add(name_here)
            matrix = offset_of(obj, index)
            position, rotation, loc_scale = locator_values(matrix)
            assembly.locators.append((name_here, position, rotation, loc_scale))
            assembly.attach.append((name_here, entity_name))
            assembly.entities.append(entity_name)
    if others:
        shown = ", ".join(sorted(others)[:3]) + (" ..." if len(others) > 3 else "")
        assembly.problems.append(
            f"{len(others)} objects under '{root.name}' are not entities and are not saved ({shown}). Models made in "
            "Blender need exporting as meshes first (Save Mesh), and an entity and pdxmesh in a .gfx file that uses them"
        )
    return assembly


def reachable(index, names):
    seen, stack = set(), list(names)
    while stack:
        name = stack.pop()
        if name not in seen:
            seen.add(name)
            stack.extend(index.children(name))
    return sorted(seen)


def check_references(assembly, index):
    problems = []
    for name in reachable(index, set(assembly.entities)):
        entity = index.entity(name)
        if entity is None:
            problems.append(f"entity '{name}' is not defined in the mod or game folders")
        elif entity.pdxmesh:
            pdxmesh = index.pdxmeshes.get(entity.pdxmesh)
            if pdxmesh is None:
                problems.append(f"'{name}' uses the pdxmesh '{entity.pdxmesh}', which is not defined in any .gfx file")
            elif not pdxmesh.file or index.resolve_file(pdxmesh.file) is None:
                problems.append(
                    f"'{name}' needs the mesh file '{pdxmesh.file}', which does not exist yet. Export it with Save Mesh"
                )
    return problems


def write_assembly(assembly, filepath, index=None, cull_radius=0.0):
    path = Path(filepath)
    problems = list(assembly.problems)
    if index is not None:
        problems += check_references(assembly, index)
        if assembly.name in index.entity_blocks and Path(index.entity_blocks[assembly.name][1]).resolve() != path.resolve():
            other = index.entity_blocks[assembly.name][1]
            problems.append(f"an entity called '{assembly.name}' is already defined in {Path(other).name}")
    if find_mod_root(path.parent) is None:
        problems.append(f"'{path.name}' is not inside a folder with a gfx folder, so the game will not load it")
    text, bom = read_text(path) if path.is_file() else ("", False)
    doc = AssetText(text)
    if doc.warnings:
        raise ValueError(f"'{path.name}' has unbalanced braces ({doc.warnings[0]}), fix it before saving into it")
    replaced = doc.entity(assembly.name) is not None
    doc.put_entity(
        assembly.name,
        format_entity(assembly.name, assembly.locators, assembly.attach, assembly.scale, cull_radius),
    )
    result = doc.result()
    if len(parse(result).root.blocks("entity")) < len(parse(text).root.blocks("entity")):
        raise ValueError("the edit would have removed an entity, nothing was written")
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(path, result, bom)
    return replaced, problems


SUFFIX = re.compile(r"\.\d+$")


def base_name(name):
    return SUFFIX.sub("", name)


class Change:
    def __init__(self, kind, owner, path, locator=None, values=None, parts=(), parent_joint=None, ordinal=None, target=None, block=None):
        self.kind, self.owner, self.path, self.locator = kind, owner, Path(path), locator
        self.values, self.parts, self.parent_joint, self.ordinal = values, parts, parent_joint, ordinal
        self.target, self.block = target, block


class UpdatePlan:
    def __init__(self):
        self.changes = []
        self.problems = []
        self.checked = 0
        self.tops = []
        self.nodes = []
        self.adopted = 0

    def files(self):
        return sorted({change.path for change in self.changes})

    def count(self, kind):
        return sum(1 for change in self.changes if change.kind == kind)

    def lines(self):
        labels = (
            ("variant", "entity copies written"),
            ("locator", "locators changed"),
            ("new_locator", "locators added"),
            ("attach", "parts moved"),
            ("repoint", "attach lines pointed at copies"),
            ("remove", "attach lines removed"),
            ("drop_model", "models removed"),
        )
        parts = [f"{self.count(kind)} {label}" for kind, label in labels if self.count(kind)]
        shown = ", ".join(self.tops[:3]) + (" ..." if len(self.tops) > 3 else "")
        head = ", ".join(parts) + f" in {len(self.files())} files" if parts else f"nothing to change in {shown} ({self.checked} locators and parts checked)"
        copies = [f"{c.owner} is a copy of {c.locator}" for c in self.changes if c.kind == "variant"][:5]
        loose = [
            f"{self.adopted} parts came loose when the locator or part they hung from was deleted, they are saved where they "
            "are now (Blender moves them when that happens, undo and delete them too to remove them)"
        ] if self.adopted else []
        return [head] + loose + copies + self.problems


def same_values(a, b):
    return (
        max(abs(x - y) for x, y in zip(a[0], b[0])) <= POSITION_TOLERANCE
        and max(abs(rotation_matrix(a[1])[i][j] - rotation_matrix(b[1])[i][j]) for i in range(3) for j in range(3)) <= ROTATION_TOLERANCE
        and abs(a[2] - b[2]) <= SCALE_TOLERANCE
    )


def same_matrix(a, b):
    return same_values(locator_values(a), locator_values(b))


def file_matrix(locator):
    return local_matrix(locator.position, locator.rotation, locator.scale)


class Placed:
    def __init__(self, key, kind, matrix, child=None, scale=1.0, together=True):
        self.key, self.kind, self.matrix, self.child = key, kind, matrix, child
        self.scale, self.together = scale, together


class Layout:
    def __init__(self):
        self.locators = {}
        self.pairs = []
        self.removed = []
        self.objects = {}
        self.checked = 0


def scan_instances(top):
    instances = [top] + [o for o in top.children_recursive if o.get("pdx_entity")]
    info = {inst: ([], []) for inst in instances}
    for obj in top.children_recursive:
        if obj.get("pdx_entity"):
            owner = owner_of(obj.parent)
            if owner in info:
                info[owner][1].append(obj)
        elif obj.type == "EMPTY":
            owner = owner_of(obj.parent)
            if owner in info:
                info[owner][0].append(obj)
    return instances, info


def read_layout(inst, entity, index, empties, children, problems):
    layout = Layout()
    found = {}
    tagged = [o for o in empties if o.get("pdx_locator")]
    for obj in tagged + [o for o in empties if not o.get("pdx_locator")]:
        name = obj.get("pdx_locator") or base_name(obj.name)
        if name in entity.locators and name not in found:
            found[name] = obj
            layout.locators[name] = local_of(obj)
    layout.objects = found
    names = {obj: name for name, obj in found.items()}
    grouped = {}
    for child in sorted(children, key=lambda o: o.name):
        parent = child.parent
        if parent is inst:
            key, kind = "root", "root"
        elif parent in names:
            key, kind = names[parent], "locator"
        elif parent.type == "ARMATURE" and child.parent_type == "BONE":
            key, kind = child.parent_bone, "bone"
        else:
            key, kind = base_name(parent.name), "mesh"
        grouped.setdefault((key, child["pdx_entity"]), []).append((child, kind))
    layout.pairs = [None] * len(entity.attach)
    used = {}
    for position, pair in enumerate(entity.attach):
        queue, taken = grouped.get(pair, []), used.get(pair, 0)
        used[pair] = taken + 1
        if taken < len(queue):
            child, kind = queue[taken]
            shift, together = content_shift(child)
            offset = offset_of(child, index, shift if together else None)
            if not together:
                problems.append(f"the model of '{child['pdx_entity']}' was moved in pieces, move its empty ('{child.name}') instead")
            elif not is_identity(shift) and index.entity(child["pdx_entity"]) is not None and index.entity(child["pdx_entity"]).attach:
                problems.append(f"'{child['pdx_entity']}' was moved by its model, the parts attached to it move with it when saved")
            matrix = local_of(found[pair[0]]) @ offset if kind == "locator" else offset
            layout.pairs[position] = Placed(pair[0], kind, matrix, child, entity_scale(index, child["pdx_entity"]) or 1.0, together)
    lineage, above = set(), inst
    while above is not None:
        if above.get("pdx_entity"):
            lineage.add(above["pdx_entity"])
        above = above.parent
    _, parts = mesh_facts(index, entity)
    for position, (key, child_name) in enumerate(entity.attach):
        if layout.pairs[position] is None and child_name not in lineage and index.entity(child_name) is not None and (key in entity.locators or key == "root" or key in parts):
            layout.removed.append(position)
    layout.checked = len(layout.locators) + sum(1 for placed in layout.pairs if placed)
    return layout


def clusters_of(members):
    clusters = []
    for position, placed in members:
        for cluster in clusters:
            if same_matrix(cluster[0], placed.matrix):
                cluster[1].append(position)
                break
        else:
            clusters.append([placed.matrix, [position]])
    return clusters


def free_name(key, taken):
    number = 2
    while f"{key}_{number}" in taken:
        number += 1
    taken.add(f"{key}_{number}")
    return f"{key}_{number}"


class Derived:
    def __init__(self):
        self.want = {}
        self.new = []
        self.removed = []
        self.drop_model = False
        self.problems = []

    def signature(self):
        def rounded(matrix):
            position, rotation, scale = locator_values(matrix)
            return tuple(round(v, 2) for v in position + rotation + (scale,))

        return (
            tuple(sorted((name, rounded(matrix)) for name, matrix in self.want.items())),
            tuple(sorted((rounded(matrix), joint or "", tuple(positions)) for _, matrix, joint, positions in self.new)),
            tuple(self.removed),
            self.drop_model,
        )


def derive(entity, layout):
    derived = Derived()
    derived.want = dict(layout.locators)
    derived.removed = list(layout.removed)
    taken = set(entity.locators)
    groups = {}
    for position, placed in enumerate(layout.pairs):
        if placed is not None:
            groups.setdefault(placed.key, []).append((position, placed))
    for key, members in groups.items():
        kind = members[0][1].kind
        clusters = clusters_of(members)
        if kind == "locator":
            keep = next((c for c in clusters if same_matrix(c[0], file_matrix(entity.locators[key]))), None)
            keep = keep or max(clusters, key=lambda c: len(c[1]))
            derived.want[key] = keep[0]
            for cluster in clusters:
                if cluster is not keep:
                    derived.new.append((free_name(key, taken), cluster[0], None, cluster[1]))
        elif kind in ("root", "bone"):
            for matrix, positions in clusters:
                if not is_identity(matrix):
                    derived.new.append((free_name(key, taken), matrix, key if kind == "bone" else None, positions))
        elif any(not is_identity(placed.matrix) for _, placed in members):
            derived.problems.append(f"parts attached to '{key}', a locator of the mesh, were moved, that cannot be saved")
    return derived


def changed_locators(entity, derived):
    for name, matrix in derived.want.items():
        moved, turned, scaled, values = differences(matrix, entity.locators[name])
        if moved or turned or scaled:
            yield name, values, (moved, turned, scaled)


class Node:
    def __init__(self, inst, entity, layout, derived):
        self.inst, self.entity, self.layout, self.derived = inst, entity, layout, derived
        self.children = {}
        self.name = entity.name
        self.repoint = {}
        self.sig = None
        self.changed = False
        self.gone = None
        self.model_gone = False


def referrers(index):
    found = getattr(index, "_referrers", None)
    if found is None:
        found = {}
        for name, (block, _) in index.entity_blocks.items():
            targets = [block.get("clone")] + [v for attach in block.blocks("attach") for _, v in attach if isinstance(v, str)]
            for target in targets:
                if target:
                    found.setdefault(target, set()).add(name)
        index._referrers = found
    return found


def in_place_changes(name, entity, node, index, is_protected):
    changes = []
    for loc_name, values, parts in changed_locators(entity, node.derived):
        source = index.locator_source(name, loc_name)
        if source is None:
            continue
        owner, path = source
        if is_protected(path):
            return None, f"'{loc_name}' of '{name}' is defined in a game file ({Path(path).name})"
        changes.append(Change("locator", owner, path, loc_name, values, parts))
    for new_name, matrix, joint, positions in node.derived.new:
        sources = [entity.attach_sources[position] for position in positions]
        paths = {block: Path(index.entity_blocks[block][1]) for block, _ in sources}
        if any(is_protected(path) for path in paths.values()):
            return None, f"parts of '{name}' are attached in a game file"
        values = locator_values(matrix)
        for block, path in paths.items():
            changes.append(Change("new_locator", block, path, new_name, values, parent_joint=joint))
        for block, ordinal in sources:
            changes.append(Change("attach", block, paths[block], new_name, ordinal=ordinal))
    for position, target in node.repoint.items():
        block, ordinal = entity.attach_sources[position]
        path = Path(index.entity_blocks[block][1])
        if is_protected(path):
            return None, f"parts of '{name}' are attached in a game file"
        changes.append(Change("repoint", block, path, ordinal=ordinal, target=target))
    for position in node.derived.removed:
        block, ordinal = entity.attach_sources[position]
        path = Path(index.entity_blocks[block][1])
        if is_protected(path):
            return None, f"parts of '{name}' are attached in a game file"
        changes.append(Change("remove", block, path, ordinal=ordinal))
    if node.derived.drop_model:
        source = index.setting_source(name, "pdxmesh")
        if source is not None:
            if is_protected(source[1]):
                return None, f"the model of '{name}' is set in a game file"
            changes.append(Change("drop_model", source[0], source[1]))
    return changes, None


VARIABLE = re.compile(r"@[A-Za-z_][A-Za-z0-9_]*")


def variant_text(index, name, variant_name, entity, node):
    block, source = index.entity_blocks[name]
    text, _ = read_text(Path(source))
    entry = AssetText(text).entity(name)
    if entry is None:
        return None, f"could not find '{name}' in {Path(source).name}"
    variables = parse(text).variables
    sub = AssetText(text[entry.start : entry.end].replace("\r\n", "\n"))
    inner = sub.entity(name)
    pairs = sub.pairs(inner)
    sub.set_entity_name(inner, variant_name)
    added = []

    def own_pair(position):
        origin, ordinal = entity.attach_sources[position]
        return pairs[ordinal] if origin == name and ordinal < len(pairs) else None

    for loc_name, values, parts in changed_locators(entity, node.derived):
        locator = sub.locator(inner, loc_name)
        if locator is None:
            added.append(format_locator(loc_name, *values, entity.locators[loc_name].parent_joint))
        else:
            position, rotation, scale = values
            moved, turned, scaled = parts
            sub.set_locator(locator, position=position if moved else None, rotation=rotation if turned else None, scale=scale if scaled else None)
    for new_name, matrix, joint, positions in node.derived.new:
        for position in positions:
            pair = own_pair(position)
            if pair is None:
                return None, f"'{name}' gets its parts from an entity it clones, they cannot be moved in a copy"
            sub.rename_pair(pair, new_name)
        added.append(format_locator(new_name, *locator_values(matrix), joint))
    for position, target in node.repoint.items():
        pair = own_pair(position)
        if pair is None:
            return None, f"'{name}' gets its parts from an entity it clones, they cannot be moved in a copy"
        sub.set_pair_value(pair, target)
    if node.derived.drop_model and not sub.remove_child(inner, "pdxmesh"):
        return None, f"'{name}' gets its model from an entity it clones, it cannot be removed in a copy"
    gone = []
    for position in node.derived.removed:
        pair = own_pair(position)
        if pair is None:
            return None, f"'{name}' gets its parts from an entity it clones, they cannot be removed in a copy"
        gone.append(pair)
    if gone:
        sub.remove_pairs(inner, gone)
    if added:
        sub.add_locators(inner, added)

    def value_of(match):
        value = variables.get(match.group(0))
        return match.group(0) if value is None else (f'"{value}"' if re.search(r"\s", value) else value)

    body = VARIABLE.sub(value_of, sub.result())
    return f"# {variant_name} is a copy of {name} with changes made in Blender\n{body}", None


# The top entity is edited in place. Shared definitions below it are never edited (the same chunk can be used 60
# times): a changed instance gets a copy, <name>_v2, and its parent is pointed at the copy, all the way up. An
# entity only this scene uses, changed the same way everywhere, is edited in place instead.
def plan_update(root, index, protected=()):
    plan = UpdatePlan()
    tops = tops_of(root if isinstance(root, (list, tuple, set)) else [root])
    if not tops:
        raise ValueError("Select an imported entity, or something that belongs to one")
    plan.tops = [top["pdx_entity"] for top in tops]
    protected_dirs = [Path(p).resolve() for p in protected if p]

    def is_protected(path):
        return any(folder in Path(path).resolve().parents for folder in protected_dirs)

    instances, info, top_of = [], {}, {}
    for top in tops:
        found, found_info = scan_instances(top)
        instances += found
        info.update(found_info)
        top_of.update({inst: top for inst in found})
        if not is_identity(content_shift(top)[0]):
            plan.problems.append(f"the model of '{top['pdx_entity']}' was moved on its own, it is the top of what is saved so that is not saved")

    nodes = {}
    for inst in instances:
        entity = index.entity(inst["pdx_entity"])
        if entity is None:
            continue
        layout = read_layout(inst, entity, index, *info[inst], plan.problems)
        plan.checked += layout.checked
        node = Node(inst, entity, layout, None)
        node.children = {position: placed.child for position, placed in enumerate(layout.pairs) if placed is not None and placed.child in info}
        nodes[inst] = node

    loose = {}
    for obj in bpy.context.scene.objects:
        if obj.get("pdx_entity") and obj.parent is None and obj not in info:
            loose.setdefault(obj["pdx_entity"], []).append(obj)
    queue = list(nodes.values())
    while queue and loose:
        node = queue.pop()
        base = None
        for position in list(node.layout.removed):
            key, child_name = node.entity.attach[position]
            if not loose.get(child_name):
                continue
            base = base if base is not None else rest_world(node.inst)
            scale = entity_scale(index, child_name) or 1.0
            locator = node.entity.locators.get(key)
            expected = base @ (file_matrix(locator) if locator is not None else Matrix()) @ Matrix.Scale(scale, 4)
            orphan = min(loose[child_name], key=lambda o: (local_of(o).translation - expected.translation).length)
            loose[child_name].remove(orphan)
            shift, together = content_shift(orphan)
            matrix = base.inverted_safe() @ local_of(orphan) @ (shift if together else Matrix()) @ Matrix.Scale(1.0 / scale, 4)
            node.layout.removed.remove(position)
            node.layout.pairs[position] = Placed(key, "locator" if locator is not None else "root", matrix, orphan, scale, together)
            node.children[position] = orphan
            plan.adopted += 1
            found, found_info = scan_instances(orphan)
            info.update(found_info)
            for inst in found:
                entity = index.entity(inst["pdx_entity"])
                if entity is None:
                    continue
                layout = read_layout(inst, entity, index, *info[inst], plan.problems)
                plan.checked += layout.checked
                child = Node(inst, entity, layout, None)
                child.children = {p: placed.child for p, placed in enumerate(layout.pairs) if placed is not None and placed.child in info}
                nodes[inst] = child
                queue.append(child)

    part_names = {child for node in nodes.values() for _, child in node.entity.attach}
    stranded = sum(len(objs) for name, objs in loose.items() if name in part_names)
    if stranded:
        plan.problems.append(
            f"{stranded} parts came loose when what they hung from was deleted, and there is nothing left to hang them on, "
            "so they are not saved"
        )

    def settle(node):
        if node.gone is None:
            for child in node.children.values():
                if child in nodes:
                    settle(nodes[child])
            visible, _ = mesh_facts(index, node.entity)
            shown = visible and shows_model(node.inst)
            node.model_gone = visible and not shown
            alive = [c for c in node.children.values() if c not in nodes or not nodes[c].gone]
            lost = node.model_gone or node.layout.removed or len(alive) < len(node.children)
            node.gone = not shown and not alive and bool(lost)
        return node.gone

    for node in nodes.values():
        settle(node)
    for top in tops:
        if top in nodes:
            nodes[top].gone = False
    for node in nodes.values():
        for position, child in list(node.children.items()):
            if child in nodes and nodes[child].gone:
                node.layout.pairs[position] = None
                node.layout.removed.append(position)
                del node.children[position]
        node.derived = derive(node.entity, node.layout)
        node.derived.drop_model = node.model_gone and not node.gone
        if node.derived.drop_model and any(placed is not None and placed.kind in ("bone", "mesh") for placed in node.layout.pairs):
            node.derived.drop_model = False
            plan.problems.append(f"the model of '{node.entity.name}' was deleted but parts still hang from its bones or locators, so it is kept")
        if not node.gone:
            plan.problems += node.derived.problems
    plan.nodes = list(nodes.values())
    nodes = {inst: node for inst, node in nodes.items() if not node.gone}
    top_entities = {nodes[top].entity.name for top in tops if top in nodes}
    by_entity = {}
    for node in nodes.values():
        by_entity.setdefault(node.entity.name, []).append(node)

    order, seen = [], set()

    def visit(name):
        if name in seen:
            return
        seen.add(name)
        for node in by_entity[name]:
            for child in node.children.values():
                if child in nodes:
                    visit(nodes[child].entity.name)
        order.append(name)

    for name in sorted(by_entity):
        visit(name)

    scene_names = set(by_entity)
    taken = set(index.entity_blocks)
    made = {}
    blocked = set()

    for name in order:
        group = by_entity[name]
        entity = group[0].entity
        for node in group:
            node.repoint = {
                position: nodes[child].name
                for position, child in node.children.items()
                if child in nodes and nodes[child].name != entity.attach[position][1]
            }
            node.changed = bool(list(changed_locators(entity, node.derived)) or node.derived.new or node.derived.removed or node.derived.drop_model or node.repoint)
            node.sig = (node.derived.signature(), tuple(sorted(node.repoint.items())))
        changed = [node for node in group if node.changed]
        if not changed:
            continue
        signatures = {node.sig for node in changed}
        is_top = name in top_entities
        outside = referrers(index).get(name, set()) - scene_names
        if len(signatures) == 1 and (is_top or (len(changed) == len(group) and not outside)):
            changes, why = in_place_changes(name, entity, changed[0], index, is_protected)
            if changes is not None:
                plan.changes += changes
                continue
            if is_top:
                plan.problems.append(f"{why}, it was not saved")
                continue
        elif is_top:
            plan.problems.append(f"copies of '{name}', which is at the top, were changed differently, it was not saved")
            continue
        for node in changed:
            if node.inst in top_of and top_of[node.inst]["pdx_entity"] in blocked:
                continue
            top_entity = top_of[node.inst]["pdx_entity"]
            target = Path(index.entity_blocks[top_entity][1])
            if is_protected(target):
                blocked.add(top_entity)
                plan.problems.append(f"'{top_entity}' is defined in a game file ({target.name}), nothing below it was saved")
                continue
            key = (name, node.sig, str(target))
            if key not in made:
                base = re.sub(r"_v\d+$", "", name)
                number = 2
                while f"{base}_v{number}" in taken:
                    number += 1
                variant = f"{base}_v{number}"
                block, why = variant_text(index, name, variant, entity, node)
                if block is None:
                    plan.problems.append(f"{why}, a copy of '{name}' was not made")
                    made[key] = None
                else:
                    taken.add(variant)
                    made[key] = (variant, Change("variant", variant, target, name, block=block))
                    plan.changes.append(made[key][1])
            if made[key] is not None:
                node.name = made[key][0]
    return plan


def apply_update(plan, backup=True):
    problems = []
    for path in plan.files():
        text, bom = read_text(path)
        doc = AssetText(text)
        if doc.warnings:
            problems.append(f"{path.name} has unbalanced braces ({doc.warnings[0]}), it was not changed")
            continue
        owners, variants = {}, []
        for change in plan.changes:
            if change.path != path:
                continue
            if change.kind == "variant":
                variants.append(change)
            else:
                owners.setdefault(change.owner, []).append(change)
        edited = 0
        for owner, changes in owners.items():
            entity = doc.entity(owner)
            if entity is None:
                problems.append(f"could not find the entity '{owner}' in {path.name}")
                continue
            pairs, added, gone = doc.pairs(entity), [], []
            for change in changes:
                try:
                    if change.kind == "locator":
                        locator = doc.locator(entity, change.locator)
                        if locator is None:
                            problems.append(f"could not find locator '{change.locator}' of '{owner}' in {path.name}")
                            continue
                        position, rotation, scale = change.values
                        moved, turned, scaled = change.parts
                        doc.set_locator(
                            locator,
                            position=position if moved else None,
                            rotation=rotation if turned else None,
                            scale=scale if scaled else None,
                        )
                    elif change.kind == "new_locator":
                        added.append(format_locator(change.locator, *change.values, change.parent_joint))
                    elif change.kind == "drop_model":
                        continue
                    elif change.ordinal >= len(pairs):
                        problems.append(f"could not find attach pair {change.ordinal} of '{owner}' in {path.name}")
                        continue
                    elif change.kind == "attach":
                        doc.rename_pair(pairs[change.ordinal], change.locator)
                    elif change.kind == "remove":
                        gone.append(pairs[change.ordinal])
                    else:
                        doc.set_pair_value(pairs[change.ordinal], change.target)
                    edited += 1
                except ValueError as err:
                    problems.append(str(err))
            if any(change.kind == "drop_model" for change in changes):
                doc.remove_child(entity, "pdxmesh")
                edited += 1
            if gone:
                doc.remove_pairs(entity, gone)
            if added:
                doc.add_locators(entity, added)
        for change in variants:
            doc.put_entity(change.owner, change.block)
            edited += 1
        if not edited:
            continue
        result = doc.result()
        if len(scan(result)[1]) > len(doc.warnings):
            problems.append(f"{path.name} would have ended up with unbalanced braces, it was not changed")
            continue
        if backup and not path.with_name(path.name + ".bak").exists():
            write_text(path.with_name(path.name + ".bak"), text, bom)
        write_text(path, result, bom)
    return problems


# After saving, make the scene match the files so a second update starts from what was saved.
def commit_scene(plan):
    for node in plan.nodes:
        if node.gone:
            try:
                doomed = [node.inst] + list(node.inst.children_recursive)
            except ReferenceError:
                continue
            for obj in doomed:
                try:
                    bpy.data.objects.remove(obj, do_unlink=True)
                except ReferenceError:
                    pass
    for node in plan.nodes:
        if node.gone or not (node.changed or node.name != node.entity.name):
            continue
        inst = node.inst
        collection = inst.users_collection[0] if inst.users_collection else bpy.context.scene.collection
        if node.name != node.entity.name:
            inst["pdx_entity"] = node.name
        for name, matrix in node.derived.want.items():
            obj = node.layout.objects.get(name)
            if obj is not None:
                set_local(obj, matrix)
        moved_to = {}
        for new_name, matrix, joint, positions in node.derived.new:
            made = new_empty(new_name, collection)
            made["pdx_locator"] = new_name
            rig = next((o for o in content_objects(inst) if o.type == "ARMATURE"), None)
            if joint and rig is not None and joint in rig.data.bones:
                attach(made, (rig, Matrix(), rig.data.bones[joint]), matrix)
            else:
                made.parent = inst
                made.matrix_basis = matrix
            for position in positions:
                moved_to[position] = made
        for position, placed in enumerate(node.layout.pairs):
            if placed is None or placed.kind == "mesh":
                continue
            child = placed.child
            if position in moved_to:
                child.parent, child.parent_type, child.parent_bone = moved_to[position], "OBJECT", ""
            elif placed.kind != "locator":
                continue
            else:
                holder = node.layout.objects.get(placed.key)
                if holder is None:
                    holder = new_empty(placed.key, collection)
                    holder["pdx_locator"] = placed.key
                    holder.parent = inst
                    holder.matrix_basis = node.derived.want.get(placed.key, placed.matrix)
                    node.layout.objects[placed.key] = holder
                if child.parent is not holder:
                    child.parent, child.parent_type, child.parent_bone = holder, "OBJECT", ""
            child.matrix_parent_inverse = Matrix()
            child.matrix_basis = Matrix.Scale(placed.scale, 4)
            if placed.together:
                for obj in content_objects(child):
                    obj.matrix_parent_inverse = Matrix()
                    obj.matrix_basis = Matrix()
    bpy.context.view_layer.update()
