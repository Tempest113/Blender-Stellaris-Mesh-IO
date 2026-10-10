"""Headless Blender tests, started by tests/run_tests.py."""

import argparse
import importlib
import os
import re
import sys
import traceback
import types
import xml.etree.ElementTree as Xml
from unittest import mock

import bpy
from mathutils import Matrix, Quaternion, Vector

MODULE = "bl_ext.user_default.blender_stellaris_mesh_io"
RESULTS = []


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--mesh", action="append", default=[])
    return parser.parse_args(argv)


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return bool(ok)


class stage:
    def __init__(self, name):
        self.name = name

    def __enter__(self):
        print(f"\n=== {self.name}", flush=True)

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            traceback.print_exception(exc_type, exc, tb)
            check(f"{self.name}: no exception", False, f"{exc_type.__name__}: {exc}")
        return True


def clear_scene():
    if bpy.context.object and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for coll in (bpy.data.meshes, bpy.data.armatures, bpy.data.materials, bpy.data.images, bpy.data.actions):
        for datablock in list(coll):
            coll.remove(datablock)


def action_fcurve_count(action):
    if hasattr(action, "fcurves"):
        return len(action.fcurves)
    return sum(len(cb.fcurves) for layer in action.layers for strip in layer.strips for cb in strip.channelbags)


def corner_normals(mesh):
    if hasattr(mesh, "corner_normals"):
        return [tuple(n.vector) for n in mesh.corner_normals]
    mesh.calc_normals_split()
    return [tuple(loop.normal) for loop in mesh.loops]


def pos_key(vec):
    return tuple(round(c, 4) for c in vec)


def scene_stats():
    stats = dict(meshes=0, verts=0, faces=0, tris=0, uv_layers=0, materials=set(), vgroups=0, armatures=0, bones=0)
    stats["locators"] = 0
    uv_names = set()
    corners, skin = [], []
    for obj in bpy.context.scene.objects:
        if obj.type == "ARMATURE":
            stats["armatures"] += 1
            stats["bones"] += len(obj.data.bones)
        elif obj.type == "EMPTY":
            stats["locators"] += 1
        elif obj.type == "MESH":
            me = obj.data
            me.calc_loop_triangles()
            stats["meshes"] += 1
            stats["verts"] += len(me.vertices)
            stats["faces"] += len(me.polygons)
            stats["tris"] += len(me.loop_triangles)
            uv_names.update(layer.name for layer in me.uv_layers)
            stats["vgroups"] += len(obj.vertex_groups)
            stats["materials"].update(m.name for m in me.materials if m)
            mw = obj.matrix_world
            normals = corner_normals(me)
            uvs = me.uv_layers[0].data if me.uv_layers else None
            for loop in me.loops:
                co = pos_key(mw @ me.vertices[loop.vertex_index].co)
                uv = tuple(uvs[loop.index].uv) if uvs else ()
                corners.append((co, tuple(normals[loop.index]) + uv))
            names = [g.name for g in obj.vertex_groups]
            for loop in me.loops:
                v = me.vertices[loop.vertex_index]
                weights = tuple(sorted((names[g.group], round(g.weight, 3)) for g in v.groups if g.weight > 0))
                skin.append((pos_key(mw @ v.co), weights))
    stats["materials"] = len(stats["materials"])
    stats["uv_layers"] = len(uv_names)
    return stats, corners, skin


def multiset_match(a, b, tol=1e-3):
    if len(a) != len(b):
        return False, f"length {len(a)} != {len(b)}"
    pool = {}
    for key, val in b:
        pool.setdefault(key, []).append(val)
    for key, val in a:
        candidates = pool.get(key, [])
        for i, other in enumerate(candidates):
            if len(other) == len(val) and all(
                (abs(x - y) <= tol) if isinstance(x, float) else x == y for x, y in zip(val, other)
            ):
                del candidates[i]
                break
        else:
            return False, f"no match for {key} {val}"
    return True, ""


def raw_counts(pdx_data, path):
    root = pdx_data.read_meshfile(path)
    c = dict(meshes=0, verts=0, tris=0, materials=0, locators=0, no_normals=0)
    bones, uv_sets, textures = set(), set(), set()
    for node in root.find("object"):
        skeleton = node.find("skeleton")
        if skeleton is not None:
            bones.update(bone.tag for bone in skeleton)
        for mesh in node.findall("mesh"):
            c["meshes"] += 1
            c["verts"] += len(mesh.attrib["p"]) // 3
            c["tris"] += len(mesh.attrib["tri"]) // 3
            c["no_normals"] += 0 if "n" in mesh.attrib else 1
            uv_sets.update(k for k in ("u0", "u1", "u2", "u3") if k in mesh.attrib)
            material = mesh.find("material")
            if material is not None:
                c["materials"] += 1
                textures.update((slot, material.attrib[slot][0]) for slot in ("diff", "spec", "n") if slot in material.attrib)
    locators = root.find("locator")
    c["locators"] = len(locators) if locators is not None else 0
    c.update(bones=len(bones), uv_layers=len(uv_sets), textures=textures)
    return c


class FakeMaterial:
    def __init__(self, shader, **textures):
        self.shader = [shader]
        for slot, name in textures.items():
            setattr(self, slot, [name])


def build_synthetic_scene(bie, out_dir):
    clear_scene()
    scene = bpy.context.scene

    arm = bpy.data.armatures.new("TestRigArm")
    rig = bpy.data.objects.new("TestRig", arm)
    scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    parent = None
    for i, name in enumerate(("root", "mid", "tip")):
        bone = arm.edit_bones.new(name)
        bone.head, bone.tail = (0, 0, i), (0, 0, i + 1)
        if parent:
            bone.parent, bone.use_connect = parent, True
        parent = bone
    bpy.ops.object.mode_set(mode="OBJECT")

    verts = [(x, y, z) for z in (0, 2) for y in (-1, 1) for x in (-1, 1)]
    faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (1, 5, 7, 3), (3, 7, 6, 2), (2, 6, 4, 0)]
    me = bpy.data.meshes.new("TestShape")
    me.from_pydata(verts, [], faces)
    me.update()
    obj = bpy.data.objects.new("TestShape", me)
    scene.collection.objects.link(obj)

    texdir = out_dir
    for name in ("t_diff.png", "t_spec.png", "t_n.png", "t_diff2.png"):
        img = bpy.data.images.new(name, 4, 4)
        img.filepath_raw = os.path.join(texdir, name)
        img.file_format = "PNG"
        img.save()
        bpy.data.images.remove(img)
    mat_a = FakeMaterial("PdxMeshStandard", diff="t_diff.png", spec="t_spec.png", n="t_n.png")
    mat_b = FakeMaterial("PdxMeshAlphaBlend", diff="t_diff2.png")
    me.materials.append(bie.create_shader(mat_a, "TestMatA", texdir))
    me.materials.append(bie.create_shader(mat_b, "TestMatB", texdir))
    for poly in me.polygons:
        poly.material_index = 0 if poly.index < 3 else 1

    uv = me.uv_layers.new(name="map1")
    for poly in me.polygons:
        for li in poly.loop_indices:
            co = me.vertices[me.loops[li].vertex_index].co
            uv.data[li].uv = (co.x * 0.25 + 0.5 + poly.index * 0.01, co.y * 0.25 + 0.5)

    me.normals_split_custom_set_from_vertices([Vector(v).normalized() for v in verts])
    me.polygons.foreach_set("use_smooth", [True] * len(me.polygons))

    for name in ("root", "mid", "tip"):
        obj.vertex_groups.new(name=name)
    for i, v in enumerate(verts):
        if v[2] == 0:
            obj.vertex_groups["root"].add([i], 1.0, "REPLACE")
        else:
            obj.vertex_groups["mid"].add([i], 0.75, "REPLACE")
            obj.vertex_groups["tip"].add([i], 0.25, "REPLACE")
    mod = obj.modifiers.new("TestRig_skin", "ARMATURE")
    mod.object = rig

    loc = bpy.data.objects.new("TestLocator", None)
    loc.location = (1, 2, 3)
    scene.collection.objects.link(loc)
    bpy.context.view_layer.update()
    return obj, rig


def export_mesh(path):
    return bpy.ops.io_pdx_mesh.export_mesh(filepath=path)


def import_mesh(path):
    return bpy.ops.io_pdx_mesh.import_mesh(filepath=path)


def compare_stats(label, a, b, flat_split=False):
    for key in a:
        if key == "no_normals":
            continue
        if key == "verts" and flat_split:
            ok = a[key] <= b[key] <= 3 * a.get("tris", b[key])
            check(f"{label}: {key} (flat shaded elements may split)", ok, f"{a[key]} vs {b[key]}")
        else:
            check(f"{label}: {key}", a[key] == b[key], f"{a[key]} vs {b[key]}")


def mesh_roundtrip(label, pdx_data, first_mesh, out_dir, expect=None, tol=1e-3):
    clear_scene()
    check(f"{label}: import", import_mesh(first_mesh) == {"FINISHED"})
    stats1, corners1, skin1 = scene_stats()
    print(f"{label}: first import {stats1}")
    check(f"{label}: imported something", stats1["meshes"] > 0 and stats1["verts"] > 0)

    raw1 = raw_counts(pdx_data, first_mesh)
    print(f"{label}: raw file {raw1}")
    for key in ("verts", "tris", "materials", "bones", "locators", "uv_layers"):
        check(f"{label}: raw file vs scene {key}", raw1[key] == stats1[key], f"{raw1[key]} vs {stats1[key]}")
    for key, want in (expect or {}).items():
        check(f"{label}: expected {key}", stats1[key] == want, f"{stats1[key]} vs {want}")

    second_mesh = os.path.join(out_dir, f"{label}_reexport.mesh")
    check(f"{label}: re-export", export_mesh(second_mesh) == {"FINISHED"})
    raw2 = raw_counts(pdx_data, second_mesh)
    print(f"{label}: re-exported raw file {raw2}")
    flat = raw1["no_normals"] > 0
    compare_stats(f"{label}: raw first vs re-exported", raw1, raw2, flat_split=flat)

    clear_scene()
    check(f"{label}: re-import", import_mesh(second_mesh) == {"FINISHED"})
    stats2, corners2, skin2 = scene_stats()
    compare_stats(f"{label}: scene first vs re-imported", stats1, stats2, flat_split=flat)
    ok, why = multiset_match(corners1, corners2, tol)
    check(f"{label}: positions / normals / UVs per corner", ok, why)
    ok, why = multiset_match(skin1, skin2)
    check(f"{label}: skin weights per corner", ok, why)


def anim_roundtrip(out_dir, bie):
    scene = bpy.context.scene
    rigs = [o for o in scene.objects if o.type == "ARMATURE"]
    if not check("anim: found imported rig", len(rigs) == 1, f"{len(rigs)} rigs"):
        return
    rig = rigs[0]
    bone_names = [b.name for b in rig.data.bones]
    scene.frame_start, scene.frame_end = 1, 6
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)

    for pb in rig.pose.bones:
        pb.rotation_mode = "QUATERNION"
    for f in range(1, 7):
        for i, pb in enumerate(rig.pose.bones):
            pb.rotation_quaternion = Quaternion((0, 1, 0), 0.1 * f * (i + 1))
            pb.location = (0.02 * f, 0, 0) if i == 1 else (0, 0, 0)
            pb.keyframe_insert("rotation_quaternion", frame=f)
            pb.keyframe_insert("location", frame=f)
    expected = {}
    for f in range(1, 7):
        scene.frame_set(f)
        bpy.context.view_layer.update()
        expected[f] = {n: rig.pose.bones[n].matrix.copy() for n in bone_names}

    action = rig.animation_data.action if rig.animation_data else None
    check("anim: keyframes created an action", action is not None)
    if action:
        check("anim: action has fcurves", action_fcurve_count(action) > 0, f"{action_fcurve_count(action)} fcurves")

    anim_path = os.path.join(out_dir, "synthetic.anim")
    check("anim: export", bpy.ops.io_pdx_mesh.export_anim(filepath=anim_path) == {"FINISHED"})

    rig.animation_data_clear()
    for act in list(bpy.data.actions):
        bpy.data.actions.remove(act)
    for pb in rig.pose.bones:
        pb.location, pb.rotation_quaternion = (0, 0, 0), (1, 0, 0, 0)
    scene.frame_set(1)
    check("anim: import", bpy.ops.io_pdx_mesh.import_anim(filepath=anim_path) == {"FINISHED"})
    action = rig.animation_data.action if rig.animation_data else None
    count = action_fcurve_count(action) if action else 0
    check("anim: import created an action with fcurves", count > 0, f"{count} fcurves")

    worst = 0.0
    for f in range(1, 7):
        scene.frame_set(f)
        bpy.context.view_layer.update()
        for n in bone_names:
            rows = zip(rig.pose.bones[n].matrix, expected[f][n])
            worst = max(worst, max(abs(a - b) for ra, rb in rows for a, b in zip(ra, rb)))
    check("anim: poses match after re-import", worst < 1e-3, f"max matrix delta {worst:.2e} over 6 frames")

    check("anim: import made a named action that stays alive", action is not None and action.name.startswith("synthetic") and action.use_fake_user, str(action and action.name))
    first_action = action
    scene.frame_end = 99
    scene.render.fps = 12
    result = bie.import_animfile(anim_path, rig=rig, action_name="again", set_scene=False)
    check("anim: importing again makes a new action and leaves the first one",
          result["action"].name == "again" and rig.animation_data.action is result["action"] and first_action.name in bpy.data.actions)
    check("anim: set_scene=False leaves the scene range and speed alone", scene.frame_end == 99 and scene.render.fps == 12 and result["frames"] == 6 and result["fps"] == 24,
          f"{scene.frame_end}, {scene.render.fps}, {result['frames']}, {result['fps']}")

    curve = bie.create_fcurve(rig, "root", "scale", 1)
    again = bie.create_fcurve(rig, "root", "scale", 1)
    check("anim: create_fcurve makes and reuses an fcurve",
          curve.data_path == 'pose.bones["root"].scale' and curve.array_index == 1 and again.as_pointer() == curve.as_pointer())
    check("anim: create_fcurve puts the fcurve in the bone group", curve.group is not None and curve.group.name == "root")


def ui_smoke(pdx_module):
    ui = importlib.import_module(MODULE + ".pdx_blender.blender_ui")
    updater = importlib.import_module(MODULE + ".updater")
    github = updater.github
    panels = [c for c in pdx_module.pdx_blender.classes if issubclass(c, bpy.types.Panel)]
    check("panels found", len(panels) > 0, f"{len(panels)} panels")

    states = {
        "default": {},
        "offline, no cached release": dict(AT_LATEST=False, LATEST_VERSION=0.0, LATEST_URL="", LATEST_NOTES=""),
        "update available": dict(AT_LATEST=False, LATEST_VERSION=9.9, LATEST_URL={"blender": "https://example.invalid/x.zip"}),
        "up to date": dict(AT_LATEST=True),
    }
    saved = {k: getattr(github, k) for k in ("AT_LATEST", "LATEST_VERSION", "LATEST_URL", "LATEST_NOTES")}
    try:
        for state, values in states.items():
            for k, v in saved.items():
                setattr(github, k, v)
            for k, v in values.items():
                setattr(github, k, v)
            for panel in panels:
                holder = types.SimpleNamespace(layout=mock.MagicMock())
                try:
                    panel.draw(holder, bpy.context)
                    ok, why = True, ""
                except Exception as err:
                    ok, why = False, f"{type(err).__name__}: {err}"
                check(f"ui draw [{state}]: {panel.__name__}", ok, why)
    finally:
        for k, v in saved.items():
            setattr(github, k, v)
    del ui


def options_and_helpers(pdx_data, bie, synthetic, out_dir):
    clear_scene()
    import_mesh(synthetic)
    scene = bpy.context.scene
    obj = next(o for o in scene.objects if o.type == "MESH")
    rig = next(o for o in scene.objects if o.type == "ARMATURE")

    bie.set_mesh_index(obj.data, 3)
    check("helpers: mesh index set / get", bie.get_mesh_index(obj.data) == 3, str(bie.get_mesh_index(obj.data)))

    check("helpers: show_axis operator", bpy.ops.io_pdx_mesh.show_axis(show=True, data_type="ARMATURE") == {"FINISHED"})
    check("helpers: show_axis changed the armature", rig.data.show_axes and rig.show_axis)

    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode="POSE")
    for pose_bone in rig.pose.bones:
        selected = pose_bone.name == "tip"
        if hasattr(pose_bone, "select"):
            pose_bone.select = selected
        else:
            pose_bone.bone.select = selected
    check("helpers: ignore_bone operator", bpy.ops.io_pdx_mesh.ignore_bone(state=True) == {"FINISHED"})
    names = [b.name for b in bie.get_skeleton_hierarchy(rig)]
    check("helpers: ignored bone leaves the exported hierarchy", names == ["root", "mid"], str(names))
    bpy.ops.io_pdx_mesh.ignore_bone(state=False)
    bpy.ops.object.mode_set(mode="OBJECT")
    check("helpers: unignored bone is back", len(bie.get_skeleton_hierarchy(rig)) == 3)

    path = os.path.join(out_dir, "options_skel.mesh")
    ok = bpy.ops.io_pdx_mesh.export_mesh(filepath=path, chk_mesh=False, chk_skel=True, chk_locs=False) == {"FINISHED"}
    raw = raw_counts(pdx_data, path)
    check("options: skeleton only export", ok and raw["bones"] == 3 and raw["meshes"] == 0, str(raw))

    path = os.path.join(out_dir, "options_split.mesh")
    ok = bpy.ops.io_pdx_mesh.export_mesh(filepath=path, chk_debug=True, chk_split_vtx=True, chk_plain_txt=True) == {"FINISHED"}
    raw = raw_counts(pdx_data, path)
    check("options: split every vertex", ok and raw["verts"] == raw["tris"] * 3, f"{raw['verts']} verts / {raw['tris']} tris")
    check("options: plain text file written", os.path.isfile(os.path.splitext(path)[0] + ".txt"))

    path = os.path.join(out_dir, "options_blendshape.mesh")
    ok = bpy.ops.io_pdx_mesh.export_mesh(filepath=path, chk_mesh_blendshape=True) == {"FINISHED"}
    raw = raw_counts(pdx_data, path)
    check("options: blendshape export", ok and raw["tris"] == 12, str(raw))

    for order in ("+", "~", "-"):
        path = os.path.join(out_dir, f"options_sort_{'p' if order == '+' else 'n' if order == '~' else 'm'}.mesh")
        ok = bpy.ops.io_pdx_mesh.export_mesh(filepath=path, chk_debug=True, ddl_sort_vtx=order) == {"FINISHED"}
        raw = raw_counts(pdx_data, path)
        check(f"options: vertex sort '{order}'", ok and raw["tris"] == 12, str(raw["tris"]))

    path = os.path.join(out_dir, "options_selected.mesh")
    for o in scene.objects:
        o.select_set(o is obj)
    ok = bpy.ops.io_pdx_mesh.export_mesh(filepath=path, chk_selected=True) == {"FINISHED"}
    raw = raw_counts(pdx_data, path)
    check("options: selected only export", ok and raw["meshes"] == 2 and raw["locators"] == 0, str(raw))

    clear_scene()
    bpy.ops.io_pdx_mesh.import_mesh(filepath=synthetic, chk_mesh=True, chk_skel=False, chk_locs=False, chk_joinmats=False)
    stats, _, _ = scene_stats()
    check("options: import mesh only, materials left separate",
          stats["armatures"] == 0 and stats["locators"] == 0 and stats["meshes"] == 2, str(stats))


def updater_checks(pdx_module):
    updater = importlib.import_module(MODULE + ".updater")
    info = pdx_module.IO_PDX_INFO
    current = info["version"]

    owner, repo = info["website"].rstrip("/").split("/")[-2:]
    check("updater: follows the repository in the manifest website",
          (updater.github.owner, updater.github.repo) == (owner, repo), f"{owner}/{repo}")

    def release(tag, assets=("blender_stellaris_mesh_io-9.9.0.zip",)):
        return {"tag_name": tag, "html_url": f"https://example.invalid/releases/{tag}", "published_at": "2026-01-01T00:00:00Z",
                "body": None, "assets": [{"name": n, "browser_download_url": f"https://example.invalid/{n}"} for n in assets]}

    def run(releases):
        fake_settings = types.SimpleNamespace(last_update_check=None)
        with mock.patch.object(updater, "IO_PDX_SETTINGS", fake_settings),                 mock.patch.object(updater.Github_API, "get_data", return_value=releases):
            return updater.Github_API(owner="o", repo="r")

    newer = run([release("99.5")])
    check("updater: newer release is detected", newer.AT_LATEST is False and newer.LATEST_VERSION == "99.5")
    check("updater: update link falls back to the release page", newer.LATEST_URL.get("blender", newer.LATEST_RELEASE) ==
          "https://example.invalid/releases/99.5", str(newer.LATEST_RELEASE))
    check("updater: release without notes gives a string", isinstance(newer.LATEST_NOTES, str))
    check("updater: 'v' prefixed tag", run([release("v99.5")]).LATEST_VERSION == "99.5")
    major, minor, patch = (int(part) for part in (current.split(".") + ["0", "0"])[:3])
    for tag, latest in ((current, True), (current.rsplit(".", 1)[0], True), ("0.0.1", True),(f"{major}.{minor}.{patch + 1}", False), (f"{major}.{minor + 1}", False)):
        check(f"updater: tag {tag} against {current} is {'up to date' if latest else 'an update'}", run([release(tag)]).AT_LATEST is latest)
    odd = run([release("beta-2")])
    check("updater: unsupported tag is ignored, not fatal", odd.LATEST_URL == {} and odd.AT_LATEST is True)
    check("updater: no releases is not fatal", run([]).AT_LATEST is True)


GFX = """
objectTypes = {
    pdxmesh = {
        name = "body_mesh"
        file = "gfx/models/test/body.mesh"
        animation = { id = "idle" type = "body_idle_anim" }
        animation = { id = "work" type = "body_work_anim" }
        animation = { id = "lost" type = "body_lost_anim" }
    }
    pdxmesh = { name = "plain_mesh" file = "gfx/models/test/plain.mesh" scale = 1 }
    pdxmesh = { name = "gone_mesh" file = "gfx/models/test/gone.mesh" }
    pdxmesh = { name = "collider_mesh" file = "gfx/models/test/collider.mesh" }
    pdxmesh = { name = "empty_mesh" file = "gfx/models/test/with_empty.mesh" }
    pdxmesh = { name = "corrupt_mesh" file = "gfx/models/test/corrupt.mesh" }
}
"""

ASSET = """
entity = {
    name = "top_entity"
    scale = 2
    locator = { name = "a" position = { 10 0 0 } rotation = { 0 90 0 } scale = 3 }
    attach = { a = "body_entity" a = "body_entity" a = "leaf_entity" b = "leaf_entity" }
}
animation = { name = "body_idle_anim" file = "body_idle.anim" }
animation = { name = "body_work_anim" file = "body_work.anim" }
entity = {
    name = "body_entity"
    pdxmesh = "body_mesh"
    default_state = "resting"
    state = { name = "resting" animation = "idle" }
    state = { name = "busy" animation = "work" }
    state = { name = "unlucky" animation = "lost" }
    locator = { name = "up" position = { 0 5 0 } }
    attach = { TestLocator = "leaf_entity" mid = "leaf_entity" up = "leaf_entity" }
}
entity = { name = "leaf_entity" pdxmesh = "plain_mesh" }
entity = { name = "root_attach_entity" attach = { root = "leaf_entity" } }
entity = { name = "collider_entity" pdxmesh = "collider_mesh" }
entity = { name = "collider_pair_entity" locator = { name = "l" } attach = { l = "collider_entity" l = "collider_entity" } }
entity = { name = "cycle_entity" locator = { name = "l" } attach = { l = "cycle_entity" } }
entity = { name = "broken_entity" attach = { l = "no_such_entity" } locator = { name = "l" } pdxmesh = "gone_mesh" }
entity = { name = "other_entity" pdxmesh = "no_such_pdxmesh" }
entity = { name = "empty_element_entity" pdxmesh = "empty_mesh" }
entity = { name = "corrupt_entity" pdxmesh = "corrupt_mesh" }
entity = {
    name = "mixed_entity"
    locator = { name = "l" }
    attach = { l = "corrupt_entity" l = "corrupt_entity" l = "leaf_entity" }
}
entity = {
    name = "override_entity"
    pdxmesh = "body_mesh"
    meshsettings = { name = "TestShape" index = 0 texture_diffuse = "t_diff2.png" }
}
entity = { name = "override_all_entity" pdxmesh = "body_mesh" meshsettings = { texture_diffuse = "t_diff2.png" } }
entity = { name = "override_node_entity" pdxmesh = "body_mesh" meshsettings = { name = "TestShape" texture_diffuse = "t_diff2.png" } }
"""


ROOT_ENTITIES = [
    "top_entity", "root_attach_entity", "collider_pair_entity", "broken_entity", "other_entity", "empty_element_entity", "mixed_entity",
    "override_entity", "override_all_entity", "override_node_entity",
]


def build_game_folder(bie, pdx_data, out_dir):
    import shutil

    game = os.path.join(out_dir, "game")
    models = os.path.join(game, "gfx", "models", "test")
    os.makedirs(models, exist_ok=True)

    build_synthetic_scene(bie, out_dir)
    check("asset: export skinned mesh", export_mesh(os.path.join(models, "body.mesh")) == {"FINISHED"})
    for name in ("t_diff.png", "t_spec.png", "t_n.png", "t_diff2.png"):
        shutil.copy(os.path.join(out_dir, name), models)
    for name in ("body_idle.anim", "body_work.anim"):
        shutil.copy(os.path.join(out_dir, "synthetic.anim"), os.path.join(models, name))

    clear_scene()
    me = bpy.data.meshes.new("PlainShape")
    me.from_pydata([(x, y, z) for z in (0, 1) for y in (0, 1) for x in (0, 1)], [], [(0, 1, 3, 2), (4, 6, 7, 5)])
    me.materials.append(bie.create_shader(FakeMaterial("PdxMeshStandard", diff="t_diff.png"), "PlainMat", models))
    me.uv_layers.new(name="map1")
    bpy.context.scene.collection.objects.link(bpy.data.objects.new("PlainShape", me))
    check("asset: export plain mesh", export_mesh(os.path.join(models, "plain.mesh")) == {"FINISHED"})
    clear_scene()
    me = bpy.data.meshes.new("ColliderShape")
    me.from_pydata([(x, y, z) for z in (0, 1) for y in (0, 1) for x in (0, 1)], [], [(0, 1, 3, 2), (4, 6, 7, 5)])
    me.materials.append(bie.create_shader(FakeMaterial("Collision"), "ColliderMat", models))
    me.uv_layers.new(name="map1")
    box = bpy.data.meshes.new("BoxShape")
    box.from_pydata([(x, y, z) for z in (0, 1) for y in (0, 1) for x in (0, 1)], [], [(0, 1, 3, 2), (4, 6, 7, 5)])
    box.materials.append(bie.create_shader(FakeMaterial("PdxMeshStandard", diff="t_diff.png"), "BoxMat", models))
    box.uv_layers.new(name="map1")
    for data in (box, me):
        bpy.data.scenes[0].collection.objects.link(bpy.data.objects.new(data.name, data))
    check("asset: export a mesh with a collision part", export_mesh(os.path.join(models, "collider.mesh")) == {"FINISHED"})
    clear_scene()

    root = pdx_data.read_meshfile(os.path.join(models, "plain.mesh"))
    Xml.SubElement(root.find("object")[0], "mesh")
    pdx_data.write_meshfile(os.path.join(models, "with_empty.mesh"), root)
    with open(os.path.join(models, "corrupt.mesh"), "wb") as fh:
        fh.write(b"@@b@ this is not a mesh file")
    clear_scene()

    for name, text in (("parts.gfx", GFX), ("parts.asset", ASSET), ("other.asset", 'entity = { name = "elsewhere_entity" }')):
        with open(os.path.join(models, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    return os.path.join(models, "parts.asset")


def rig_sharing(bie, out_dir):
    build_synthetic_scene(bie, out_dir)
    first = bpy.data.objects["TestShape"]
    second = first.copy()
    second.data = first.data.copy()
    second.data.name = "TestShape2"
    bpy.context.scene.collection.objects.link(second)
    path = os.path.join(out_dir, "two_nodes.mesh")
    check("asset: export two nodes sharing a skeleton", export_mesh(path) == {"FINISHED"})
    clear_scene()
    bie.import_meshfile(path, new_rig=True)
    rigs = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    check("asset: nodes sharing a skeleton share one new armature", len(rigs) == 1 and len(meshes) == 2, f"{len(rigs)} rigs, {len(meshes)} meshes")
    bie.import_meshfile(path, new_rig=True)
    check("asset: a second import gets its own armature", len([o for o in bpy.data.objects if o.type == "ARMATURE"]) == 2)
    clear_scene()


def shader_modes(bie, out_dir):
    textures = dict(diff="t_diff.png", spec="t_spec.png", n="t_n.png")

    def make(blend, emissive):
        info = types.SimpleNamespace(blend=blend, emissive=emissive)
        return bie.create_shader(FakeMaterial("Any", **textures), f"mode_{blend}_{emissive}", out_dir, shader_info=lambda name: info)

    def principled(material):
        return material.node_tree.nodes["Principled BSDF"]

    for blend in ("blend", "cutout"):
        check(f"materials: {blend} shaders use the diffuse alpha", principled(make(blend, False)).inputs["Alpha"].is_linked)
    check("materials: opaque shaders ignore the diffuse alpha", not principled(make(None, False)).inputs["Alpha"].is_linked)

    glowing = principled(make(None, True))
    strength, color = glowing.inputs["Emission Strength"], glowing.inputs["Emission Color"]
    check("materials: emissive shaders glow with the diffuse colour where the normal map blue says so",
          strength.is_linked and strength.links[0].from_socket.name == "Blue" and strength.links[0].from_node.type == "SEPARATE_COLOR"
          and color.is_linked and color.links[0].from_node.type == "TEX_IMAGE")
    check("materials: other shaders do not glow", not principled(make(None, False)).inputs["Emission Strength"].is_linked)

    additive = make("additive", False)
    nodes = additive.node_tree.nodes
    surface = next(n for n in nodes if n.type == "OUTPUT_MATERIAL").inputs["Surface"].links[0].from_node
    check("materials: additive shaders add emission to a transparent shader", surface.type == "ADD_SHADER"
          and {link.from_node.type for socket in surface.inputs for link in socket.links} == {"BSDF_TRANSPARENT", "EMISSION"})
    place = {n.type: n.location.x for n in nodes if n.type in ("TEX_IMAGE", "EMISSION", "ADD_SHADER", "OUTPUT_MATERIAL")}
    check("materials: additive nodes run left to right", place["TEX_IMAGE"] < place["EMISSION"] < place["ADD_SHADER"] < place["OUTPUT_MATERIAL"], str(place))
    unused = nodes["Principled BSDF"]
    check("materials: the Principled BSDF of an additive material is muted and labelled", unused.mute and "export" in unused.label, unused.label)
    check("materials: additive shaders blend", additive.blend_method == "BLEND"
          and getattr(additive, "surface_render_method", "BLENDED") == "BLENDED")
    exported = {slot: os.path.basename(path) for slot, path in bie.get_material_textures(additive).items()}
    check("materials: the exporter still reads textures from an additive material", exported == textures, str(exported))

    guessed = [bie.shader_traits(name) for name in ("PdxMeshAlphaAdditive", "PdxMeshAlphaBlend", "PdxMeshShipDiffuseEmissive", "PdxMeshTerra")]
    check("materials: without shader files the name is used", guessed == [("additive", False), ("blend", False), (None, True), (None, False)], str(guessed))


def degenerate_faces(bie, pdx_data, out_dir):
    models = os.path.join(out_dir, "game", "gfx", "models", "test")
    clear_scene()
    bie.import_meshfile(os.path.join(models, "plain.mesh"))
    wanted = sum(len(o.data.polygons) for o in bpy.data.objects if o.type == "MESH")

    root = pdx_data.read_meshfile(os.path.join(models, "plain.mesh"))
    mesh = root.find("object")[0].find("mesh")
    mesh.attrib["tri"] = list(mesh.attrib["tri"]) + [0, 0, 1]
    path = os.path.join(out_dir, "degenerate.mesh")
    pdx_data.write_meshfile(path, root)
    clear_scene()
    for _ in range(10):
        bie.import_meshfile(path)
    counts = {len(o.data.polygons) for o in bpy.data.objects if o.type == "MESH"}
    check("asset: a face using a vertex twice is skipped, however often it is imported", counts == {wanted}, f"{counts} vs {wanted}")


def alpha_shaders(bie, out_dir):
    for shader, linked in (
        ("PdxMeshShip", False),
        ("PdxMeshShipDiffuseEmissive", False),
        ("PdxMeshAlphaBlend", True),
        ("PdxMeshShipDiffuseEmissiveAlpha", True),
        ("OmniMeshMegaAlpha", True),
    ):
        material = bie.create_shader(FakeMaterial(shader, diff="t_diff.png"), f"m_{shader}", out_dir)
        alpha = material.node_tree.nodes["Principled BSDF"].inputs["Alpha"]
        ok = alpha.is_linked == linked and (not linked or alpha.links[0].from_socket.name == "Alpha")
        check(f"materials: {shader} {'uses' if linked else 'ignores'} the diffuse alpha", ok)


def asset_collision(asset_mod, index, bie, out_dir):
    path = os.path.join(out_dir, "game", "gfx", "models", "test", "collider.mesh")
    clear_scene()
    kept = bie.import_meshfile(path, skip_collision=True)
    clear_scene()
    both = bie.import_meshfile(path)
    check("asset collision: import_meshfile can leave the collision mesh out", len(kept["objects"]) == 1 and len(both["objects"]) == 2,
          f"{len(kept['objects'])} vs {len(both['objects'])}")

    clear_scene()
    collection, _, report = asset_mod.import_entity(index, "collider_pair_entity")
    check("asset collision: left out by default", report.meshes == 2 and not [o for o in collection.objects if o.get("pdx_collision")], str(report.lines()))

    collection, _, report = asset_mod.import_entity(index, "collider_pair_entity", collision=True)
    boxes = [o for o in collection.all_objects if o.get("pdx_collision")]
    inner = collection.children.get(f"{collection.name} collision")
    check("asset collision: included on request, for every instance", len(boxes) == 2 and report.meshes == 4, f"{len(boxes)} boxes, {report.lines()}")
    check("asset collision: wireframes in a collection of their own",
          inner is not None and set(boxes) == set(inner.objects) and all(o.display_type == "WIRE" and o.hide_render for o in boxes))
    layer = asset_mod.find_layer_collection(inner)
    check("asset collision: the collision collection starts hidden", layer is not None and layer.hide_viewport and not asset_mod.find_layer_collection(collection).hide_viewport)
    check("asset collision: the visible meshes stay in the entity's collection",
          all(o.get("pdx_collision") is None for o in collection.objects if o.type == "MESH"))


def asset_animations(asset_mod, index, bie):
    def rigs_of(collection):
        return [o for o in collection.objects if o.type == "ARMATURE"]

    def actions_named(prefix):
        return sorted(a.name for a in bpy.data.actions if a.name.startswith(prefix))

    for a in list(bpy.data.actions):
        bpy.data.actions.remove(a)
    collection, _, none = asset_mod.import_entity(index, "top_entity")
    check("asset animations: off by default", not actions_named("body_mesh") and all(r.animation_data is None or r.animation_data.action is None for r in rigs_of(collection)))

    collection, _, report = asset_mod.import_entity(index, "top_entity", animations="default")
    rigs = rigs_of(collection)
    active = {r.animation_data.action for r in rigs if r.animation_data and r.animation_data.action}
    check("asset animations: the default state's animation plays on every armature, as one shared action",
          len(rigs) == 2 and len(active) == 1 and next(iter(active)).name == "body_mesh.idle", str(actions_named("body_mesh")))
    check("asset animations: only the default state's animation is imported", actions_named("body_mesh") == ["body_mesh.idle"], str(actions_named("body_mesh")))
    check("asset animations: the missing animation is not reported unless asked for", "animations" not in report.unresolved, str(report.unresolved))
    scene = bpy.context.scene
    check("asset animations: scene speed and range follow", scene.render.fps == 24 and scene.frame_end == 6, f"{scene.render.fps} fps, end {scene.frame_end}")
    scene.frame_set(1)
    bpy.context.view_layer.update()
    start = [tuple(pb.rotation_quaternion) for pb in rigs[0].pose.bones]
    scene.frame_set(6)
    bpy.context.view_layer.update()
    end = [tuple(pb.rotation_quaternion) for pb in rigs[0].pose.bones]
    other = [tuple(pb.rotation_quaternion) for pb in rigs[1].pose.bones]
    check("asset animations: the armatures animate, together", start != end and end == other, f"{start[0]} -> {end[0]}")

    on_bone = next(o for o in collection.objects if o.get("pdx_entity") == "leaf_entity" and o.parent_type == "BONE")
    scene.frame_set(1)
    bpy.context.view_layer.update()
    first = on_bone.matrix_world.translation.copy()
    scene.frame_set(6)
    bpy.context.view_layer.update()
    check("asset animations: a part attached to a bone follows it", (on_bone.matrix_world.translation - first).length > 0.05, f"{tuple(first)} -> {tuple(on_bone.matrix_world.translation)}")

    for a in list(bpy.data.actions):
        bpy.data.actions.remove(a)
    collection, _, report = asset_mod.import_entity(index, "top_entity", animations="all")
    rigs = rigs_of(collection)
    idle = bpy.data.actions.get("body_mesh.idle")
    check("asset animations: every state's animation is imported, only the default plays",
          actions_named("body_mesh") == ["body_mesh.idle", "body_mesh.work"] and all(r.animation_data.action is idle for r in rigs)
          and not any(r.animation_data.action is bpy.data.actions["body_mesh.work"] for r in rigs) and bpy.data.actions["body_mesh.work"].use_fake_user,
          str(actions_named("body_mesh")))
    check("asset animations: an animation that cannot be found is reported", report.unresolved.get("animations") == {"lost of body_entity"}, str(report.unresolved))

    ui = importlib.import_module(MODULE + ".pdx_blender.blender_ui")
    work = bpy.data.actions["body_mesh.work"]
    rig = rigs[0]
    on_bone = next(o for o in collection.objects if o.get("pdx_entity") == "leaf_entity" and o.parent_type == "BONE")
    check("asset picker: a rig is found from itself and from a part on its bone", asset_mod.rig_of(rig) is rig and asset_mod.rig_of(on_bone) in rigs)
    check("asset picker: an object with no rig has none", asset_mod.rig_of(bpy.data.objects.new("plain", None)) is None)
    labels = [asset_mod.action_label(a) for a in asset_mod.actions_for(rig)]
    check("asset picker: the animations made for a rig are offered, by animation id", labels == ["idle", "work"], str(labels))

    bpy.context.view_layer.objects.active = rig
    panel = types.SimpleNamespace(layout=mock.MagicMock())
    ui.IOPDX_PT_PDXblender_animation.draw(panel, bpy.context)
    menu = types.SimpleNamespace(layout=mock.MagicMock())
    ui.IOPDX_MT_animations.draw(menu, bpy.context)
    texts = [call.kwargs["text"] for call in menu.layout.operator.call_args_list]
    check("asset picker: the panel has a menu that lists Rest Pose and each animation", panel.layout.menu.called and texts == ["Rest Pose", "idle", "work"], str(texts))

    check("asset picker: picking an animation plays it", bpy.ops.io_pdx_mesh.set_animation(action=work.name) == {"FINISHED"}
          and rig.animation_data.action is work and rig.animation_data.action_slot is not None)
    scene = bpy.context.scene
    check("asset picker: the scene is long enough for it", scene.frame_end >= int(work.frame_range[1]), f"{scene.frame_end} vs {work.frame_range[1]}")
    check("asset picker: the other rig keeps its own animation", rigs[1].animation_data.action is not work)
    check("asset picker: picking the rest pose stops it", bpy.ops.io_pdx_mesh.set_animation(action="") == {"FINISHED"} and rig.animation_data.action is None)

    foreign = bpy.data.actions.new("foreign")
    foreign["pdx_armature"] = "somewhere else"
    for name in (foreign.name, "does not exist"):
        try:
            bpy.ops.io_pdx_mesh.set_animation(action=name)
            refused = False
        except RuntimeError:
            refused = True
        check(f"asset picker: '{name}' is refused for this rig", refused and rig.animation_data.action is None)
    bpy.data.actions.remove(foreign)


def world_key(obj):
    matrix = obj.matrix_world
    return (re.sub(r"_v\d+$", "", obj["pdx_entity"]), tuple(round(v, 2) for v in matrix.translation), matrix.copy())


def unmatched_worlds(first, second, tolerance=1e-3):
    remaining = list(second)
    missing = []
    for name, translation, matrix in sorted(first, key=lambda k: k[:2]):
        match = next((r for r in remaining if r[0] == name and all(abs(a - b) < tolerance for ra, rb in zip(matrix, r[2]) for a, b in zip(ra, rb))), None)
        if match is None:
            missing.append((name, translation))
        else:
            remaining.remove(match)
    return missing


def same_worlds(first, second, tolerance=1e-3):
    return len(first) == len(second) and not unmatched_worlds(first, second, tolerance)


def asset_export(pdx, asset_mod, index_mod, bie, out_dir, asset):
    import shutil
    from pathlib import Path

    export_mod = importlib.import_module(MODULE + ".pdx_blender.blender_asset_export")
    clausewitz = importlib.import_module(MODULE + ".clausewitz_text")

    ui = importlib.import_module(MODULE + ".pdx_blender.blender_ui")

    def drawn(draw_owner, **props):
        holder = types.SimpleNamespace(layout=mock.MagicMock(), **props)
        draw_owner.draw(holder, bpy.context)
        return str(holder.layout.mock_calls)

    operators = (bpy.ops.io_pdx_mesh.export_asset, bpy.ops.io_pdx_mesh.update_asset, bpy.ops.io_pdx_mesh.create_assembly)
    check("asset export: its buttons are shown and its operators can run",
          "update_asset" in drawn(ui.IOPDX_PT_PDXblender_file) and "create_assembly" in drawn(ui.IOPDX_PT_PDXblender_tools) and all(op.poll() for op in operators))
    dialog = dict(entity="", chk_all_states=False)
    check("asset: the import dialog warns about leaving animations out, only when they are left out",
          "without their animations" in drawn(ui.IOPDX_OT_import_asset_options, chk_animations=False, **dialog)
          and "without their animations" not in drawn(ui.IOPDX_OT_import_asset_options, chk_animations=True, **dialog))
    for rotation in ((45, 30, -20), (23.66, 4.64, 101.26), (350, 10, 20), (30, 45, 0)):
        matrix = asset_mod.local_matrix((1, 2, 3), rotation, 1.5)
        got = export_mod.locator_values(matrix, rotation)
        worst = max(abs(a - b) for ra, rb in zip(matrix, asset_mod.local_matrix(*got)) for a, b in zip(ra, rb))
        check(f"asset export: locator values for {rotation} give the same matrix and angles", worst < 1e-4 and max(abs(a - b) for a, b in zip(got[1], rotation)) < 1e-2, str(got))
    import math as _math

    expected = bie.swap_coord_space(Matrix.Rotation(_math.radians(45), 4, "X") @ Matrix.Rotation(_math.radians(30), 4, "Y"))
    got = asset_mod.local_matrix((0, 0, 0), (30, 45, 0), 1.0)
    check("asset: yaw and pitch together apply the pitch last, as the game does", max(abs(a - b) for ra, rb in zip(expected, got) for a, b in zip(ra, rb)) < 1e-6)

    for position, rotation, scale, exact in (
        ((10, 0, 0), (0, 90, 0), 3.0, True), ((0, 5, -7), (90, 0, 0), 1.0, True), ((1, 2, 3), (45, 30, -20), 2.5, True),
        ((5, 5, 5), (350, 10, 20), 1.0, True), ((0, 0, 0), (0, 90, 180), 1.0, False),
    ):
        matrix = asset_mod.local_matrix(position, rotation, scale)
        got = export_mod.locator_values(matrix, rotation)
        worst = max(abs(a - b) for ra, rb in zip(matrix, asset_mod.local_matrix(*got)) for a, b in zip(ra, rb))
        angles = max(abs(a - b) for a, b in zip(got[1], rotation))
        check(f"asset export: locator values for {rotation} give the same matrix", worst < 1e-4, f"{worst:.2e}")
        if exact:
            check(f"asset export: angles near the old ones are kept for {rotation}", angles < 1e-2 and abs(got[2] - scale) < 1e-6, str(got))

    game = Path(out_dir) / "game"
    copy = Path(out_dir) / "game_edit"
    shutil.rmtree(copy, ignore_errors=True)
    shutil.copytree(game, copy)
    asset_copy = copy / Path(asset).relative_to(game)

    def fresh(entity="top_entity"):
        clear_scene()
        index = index_mod.AssetIndex([copy])
        collection, root, _ = asset_mod.import_entity(index, entity)
        bpy.context.view_layer.update()
        return index, collection, root

    def locator_of(collection, name):
        return [o for o in collection.all_objects if o.get("pdx_locator") == name]

    def locators_in(path, entity):
        return index_mod.AssetIndex([copy]).entity(entity).locators

    index, collection, root = fresh()
    plan = export_mod.plan_update(root, index)
    check("asset export: an imported entity left alone has nothing to save", not plan.changes and not plan.problems and plan.checked >= 3, str(plan.lines()))

    (a,) = locator_of(collection, "a")
    a.location.x += 1.0
    plan = export_mod.plan_update(root, index)
    check("asset export: a moved locator is the one change", len(plan.changes) == 1 and plan.changes[0].locator == "a" and plan.changes[0].parts == (True, False, False), str(plan.lines()))
    before = asset_copy.read_text(encoding="utf-8")
    problems = export_mod.apply_update(plan)
    after = asset_copy.read_text(encoding="utf-8")
    different = [i for i, (x, y) in enumerate(zip(before.splitlines(), after.splitlines())) if x != y]
    check("asset export: only that line of the file changed", not problems and len(different) == 1 and before.count("\n") == after.count("\n"), f"{problems} {different}")
    check("asset export: the first version is kept as a backup", asset_copy.with_name(asset_copy.name + ".bak").read_text(encoding="utf-8") == before)
    saved = locators_in(copy, "top_entity")["a"]
    check("asset export: the locator has its new position and its old rotation and scale", saved.position == (11.0, 0.0, 0.0) and saved.rotation == (0.0, 90.0, 0.0) and saved.scale == 3.0, f"{saved.position} {saved.rotation} {saved.scale}")
    shutil.copyfile(asset_copy.with_name(asset_copy.name + ".bak"), asset_copy)
    asset_copy.with_name(asset_copy.name + ".bak").unlink()

    index, collection, root = fresh()
    (a,) = locator_of(collection, "a")
    parts = list(a.children)
    for part in parts:
        part.location.z += 4.0
    bpy.context.view_layer.update()
    moved = [world_key(o) for o in collection.all_objects if o.get("pdx_entity")]
    plan = export_mod.plan_update(root, index)
    check("asset export: moving every part on a locator moves the locator", len(plan.changes) == 1 and plan.changes[0].locator == "a" and not plan.problems, str(plan.lines()))
    export_mod.apply_update(plan)
    index, collection2, root2 = fresh()
    again = [world_key(o) for o in collection2.all_objects if o.get("pdx_entity")]
    check("asset export: after saving, the entities import where they were left", same_worlds(moved, again), f"{len(moved)} vs {len(again)}")
    shutil.copyfile(asset_copy.with_name(asset_copy.name + ".bak"), asset_copy)
    asset_copy.with_name(asset_copy.name + ".bak").unlink()

    def restore():
        shutil.copyfile(asset_copy.with_name(asset_copy.name + ".bak"), asset_copy)
        asset_copy.with_name(asset_copy.name + ".bak").unlink()

    def keys_of(collection):
        return [world_key(o) for o in collection.all_objects if o.get("pdx_entity")]

    index, collection, root = fresh()
    (a,) = locator_of(collection, "a")
    a.children[0].location.z += 7.0
    bpy.context.view_layer.update()
    moved = keys_of(collection)
    plan = export_mod.plan_update(root, index)
    check("asset export: a part moved off a shared locator gets a locator of its own", sorted(c.kind for c in plan.changes) == ["attach", "new_locator"] and not plan.problems, str(plan.lines()))
    before = asset_copy.read_text(encoding="utf-8")
    problems = export_mod.apply_update(plan)
    after = asset_copy.read_text(encoding="utf-8")
    different = set(after.splitlines()) ^ set(before.splitlines())
    saved = locators_in(copy, "top_entity")
    check("asset export: the other parts keep their locator, which is not changed", not problems and saved["a"].position == (10.0, 0.0, 0.0) and "a_2" in saved and len(different) <= 3, f"{problems} {sorted(saved)} {different}")
    check("asset export: after splitting, the entities import where they were left", same_worlds(moved, keys_of(fresh()[1])))
    restore()

    index, collection, root = fresh()
    (a,) = locator_of(collection, "a")
    for part in a.children:
        part.location.z += 3.0
    a.children[0].location.z += 2.0
    bpy.context.view_layer.update()
    moved = keys_of(collection)
    plan = export_mod.plan_update(root, index)
    export_mod.apply_update(plan)
    check("asset export: parts that moved together share the new locator", sorted(c.kind for c in plan.changes).count("attach") >= 1 and same_worlds(moved, keys_of(fresh()[1])), str(plan.lines()))
    restore()

    index, collection, root = fresh()
    (a,) = locator_of(collection, "a")
    a.location.x += 1.0
    for obj in collection.all_objects:
        if "pdx_locator" in obj:
            del obj["pdx_locator"]
    plan = export_mod.plan_update(root, index)
    check("asset export: scenes imported before locators were tagged are read by name", len(plan.changes) == 1 and plan.changes[0].locator == "a" and plan.checked >= 3, str(plan.lines()))

    index, collection, root = fresh("root_attach_entity")
    leaf = next(o for o in collection.all_objects if o.get("pdx_entity") == "leaf_entity")
    leaf.location.x += 5.0
    bpy.context.view_layer.update()
    moved = keys_of(collection)
    plan = export_mod.plan_update(root, index)
    new = [c for c in plan.changes if c.kind == "new_locator"]
    check("asset export: a part moved off the entity's own origin gets a locator", len(new) == 1 and new[0].parent_joint is None and [c.kind for c in plan.changes].count("attach") == 1, str(plan.lines()))
    export_mod.apply_update(plan)
    check("asset export: and imports where it was left", same_worlds(moved, keys_of(fresh("root_attach_entity")[1])))
    restore()

    index, collection, root = fresh()
    on_bone = next(o for o in collection.all_objects if o.get("pdx_entity") == "leaf_entity" and o.parent_type == "BONE" and o.parent_bone == "mid")
    on_bone.location.y += 4.0
    bpy.context.view_layer.update()
    moved = [world_key(on_bone)]
    plan = export_mod.plan_update(root, index)
    check("asset export: a part moved on a bone gets a locator on that bone in a copy of its entity", plan.count("variant") == 1 and "parent_joint = \"mid\"" in next(c.block for c in plan.changes if c.kind == "variant"), str(plan.lines()))
    export_mod.apply_update(plan)
    index, collection2, _ = fresh()
    again = [o for o in collection2.all_objects if o.get("pdx_entity") == "leaf_entity" and o.parent is not None and o.parent.get("pdx_locator") == "mid_2"]
    check("asset export: and imports where it was left", any(same_worlds(moved, [world_key(o)]) for o in again), str([world_key(o)[1] for o in again]))
    restore()

    index, collection, root = fresh()
    (a,) = locator_of(collection, "a")
    a.location.x += 1.0
    deep = next(o for o in collection.all_objects if o.type == "MESH")
    for given in (deep, [deep]):
        plan = export_mod.plan_update(given, index)
        check("asset export: whatever part is selected, the whole imported entity is read", len(plan.changes) == 1 and plan.changes[0].locator == "a" and plan.tops == ["top_entity"], str(plan.lines()))

    index, collection, root = fresh()
    (a,) = locator_of(collection, "a")
    child = next(o for o in a.children if o.get("pdx_entity") == "leaf_entity")
    model = next(o for o in child.children_recursive if o.type == "MESH")
    model.location.z += 6.0
    bpy.context.view_layer.update()
    expected = model.matrix_world.copy()
    plan = export_mod.plan_update(model, index)
    check("asset export: moving an entity's model counts as moving the entity", sorted(c.kind for c in plan.changes) == ["attach", "new_locator"] and not plan.problems, str(plan.lines()))
    export_mod.apply_update(plan)
    index, collection2, _ = fresh()
    (a2,) = locator_of(collection2, "a_2")
    moved_child = next(o for o in a2.children if o.get("pdx_entity") == "leaf_entity")
    saved_model = next(o for o in moved_child.children_recursive if o.type == "MESH")
    worst = max(abs(x - y) for rx, ry in zip(expected, saved_model.matrix_world) for x, y in zip(rx, ry))
    check("asset export: and the model imports where it was left", worst < 1e-3, f"{worst:.2e}")
    restore()

    def delete(obj):
        for o in [obj] + list(obj.children_recursive):
            bpy.data.objects.remove(o, do_unlink=True)

    index, collection, root = fresh()
    (a,) = locator_of(collection, "a")
    delete(next(o for o in a.children if o.get("pdx_entity") == "leaf_entity"))
    bpy.context.view_layer.update()
    moved = keys_of(collection)
    plan = export_mod.plan_update(root, index)
    check("asset export: a part deleted from the scene is taken out of the entity's attach lines", plan.count("remove") == 1 and not plan.problems, str(plan.lines()))
    export_mod.apply_update(plan)
    check("asset export: and the saved scene imports without it", same_worlds(moved, keys_of(fresh()[1])) and 'a = "leaf_entity"' not in asset_copy.read_text(encoding="utf-8").split("body_entity")[0][-200:] + "")
    restore()

    index, collection, root = fresh()
    ups = locator_of(collection, "up")
    delete(next(o for o in ups[0].children if o.get("pdx_entity") == "leaf_entity"))
    bpy.context.view_layer.update()
    moved = keys_of(collection)
    plan = export_mod.plan_update(root, index)
    check("asset export: a part deleted from one copy of an entity gives that copy an entity of its own", plan.count("variant") == 1 and not plan.problems, str(plan.lines()))
    export_mod.apply_update(plan)
    check("asset export: and the saved scene imports without that part only", same_worlds(moved, keys_of(fresh()[1])))
    restore()

    index, collection, root = fresh("root_attach_entity")
    delete(next(o for o in collection.all_objects if o.get("pdx_entity") == "leaf_entity"))
    bpy.context.view_layer.update()
    moved = keys_of(collection)
    plan = export_mod.plan_update(root, index)
    export_mod.apply_update(plan)
    text = asset_copy.read_text(encoding="utf-8")
    entity_line = next(line for line in text.splitlines() if '"root_attach_entity"' in line)
    check("asset export: an attach block left empty is removed", plan.count("remove") == 1 and "attach = {" not in entity_line and same_worlds(moved, keys_of(fresh("root_attach_entity")[1])), entity_line)
    restore()

    def saved_and_settled(plan, collection, entity="top_entity"):
        problems = export_mod.apply_update(plan)
        export_mod.commit_scene(plan)
        again = export_mod.plan_update(next(o for o in collection.all_objects if o.get("pdx_entity") and o.parent is None), index_mod.AssetIndex([copy]))
        settled = keys_of(collection)
        return not problems and not again.changes and same_worlds(settled, keys_of(fresh(entity)[1])), f"{problems} {again.lines()[:2]}"

    index, collection, root = fresh()
    (a,) = locator_of(collection, "a")
    leaf = next(o for o in a.children if o.get("pdx_entity") == "leaf_entity")
    bpy.data.objects.remove(next(o for o in leaf.children_recursive if o.type == "MESH"), do_unlink=True)
    plan = export_mod.plan_update(root, index)
    check("asset export: deleting only a part's model deletes the part", plan.count("remove") == 1 and not plan.problems, str(plan.lines()))
    ok, why = saved_and_settled(plan, collection)
    check("asset export: and the saved scene matches an import of it", ok, why)
    restore()

    index, collection, root = fresh()
    up = locator_of(collection, "up")[0]
    delete(up)
    bpy.context.view_layer.update()
    plan = export_mod.plan_update(root, index)
    check("asset export: deleting a locator with its parts deletes the parts", plan.count("variant") == 1 and "up = " not in next(c.block for c in plan.changes if c.kind == "variant").split("attach")[1], str(plan.lines()))
    ok, why = saved_and_settled(plan, collection)
    check("asset export: and the saved scene matches an import of it", ok, why)
    restore()

    index, collection, root = fresh()
    body = next(o for o in collection.all_objects if o.get("pdx_entity") == "body_entity")
    for part in [o for o in body.children_recursive if o.get("pdx_entity") == "leaf_entity" and (o.parent_type == "BONE" or o.parent.name.startswith("TestLocator"))]:
        delete(part)
    bpy.context.view_layer.update()
    plan = export_mod.plan_update(root, index)
    block = next((c.block for c in plan.changes if c.kind == "variant"), "")
    check("asset export: parts on a bone and on a mesh locator can be deleted", plan.count("variant") == 1 and "mid =" not in block and "TestLocator =" not in block and "up =" in block, block[-300:])
    ok, why = saved_and_settled(plan, collection)
    check("asset export: and the saved scene matches an import of it", ok, why)
    restore()

    index, collection, root = fresh()
    body = next(o for o in collection.all_objects if o.get("pdx_entity") == "body_entity")
    for model in [o for o in export_mod.content_objects(body) if o.type == "MESH"]:
        bpy.data.objects.remove(model, do_unlink=True)
    plan = export_mod.plan_update(root, index)
    check("asset export: a model that parts hang from by its bones is kept, and that is reported", not plan.count("variant") and any("bones or locators" in p for p in plan.problems), str(plan.lines()))

    index, collection, root = fresh()
    body = next(o for o in collection.all_objects if o.get("pdx_entity") == "body_entity")
    for part in [o for o in body.children_recursive if o.get("pdx_entity") == "leaf_entity" and (o.parent_type == "BONE" or o.parent.name.startswith("TestLocator"))]:
        delete(part)
    for model in [o for o in export_mod.content_objects(body) if o.type == "MESH"]:
        bpy.data.objects.remove(model, do_unlink=True)
    bpy.context.view_layer.update()
    plan = export_mod.plan_update(root, index)
    block = next((c.block for c in plan.changes if c.kind == "variant"), "")
    check("asset export: deleting the model of a part that has parts on it removes only the model", plan.count("variant") == 1 and "pdxmesh" not in block and "up =" in block and not plan.problems, block[-300:] + str(plan.problems))
    ok, why = saved_and_settled(plan, collection)
    check("asset export: and the saved scene matches an import of it", ok, why)
    restore()

    index, collection, root = fresh()
    (a,) = locator_of(collection, "a")
    loose = [o for o in a.children if o.get("pdx_entity")]
    worlds = [o.matrix_world.copy() for o in loose]
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    a.select_set(True)
    bpy.context.view_layer.objects.active = a
    bpy.ops.object.delete()
    bpy.context.view_layer.update()
    stayed = all(max(abs(x - y) for rx, ry in zip(w, o.matrix_world) for x, y in zip(rx, ry)) < 1e-4 and o.parent is None for w, o in zip(worlds, loose))
    plan = export_mod.plan_update(root, index)
    check("asset export: parts left loose when their locator was deleted are kept where they are, not deleted", plan.adopted == 3 and not plan.count("remove"), str(plan.lines()) + f" stayed={stayed}")
    loose[0].location.x += 5.0
    bpy.context.view_layer.update()
    plan = export_mod.plan_update(root, index)
    check("asset export: and moving one of them is saved like any move", plan.adopted == 3 and plan.count("new_locator") == 1 and not plan.count("remove"), str(plan.lines()))
    ok, why = saved_and_settled(plan, collection)
    check("asset export: and the saved scene matches an import of it", ok, why)
    restore()

    index, collection, root = fresh()
    (a,) = locator_of(collection, "a")
    a.children[0].location.z += 7.0
    ups = locator_of(collection, "up")
    ups[0].location.x += 1.0
    delete(next(o for o in ups[1].children if o.get("pdx_entity") == "leaf_entity"))
    model = next(o for o in collection.all_objects if o.type == "MESH" and o.parent is not None and o.parent.get("pdx_entity") == "leaf_entity" and o.parent.parent is a)
    model.location.z += 3.0
    bpy.context.view_layer.update()
    expected = [(k[0], k[1], k[2] @ export_mod.content_shift(o)[0]) for k, o in ((world_key(o), o) for o in collection.all_objects if o.get("pdx_entity"))]
    plan = export_mod.plan_update(root, index)
    check("asset export: a mixed set of changes is planned without problems", plan.changes and not plan.problems, str(plan.lines()))
    export_mod.apply_update(plan)
    export_mod.commit_scene(plan)
    again = export_mod.plan_update(root, index_mod.AssetIndex([copy]))
    check("asset export: after saving, the scene needs nothing more saved", not again.changes and not again.problems, str(again.lines()))
    settled = keys_of(collection)
    check("asset export: and it is still where it was left", same_worlds(expected, settled))
    check("asset export: and it matches an import of what was saved", same_worlds(settled, keys_of(fresh()[1])))
    restore()

    index, collection, root = fresh()
    (a,) = locator_of(collection, "a")
    a.children[1].location.z += 2.0
    bpy.context.view_layer.update()
    plan = export_mod.plan_update(root, index)
    export_mod.apply_update(plan)
    export_mod.commit_scene(plan)
    (a,) = locator_of(collection, "a")
    a.location.x += 3.0
    ups = locator_of(collection, "up")
    ups[0].location.y += 2.0
    bpy.context.view_layer.update()
    expected = keys_of(collection)
    plan = export_mod.plan_update(root, index_mod.AssetIndex([copy]))
    check("asset export: a second update in the same scene is planned from what the first saved", plan.changes and not plan.problems, str(plan.lines()))
    export_mod.apply_update(plan)
    export_mod.commit_scene(plan)
    check("asset export: and the twice saved scene imports as it was left", same_worlds(expected, keys_of(fresh()[1])))
    restore()

    index, collection, root = fresh()
    clear_group = bpy.data.objects.new("grouped parts", None)
    bpy.context.scene.collection.objects.link(clear_group)
    root.parent = clear_group
    (a,) = locator_of(collection, "a")
    a.location.x += 1.0
    plan = export_mod.plan_update(clear_group, index)
    check("asset export: an empty holding imported entities brings them in", len(plan.changes) == 1 and plan.tops == ["top_entity"], str(plan.lines()))

    index, collection, root = fresh()
    (a,) = locator_of(collection, "a")
    a.scale = (a.scale[0] * 2,) * 3
    plan = export_mod.plan_update(root, index)
    export_mod.apply_update(plan)
    check("asset export: a rescaled locator saves its new scale", plan.changes and plan.changes[0].parts == (False, False, True) and locators_in(copy, "top_entity")["a"].scale == 6.0, str(plan.lines()))
    shutil.copyfile(asset_copy.with_name(asset_copy.name + ".bak"), asset_copy)
    asset_copy.with_name(asset_copy.name + ".bak").unlink()

    index, collection, root = fresh()
    ups = locator_of(collection, "up")
    check("asset export: two copies of an entity have a locator each", len(ups) == 2, str(len(ups)))
    original_up = locators_in(copy, "body_entity")["up"].position
    ups[0].location.x += 1.0
    bpy.context.view_layer.update()
    moved = keys_of(collection)
    plan = export_mod.plan_update(root, index)
    check("asset export: a locator moved on one copy gives that copy an entity of its own", plan.count("variant") == 1 and plan.count("repoint") == 1 and plan.count("locator") == 0 and not plan.problems, str(plan.lines()))
    export_mod.apply_update(plan)
    saved_text = asset_copy.read_text(encoding="utf-8")
    check("asset export: the original entity and the other copy are left alone", locators_in(copy, "body_entity")["up"].position == original_up and "body_entity_v2" in saved_text and saved_text.count('"body_entity"') >= 1, saved_text[-400:])
    check("asset export: and the scene imports as it was left", same_worlds(moved, keys_of(fresh()[1])))
    restore()

    index, collection, root = fresh()
    ups = locator_of(collection, "up")
    ups[0].location.x += 1.0
    ups[1].location.x += 2.0
    bpy.context.view_layer.update()
    moved = keys_of(collection)
    plan = export_mod.plan_update(root, index)
    check("asset export: copies moved differently each get an entity", plan.count("variant") == 2 and plan.count("repoint") == 2 and not plan.problems, str(plan.lines()))
    export_mod.apply_update(plan)
    check("asset export: and the scene imports as it was left, copies apart", same_worlds(moved, keys_of(fresh()[1])))
    restore()

    index, collection, root = fresh()
    ups = locator_of(collection, "up")
    for up in ups:
        up.location.x += 1.0
    bpy.context.view_layer.update()
    moved = keys_of(collection)
    plan = export_mod.plan_update(root, index)
    check("asset export: copies moved the same way, of an entity nothing else uses, change that entity", plan.count("variant") == 0 and plan.count("locator") == 1 and not plan.problems, str(plan.lines()))
    export_mod.apply_update(plan)
    check("asset export: and the scene imports as it was left", same_worlds(moved, keys_of(fresh()[1])))
    restore()

    index, collection, root = fresh()
    (a,) = locator_of(collection, "a")
    a.location.x += 1.0
    plan = export_mod.plan_update(root, index, protected=[copy])
    check("asset export: files in a protected folder are not changed", not plan.changes and any("game file" in p for p in plan.problems), str(plan.lines()))

    mini = Path(out_dir) / "mini"
    shutil.rmtree(mini, ignore_errors=True)
    models = mini / "gfx" / "models"
    models.mkdir(parents=True)
    (models / "base.asset").write_text('# keep me\nentity = { name = "base_entity" locator = { name = "l" position = { 1 2 3 } } }\n', encoding="utf-8")
    (models / "derived.asset").write_text('entity = { name = "derived_entity" clone = "base_entity" scale = 2 }\n', encoding="utf-8")
    clear_scene()
    mini_index = index_mod.AssetIndex([mini])
    collection, root, _ = asset_mod.import_entity(mini_index, "derived_entity")
    (marker,) = locator_of(collection, "l")
    marker.location.z += 5.0
    plan = export_mod.plan_update(root, mini_index)
    check("asset export: a locator is saved where its entity defined it, not in the entity that clones it", len(plan.changes) == 1 and plan.changes[0].path.name == "base.asset" and plan.changes[0].owner == "base_entity", str(plan.lines()))
    export_mod.apply_update(plan, backup=False)
    text = (models / "base.asset").read_text(encoding="utf-8")
    check("asset export: the rest of that file is left as it was", text.startswith("# keep me\n") and "position = { 1 7 3 }" in text and not (models / "base.asset.bak").exists(), text)

    def mini_setup(name, files):
        folder = Path(out_dir) / name
        shutil.rmtree(folder, ignore_errors=True)
        (folder / "gfx" / "models").mkdir(parents=True)
        for filename, content in files.items():
            (folder / "gfx" / "models" / filename).write_text(content, encoding="utf-8")
        return folder

    def mini_scene(roots, entity):
        clear_scene()
        idx = index_mod.AssetIndex([Path(r) for r in roots])
        coll, top, _ = asset_mod.import_entity(idx, entity, textures=False)
        bpy.context.view_layer.update()
        return idx, coll, top

    def tip_of(coll, name="tip_entity"):
        return next(o for o in coll.all_objects if o.get("pdx_entity") == name)

    shared = 'entity = { name = "base_entity" locator = { name = "l" position = { 1 2 3 } } attach = { l = "tip_entity" } }\nentity = { name = "tip_entity" }\nentity = { name = "top2_entity" locator = { name = "x" position = { 10 0 0 } } attach = { x = "base_entity" } }\n'
    folder = mini_setup("mini_shared", {"a.asset": shared + 'entity = { name = "other_user_entity" attach = { root = "base_entity" } }\n'})
    idx, coll, top = mini_scene([folder], "top2_entity")
    tip = tip_of(coll)
    tip.location.z += 5.0
    bpy.context.view_layer.update()
    moved = keys_of(coll)
    plan = export_mod.plan_update(top, idx)
    check("asset export: an entity another entity uses is copied, not changed", plan.count("variant") == 1 and plan.count("repoint") == 1 and plan.count("locator") == 0, str(plan.lines()))
    export_mod.apply_update(plan, backup=False)
    after = index_mod.AssetIndex([folder])
    text = (folder / "gfx" / "models" / "a.asset").read_text(encoding="utf-8")
    check("asset export: the other user still has the original", after.entity("base_entity").locators["l"].position == (1.0, 2.0, 3.0) and 'attach = { root = "base_entity" }' in text, text)
    check("asset export: and the saved scene imports as it was left", same_worlds(moved, keys_of(mini_scene([folder], "top2_entity")[1])))

    folder = mini_setup("mini_alone", {"a.asset": shared})
    idx, coll, top = mini_scene([folder], "top2_entity")
    tip_of(coll).location.z += 5.0
    bpy.context.view_layer.update()
    moved = keys_of(coll)
    plan = export_mod.plan_update(top, idx)
    check("asset export: an entity only the scene uses is changed in place", plan.count("variant") == 0 and plan.count("locator") == 1, str(plan.lines()))
    export_mod.apply_update(plan, backup=False)
    check("asset export: and the saved scene imports as it was left", same_worlds(moved, keys_of(mini_scene([folder], "top2_entity")[1])))

    game_folder = mini_setup("mini_game", {"g.asset": '@s = 3\nentity = { name = "gbase_entity" scale = @s locator = { name = "l" position = { 0 0 5 } } attach = { l = "gtip_entity" } }\nentity = { name = "gtip_entity" }\n'})
    mod_folder = mini_setup("mini_mod", {"m.asset": 'entity = { name = "mtop_entity" locator = { name = "x" position = { 4 0 0 } } attach = { x = "gbase_entity" } }\n'})
    idx, coll, top = mini_scene([mod_folder, game_folder], "mtop_entity")
    tip_of(coll, "gtip_entity").location.z += 5.0
    bpy.context.view_layer.update()
    moved = keys_of(coll)
    game_before = (game_folder / "gfx" / "models" / "g.asset").read_bytes()
    plan = export_mod.plan_update(top, idx, protected=[game_folder])
    check("asset export: a game entity that changed is copied into the mod's file", plan.count("variant") == 1 and plan.changes[0].path.name == "m.asset", str(plan.lines()))
    export_mod.apply_update(plan, backup=False)
    mod_text = (mod_folder / "gfx" / "models" / "m.asset").read_text(encoding="utf-8")
    check("asset export: the game file is untouched and the copy has the variable's value", (game_folder / "gfx" / "models" / "g.asset").read_bytes() == game_before and "scale = 3" in mod_text and "@s" not in mod_text, mod_text)
    check("asset export: and the saved scene imports as it was left", same_worlds(moved, keys_of(mini_scene([mod_folder, game_folder], "mtop_entity")[1])))
    idx, coll, top = mini_scene([mod_folder, game_folder], "mtop_entity")
    tip_of(coll, "gtip_entity").location.z += 5.0
    plan = export_mod.plan_update(top, idx, protected=[mod_folder, game_folder])
    check("asset export: nothing is written when the top entity is in a protected file", not plan.changes and any("game file" in p for p in plan.problems), str(plan.lines()))
    for name in ("mini_shared", "mini_alone", "mini_game", "mini_mod"):
        shutil.rmtree(Path(out_dir) / name, ignore_errors=True)

    clear_scene()
    scene = bpy.context.scene
    index = index_mod.AssetIndex([copy])
    group = bpy.data.objects.new("my_assembly", None)
    scene.collection.objects.link(group)
    group.scale = (2.0, 2.0, 2.0)
    placed = (
        ("leaf_entity", (100, 0, 0), (90, 0, 0), 1.0), ("leaf_entity", (0, 50, 0), (0, 0, 0), 2.0), ("body_entity", (0, 0, -30), (10, 20, 30), 1.0),
    )
    made = []
    for name, position, rotation, scale in placed:
        _, part, _ = asset_mod.import_entity(index, name)
        part.parent = group
        part.matrix_basis = asset_mod.local_matrix(position, rotation, scale) @ Matrix.Scale(index.entity(name).scale, 4)
        made.append(part)
    socket = bpy.data.objects.new("socket", None)
    scene.collection.objects.link(socket)
    socket.parent = group
    socket.matrix_basis = asset_mod.local_matrix((5, 6, 7), (0, 45, 0), 1.0)
    _, on_socket, _ = asset_mod.import_entity(index, "leaf_entity")
    on_socket.parent = socket
    made.append(on_socket)
    for name in ("ghost_entity", "broken_entity", "other_entity"):
        fake = bpy.data.objects.new(f"fake {name}", None)
        fake["pdx_entity"] = name
        scene.collection.objects.link(fake)
        fake.parent = group
    loose = bpy.data.objects.new("loose", bpy.data.meshes.new("loose"))
    scene.collection.objects.link(loose)
    loose.parent = group
    bpy.context.view_layer.update()

    assembly = export_mod.read_assembly(group, index)
    names = [loc[0] for loc in assembly.locators]
    check("asset export: a locator for each part and each empty, an attach for each part", len(assembly.locators) == 7 and len(assembly.attach) == 7 and len(set(names)) == 7 and "socket" in names, str(names))
    check("asset export: parts are named after their entity", {"leaf_1", "leaf_2", "body_1"} <= set(names), str(names))
    check("asset export: the empty's scale becomes the entity's", abs(assembly.scale - 2.0) < 1e-6, str(assembly.scale))

    path = copy / "gfx" / "models" / "test" / "assembly.asset"
    replaced, problems = export_mod.write_assembly(assembly, path, index, 9999.0)
    joined = "\n".join(problems)
    check("asset export: a new file is written", path.is_file() and not replaced)
    check("asset export: entities that do not exist are reported", "ghost_entity" in joined, joined)
    check("asset export: meshes that still need exporting are reported", "gone.mesh" in joined and "Save Mesh" in joined, joined)
    check("asset export: an undefined pdxmesh is reported", "no_such_pdxmesh" in joined, joined)
    check("asset export: objects that cannot be saved are reported", "loose" in joined and "not saved" in joined, joined)

    saved_index = index_mod.AssetIndex([copy])
    saved = saved_index.entity("my_assembly")
    check("asset export: the entity reads back", saved is not None and saved.scale == 2.0 and len(saved.locators) == 7 and len(saved.attach) == 7, str(saved and (saved.scale, len(saved.locators))))
    check("asset export: the cull radius is written", "cull_radius = 9999" in path.read_text(encoding="utf-8"))

    clear_originals = [o for o in made]
    original = [(o, o.matrix_world.copy()) for o in clear_originals]
    check("asset export: the originals are all entities", all(o.get("pdx_entity") for o in clear_originals))
    _, _, report = asset_mod.import_entity(saved_index, "my_assembly")
    bpy.context.view_layer.update()
    imported = [o for o in bpy.data.objects if o.get("pdx_entity") and o not in clear_originals]
    top = next(o for o in imported if o["pdx_entity"] == "my_assembly")
    direct = [o for o in imported if o["pdx_entity"] in ("leaf_entity", "body_entity") and o.parent is not None and o.parent.get("pdx_locator") and o.parent.parent is top]
    check("asset export: importing the saved entity puts every part where it was", len(direct) == 4 and same_worlds([world_key(o) for o in clear_originals], [world_key(o) for o in direct]),
          f"{sorted(world_key(o)[:2] for o in clear_originals)} vs {sorted(world_key(o)[:2] for o in direct)}")

    replaced, _ = export_mod.write_assembly(export_mod.read_assembly(group, index), path, index)
    check("asset export: saving again replaces the entity", replaced and len(clausewitz.parse(path.read_text(encoding="utf-8")).root.blocks("entity")) == 1)
    second = export_mod.read_assembly(group, index, name="second_assembly")
    export_mod.write_assembly(second, path, index)
    entities = clausewitz.parse(path.read_text(encoding="utf-8")).root.blocks("entity")
    check("asset export: another entity is added to the same file", [b.get("name") for b in entities] == ["my_assembly", "second_assembly"])

    for bad, label in ((made[0], "an imported entity"), (loose, "a mesh")):
        try:
            export_mod.read_assembly(bad, index)
            refused = False
        except ValueError:
            refused = True
        check(f"asset export: {label} is refused as an assembly", refused)

    settings = pdx.IO_PDX_SETTINGS
    saved_settings = (settings.game_directory, settings.extra_asset_directories)
    settings.game_directory, settings.extra_asset_directories = str(game), ""
    try:
        bpy.context.view_layer.objects.active = group
        target = copy / "gfx" / "models" / "test" / "from_operator.asset"
        check("asset export: the operator writes the file", bpy.ops.io_pdx_mesh.export_asset(filepath=str(target), entity="via_operator") == {"FINISHED"} and index_mod.AssetIndex([copy]).entity("via_operator") is not None)
        try:
            bpy.context.view_layer.objects.active = made[0]
            bpy.ops.io_pdx_mesh.export_asset(filepath=str(target))
            refused = False
        except RuntimeError:
            refused = True
        check("asset export: the operator refuses an imported entity", refused)

        index, collection, root = fresh()
        (a,) = locator_of(collection, "a")
        a.location.x += 1.0
        bpy.context.view_layer.objects.active = next(o for o in collection.all_objects if o.type == "MESH")
        check("asset export: the update operator changes the file", bpy.ops.io_pdx_mesh.update_asset() == {"FINISHED"} and locators_in(copy, "top_entity")["a"].position == (11.0, 0.0, 0.0))
        check("asset export: the update operator makes a backup", asset_copy.with_name(asset_copy.name + ".bak").is_file())
        shutil.copyfile(asset_copy.with_name(asset_copy.name + ".bak"), asset_copy)
        asset_copy.with_name(asset_copy.name + ".bak").unlink()

        index, collection, root = fresh()
        locator_of(collection, "up")[0].location.x += 1.0
        model = next(o for o in collection.all_objects if o.type == "MESH")
        bpy.context.view_layer.objects.active = model
        for obj in bpy.context.selected_objects:
            obj.select_set(False)
        model.select_set(True)
        check("asset export: the update operator writes a copy of a shared entity", bpy.ops.io_pdx_mesh.update_asset() == {"FINISHED"} and "body_entity_v2" in asset_copy.read_text(encoding="utf-8"))
        once = asset_copy.read_text(encoding="utf-8")
        bpy.ops.io_pdx_mesh.update_asset()
        check("asset export: and running it again in the same scene changes nothing", asset_copy.read_text(encoding="utf-8") == once)
        restore()

        clear_scene()
        index = index_mod.AssetIndex([copy])
        _, first_root, _ = asset_mod.import_entity(index, "leaf_entity")
        _, second_root, _ = asset_mod.import_entity(index, "leaf_entity")
        second_root.location = (30, 0, 0)
        bpy.context.view_layer.update()
        worlds = [first_root.matrix_world.copy(), second_root.matrix_world.copy()]
        for obj in bpy.context.selected_objects:
            obj.select_set(False)
        first_root.select_set(True)
        second_root.select_set(True)
        check("asset export: the group operator makes an empty", bpy.ops.io_pdx_mesh.create_assembly(name="grouped") == {"FINISHED"})
        grouped = bpy.data.objects["grouped"]
        bpy.context.view_layer.update()
        check("asset export: grouped parts stay where they were", first_root.parent is grouped and second_root.parent is grouped
              and all(max(abs(a - b) for ra, rb in zip(w, o.matrix_world) for a, b in zip(ra, rb)) < 1e-5 for w, o in zip(worlds, (first_root, second_root))))
    finally:
        settings.game_directory, settings.extra_asset_directories = saved_settings
    shutil.rmtree(copy, ignore_errors=True)
    shutil.rmtree(mini, ignore_errors=True)
    clear_scene()


def asset_import(pdx, bie, pdx_data, out_dir):
    asset_mod = importlib.import_module(MODULE + ".pdx_blender.blender_asset")
    index_mod = importlib.import_module(MODULE + ".pdx_asset")
    rig_sharing(bie, out_dir)
    alpha_shaders(bie, out_dir)
    shader_modes(bie, out_dir)
    asset = build_game_folder(bie, pdx_data, out_dir)
    degenerate_faces(bie, pdx_data, out_dir)
    index = index_mod.AssetIndex([index_mod.find_mod_root(asset)])
    check("asset: index reads the game folder", index.entity("top_entity") is not None, str(index.warnings))
    check("asset: entities nothing else attaches", index.root_entities(asset) == ROOT_ENTITIES, str(index.root_entities(asset)))
    check("asset: tree size counts attach pairs", index.tree_size("top_entity") == (11, 10, 3), str(index.tree_size("top_entity")))

    forward = Vector((0, -1, 0))
    for rotation, expected, label in (
        ((90, 0, 0), (-1, 0, 0), "yaw 90 turns the front towards -X"),
        ((0, -90, 0), (0, 0, -1), "pitch -90 points the front down"),
        ((0, 90, 180), (0, 0, 1), "roll is applied before pitch"),
    ):
        got = asset_mod.local_matrix((0, 0, 0), rotation, 1.0).to_3x3() @ forward
        check(f"asset: rotation convention, {label}", (got - Vector(expected)).length < 1e-5, str(tuple(got)))
    up = asset_mod.local_matrix((0, 0, 0), (0, 0, 180), 1.0).to_3x3() @ Vector((0, 0, 1))
    check("asset: roll 180 turns the top downwards", (up - Vector((0, 0, -1))).length < 1e-5, str(tuple(up)))
    placed = asset_mod.local_matrix((1, 2, 3), (0, 0, 0), 2.0)
    check("asset: positions swap Y and Z and keep scale", (placed.translation - Vector((1, 3, 2))).length < 1e-5 and abs(placed.to_scale().x - 2) < 1e-5, str(placed))

    collection, root, report = asset_mod.import_entity(index, "top_entity")
    bpy.context.view_layer.update()
    check("asset: collection and root named after the entity", collection.name == "top_entity" and root.name == "top_entity")
    check("asset: entity count", report.instances == 10, str(report.lines()))
    check("asset: mesh object count", report.meshes == 9, str(report.lines()))
    check("asset: unknown attach point reported", "attach points" in report.unresolved, str(report.unresolved))
    check("asset: root carries entity scale", abs(root.matrix_world.to_scale().x - 2.0) < 1e-4, str(root.matrix_world.to_scale()))

    bodies = [o for o in collection.objects if o.get("pdx_entity") == "body_entity"]
    check("asset: two body entities", len(bodies) == 2)
    check("asset: attached at the locator with scale and position", all(
        abs(b.matrix_world.translation.x - 20.0) < 1e-3 and abs(b.matrix_world.to_scale().x - 6.0) < 1e-3 for b in bodies),
        str([tuple(b.matrix_world.translation) for b in bodies]))
    rigs = [o for o in collection.objects if o.type == "ARMATURE"]
    check("asset: one rig per skinned instance, sharing data", len(rigs) == 2 and rigs[0].data is rigs[1].data, str(len(rigs)))
    skinned = [o for o in collection.objects if o.type == "MESH" and o.modifiers]
    check("asset: each skinned mesh follows its own rig", len(skinned) == 2 and len({m.modifiers[0].object for m in skinned}) == 2)
    check("asset: skin weights shared", all(len(o.vertex_groups) == 3 for o in skinned), str([len(o.vertex_groups) for o in skinned]))

    leaves = [o for o in collection.objects if o.get("pdx_entity") == "leaf_entity"]
    parents = sorted(o.parent.type + ":" + o.parent.name.split(".")[0] for o in leaves)
    check("asset: leaf parents are locators and rigs", {"EMPTY:TestLocator", "ARMATURE:io_pdx_rig", "EMPTY:up", "EMPTY:a"} <= set(parents), str(parents))
    on_bone = next(o for o in leaves if o.parent.type == "ARMATURE")
    rig = on_bone.parent
    expected = (rig.matrix_world @ rig.data.bones["mid"].matrix_local).translation
    check("asset: attached to a bone at its rest position", (on_bone.matrix_world.translation - expected).length < 1e-3,
          f"{tuple(on_bone.matrix_world.translation)} vs {tuple(expected)}")
    check("asset: the leaf on a bone is parented to that bone", on_bone.parent_type == "BONE" and on_bone.parent_bone == "mid", f"{on_bone.parent_type} {on_bone.parent_bone}")
    plain = {o.data for o in collection.objects if o.type == "MESH" and not o.modifiers}
    check("asset: repeated meshes share data", len(plain) == 1, str(len(plain)))

    images = {n.image.name for m in bpy.data.materials for n in m.node_tree.nodes if n.type == "TEX_IMAGE" and n.image}
    check("asset: textures found next to the meshes", {"t_diff.png", "t_spec.png", "t_n.png"} <= images, str(sorted(images)))

    collection, root_node, rooted = asset_mod.import_entity(index, "root_attach_entity")
    leaf = next(o for o in collection.objects if o.get("pdx_entity") == "leaf_entity")
    check("asset: 'root' is the entity's own origin", leaf.parent is root_node and not rooted.unresolved, str(rooted.unresolved))
    _, _, empty = asset_mod.import_entity(index, "empty_element_entity")
    check("asset: a mesh element without geometry is skipped", empty.meshes == 1 and not empty.unresolved, str(empty.lines()))
    collection, mixed_root, mixed = asset_mod.import_entity(index, "mixed_entity")
    check("asset: a mesh that fails to import does not stop the rest",
          mixed.instances == 4 and mixed.meshes == 1 and list(mixed.unresolved) == ["mesh files that failed to import"], str(mixed.lines()))
    check("asset: a failed import leaves no stray objects", [o for o in collection.objects if o.parent is None] == [mixed_root], str([o.name for o in collection.objects if o.parent is None]))

    _, _, cycle = asset_mod.import_entity(index, "cycle_entity")
    check("asset: attaching an entity to itself is reported", "entities attached to themselves" in cycle.unresolved, str(cycle.unresolved))
    _, _, broken = asset_mod.import_entity(index, "broken_entity")
    check("asset: missing mesh file and entity reported", {"mesh files", "entities"} <= set(broken.unresolved), str(broken.unresolved))
    _, _, other = asset_mod.import_entity(index, "other_entity")
    check("asset: missing pdxmesh reported", "pdxmeshes" in other.unresolved, str(other.unresolved))

    asset_animations(asset_mod, index, bie)
    asset_collision(asset_mod, index, bie, out_dir)
    asset_export(pdx, asset_mod, index_mod, bie, out_dir, asset)

    before = len(bpy.data.objects)
    collection, _, report = asset_mod.import_entity(index, "override_entity")
    mesh_obj = next(o for o in collection.objects if o.type == "MESH")
    names = {n.image.name for n in mesh_obj.data.materials[0].node_tree.nodes if n.type == "TEX_IMAGE" and n.image}
    check("asset: meshsettings override the texture", "t_diff2.png" in names and "t_diff.png" not in names, str(sorted(names)))

    for entity in ("override_all_entity", "override_node_entity"):
        collection, _, _ = asset_mod.import_entity(index, entity)
        mesh_obj = next(o for o in collection.objects if o.type == "MESH")
        diffuse = [
            {n.image.name for n in m.node_tree.nodes if n.type == "TEX_IMAGE" and n.image and "t_diff" in n.image.name}
            for m in mesh_obj.data.materials
        ]
        check(f"asset: {entity} retextures every sub-mesh", diffuse == [{"t_diff2.png"}, {"t_diff2.png"}], str(diffuse))

    overrides = [(None, None, {"shader": "A"}), ("a", None, {"diff": "1"}), ("a", 1, {"diff": "2"}), ("b", 0, {"diff": "3"})]
    wanted = [("x|y|a", 0, {"shader": "A", "diff": "1"}), ("a", 1, {"shader": "A", "diff": "2"}),
              ("b", 1, {"shader": "A"}), ("b", 0, {"shader": "A", "diff": "3"}), ("c", 0, {"shader": "A"})]
    got = [bie.mesh_settings_for(overrides, tag, i) for tag, i, _ in wanted]
    check("asset: meshsettings match by name, index, a name's last part and wildcards", got == [w for _, _, w in wanted], str(got))

    result = bpy.ops.io_pdx_mesh.import_asset_run("EXEC_DEFAULT", filepath=asset, entity="top_entity", game_dir="", extra_dirs="")
    check("asset: import operator", result == {"FINISHED"} and len(bpy.data.objects) > before)
    before = len(bpy.data.objects)
    result = bpy.ops.io_pdx_mesh.import_asset_options("EXEC_DEFAULT", filepath=asset, entity="leaf_entity", game_dir="", extra_dirs="")
    check("asset: options operator imports the chosen entity", result == {"FINISHED"} and len(bpy.data.objects) > before)
    ui = importlib.import_module(MODULE + ".pdx_blender.blender_ui")
    ui.get_asset_index(asset, "", "")
    check("asset: entity search filters by text", ui.search_entities(None, None, "TOP") == ["top_entity"], str(ui.search_entities(None, None, "TOP")))
    everything = ui.search_entities(None, None, "top_entity")
    check("asset: entity search lists the whole file once a name is entered, roots first",
          everything[: len(ROOT_ENTITIES)] == ROOT_ENTITIES and "leaf_entity" in everything
          and "elsewhere_entity" not in everything and len(everything) == len(set(everything)), str(everything))
    check("asset: entity search also finds entities from other files", ui.search_entities(None, None, "elsewhere") == ["elsewhere_entity"], str(ui.search_entities(None, None, "elsewhere")))
    check("asset: entity search with no text lists the file", ui.search_entities(None, None, "") == everything)
    check("asset: large import threshold", ui.LARGE_IMPORT > 10, str(ui.LARGE_IMPORT))
    check("asset: operator remembers the file", pdx.IO_PDX_SETTINGS.last_import_asset == asset)
    try:
        bpy.ops.io_pdx_mesh.import_asset_run("EXEC_DEFAULT", filepath=asset, entity="nothing_here", game_dir="", extra_dirs="")
        check("asset: unknown entity fails", False, "no error raised")
    except RuntimeError:
        check("asset: unknown entity fails", True)


def real_asset_import(game, mod=None):
    import time

    asset_mod = importlib.import_module(MODULE + ".pdx_blender.blender_asset")
    index_mod = importlib.import_module(MODULE + ".pdx_asset")

    def run(label, roots, entity, minimum, rigs_wanted=0):
        clear_scene()
        started = time.time()
        index = index_mod.AssetIndex(roots)
        indexed = time.time() - started
        started = time.time()
        collection, _, report = asset_mod.import_entity(index, entity)
        rigs = [o for o in collection.objects if o.type == "ARMATURE"]
        print(f"{label}: index {indexed:.1f}s, import {time.time() - started:.1f}s, {report.lines()}, {len(rigs)} rigs")
        check(f"real asset: {label} places the tree", report.instances >= minimum, str(report.lines()))
        if rigs_wanted:
            check(f"real asset: {label} has a rig for every skeleton", len(rigs) >= rigs_wanted, str(len(rigs)))
        return report

    report = run("dyson gun", [game], "dyson_gun_01_stage_1_entity", 2, rigs_wanted=2)
    check("real asset: dyson gun mesh parts", report.meshes >= 2, str(report.lines()))
    clear_scene()
    started = time.time()
    collection, _, animated = asset_mod.import_entity(index_mod.AssetIndex([game]), "dyson_gun_01_stage_1_entity", animations="default")
    playing = [r for r in collection.objects if r.type == "ARMATURE" and r.animation_data and r.animation_data.action]
    check("real asset: the dyson gun's parts play their idle animations", len(playing) >= 2 and animated.animations >= 2, f"{len(playing)} armatures, {animated.lines()}, {time.time() - started:.1f}s")
    for entity in (
        "arc_crucible_stage_4_entity",
        "neural_calculator_phase_3_entity",
        "dyson_swarm_phase_3_entity",
    ):
        report = run(entity, [game], entity, 1)
        check(f"real asset: {entity} has nothing unresolved", not report.unresolved, str(report.unresolved))
    if mod:
        run("quasi stellar obliterator", [mod, game], "quasistellar_obliterator_full_entity", 50)
        yard = index_mod.AssetIndex([mod, game]).entity("giga_shipyard_full_entity")
        yard_ok = yard is not None and len(yard.attach) >= 6
        if not yard_ok:
            print("SKIP real yard checks: giga_shipyard_full_entity in the mod is cut down (fewer than six parts attached)")
        if yard_ok:
            for flag in (False, True):
                collection, _, _ = asset_mod.import_entity(index_mod.AssetIndex([mod, game]), "giga_shipyard_full_entity", textures=False, collision=flag)
                shaders = {m.get("shader") for o in collection.all_objects if o.type == "MESH" for m in o.data.materials}
                check(f"real asset: the assembly yard {'includes' if flag else 'leaves out'} collision meshes", ("Collision" in shaders) is flag, str(sorted(s for s in shaders if s))[:200])
        export_mod = importlib.import_module(MODULE + ".pdx_blender.blender_asset_export")
        for roots, entity in (([game], "dyson_gun_01_stage_1_entity"), ([mod, game], "quasistellar_obliterator_full_entity")):
            clear_scene()
            index = index_mod.AssetIndex(roots)
            collection, root, _ = asset_mod.import_entity(index, entity, textures=False)
            plan = export_mod.plan_update(root, index)
            check(f"real asset: {entity} saves nothing when left alone", plan.checked > 0 and not plan.changes and not plan.problems, str(plan.lines())[:300])
        import shutil
        import tempfile
        from pathlib import Path

        scratch = Path(tempfile.mkdtemp(prefix="pdx_blender_real_"))
        locators = sorted((o for o in collection.all_objects if o.get("pdx_locator")), key=lambda o: o.name)
        for obj in locators[:3]:
            obj.location.x += 3.0
        plan = export_mod.plan_update(root, index)
        check("real asset: moved locators of a real entity are planned", plan.changes, str(plan.lines())[:300])
        originals = {}
        for change in plan.changes:
            copy = scratch / change.path.name
            originals[copy] = change.path.read_text(encoding="utf-8-sig", errors="replace")
            shutil.copyfile(change.path, copy)
            change.path = copy
        problems = export_mod.apply_update(plan, backup=False)
        edited = sum(sum(1 for x, y in zip(originals[p].splitlines(), p.read_text(encoding="utf-8-sig", errors="replace").splitlines()) if x != y) for p in originals)
        check("real asset: each moved locator changes a line of the real file and nothing else", not problems and 0 < edited <= len(plan.changes), f"{problems} {edited} lines, {len(plan.changes)} changes")
        shutil.rmtree(scratch, ignore_errors=True)

        if yard_ok:
            clear_scene()
            index = index_mod.AssetIndex([mod, game])
            collection, root, _ = asset_mod.import_entity(index, "giga_shipyard_full_entity", textures=False)
            for obj in collection.all_objects:
                if "pdx_locator" in obj:
                    del obj["pdx_locator"]
            parts = sorted((o for o in root.children_recursive if o.get("pdx_entity") and o.parent is not None and o.parent.parent is root and o.parent.name.startswith("loc101")), key=lambda o: o.name)
            for number, part in enumerate(parts):
                part.location += Vector((7.0 * number, -5.0 * number, 3.0))
                part.rotation_euler.z += 0.2 * number
            bpy.context.view_layer.update()
            before = [world_key(o) for o in root.children_recursive if o.get("pdx_entity")] + [world_key(root)]
            plan = export_mod.plan_update(root, index, protected=[game])
            check("real asset: parts moved off the yard's shared locator are planned as new locators", len(parts) >= 5 and plan.count("new_locator") >= len(parts) - 1 and not plan.problems, str(plan.lines())[:300])
            overlay = Path(tempfile.mkdtemp(prefix="pdx_blender_overlay_"))
            for change in plan.changes:
                relative = change.path.relative_to(mod)
                target = overlay / relative
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(change.path, target)
                change.path = target
            problems = export_mod.apply_update(plan, backup=False)
            clear_scene()
            index = index_mod.AssetIndex([overlay, mod, game])
            collection, root2, _ = asset_mod.import_entity(index, "giga_shipyard_full_entity", textures=False)
            bpy.context.view_layer.update()
            after = [world_key(o) for o in root2.children_recursive if o.get("pdx_entity")] + [world_key(root2)]
            check("real asset: the saved yard imports where its parts were left", not problems and same_worlds(before, after, 0.05), f"{problems} {len(before)} vs {len(after)}")
            shutil.rmtree(overlay, ignore_errors=True)

            clear_scene()
            index = index_mod.AssetIndex([mod, game])
            collection, root, _ = asset_mod.import_entity(index, "giga_shipyard_full_entity", textures=False)
            models = [o for o in root.children_recursive if o.type == "MESH" and o.parent is not None and o.parent.get("pdx_entity") == "hay_outer_ring_piece_entity"]
            for model in models:
                model.location += Vector((0.0, 0.0, 8.0))
            bpy.context.view_layer.update()
            plan = export_mod.plan_update(models[0], index, protected=[game])
            check("real asset: moving the models of the yard's parts is read as moving the parts", len(models) > 50 and plan.changes and not any("disagree" in p for p in plan.problems), str(plan.lines())[:300])
            import random

            random.seed(7)
            clear_scene()
            index = index_mod.AssetIndex([mod, game])
            collection, root, _ = asset_mod.import_entity(index, "giga_shipyard_full_entity", textures=False)
            for obj in collection.all_objects:
                if "pdx_locator" in obj:
                    del obj["pdx_locator"]
            models = [o for o in root.children_recursive if o.type == "MESH" and o.parent is not None and o.parent.get("pdx_entity")
                      and not any(c.get("pdx_entity") for c in o.parent.children_recursive)]
            roots = [o for o in root.children_recursive if o.get("pdx_entity")]

            def effective(obj):
                key = world_key(obj)
                shift, together = export_mod.content_shift(obj)
                return (key[0], key[1], key[2] @ (shift if together else Matrix()))

            for model in random.sample(models, 40):
                model.location += Vector((random.uniform(-30, 30), random.uniform(-30, 30), random.uniform(-30, 30)))
                model.rotation_euler.z += random.uniform(-0.5, 0.5)
            for part in random.sample(roots, 25):
                part.location += Vector((random.uniform(-40, 40), random.uniform(-40, 40), random.uniform(-40, 40)))
            bpy.context.view_layer.update()
            before = [effective(o) for o in root.children_recursive if o.get("pdx_entity")] + [effective(root)]
            started = time.time()
            plan = export_mod.plan_update(models[0], index, protected=[game])
            planned = time.time() - started
            check("real asset: a butchered yard is planned as copies, not refused", plan.count("variant") > 5 and not any("disagree" in p for p in plan.problems), f"{planned:.1f}s {str(plan.lines())[:400]}")
            overlay = Path(tempfile.mkdtemp(prefix="pdx_blender_overlay_"))
            for change in plan.changes:
                target = overlay / change.path.relative_to(mod)
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(change.path, target)
                change.path = target
            problems = export_mod.apply_update(plan, backup=False)
            export_mod.commit_scene(plan)
            index = index_mod.AssetIndex([overlay, mod, game])
            in_session = export_mod.plan_update(root, index, protected=[game])
            check("real asset: the butchered scene needs nothing more saved once it is committed", not in_session.changes and not in_session.problems, str(in_session.lines())[:400])
            committed = [effective(o) for o in root.children_recursive if o.get("pdx_entity")] + [effective(root)]
            clear_scene()
            collection, root2, _ = asset_mod.import_entity(index, "giga_shipyard_full_entity", textures=False)
            bpy.context.view_layer.update()
            after = [world_key(o) for o in root2.children_recursive if o.get("pdx_entity")] + [world_key(root2)]
            check("real asset: and the committed scene matches an import of what was saved", same_worlds(committed, after, 0.05))
            check("real asset: the butchered yard imports back as it was left", not problems and same_worlds(before, after, 0.05), f"{problems} {len(before)} vs {len(after)}, {len(plan.changes)} changes, {plan.count('variant')} copies, wrong: {len(unmatched_worlds(before, after, 0.05))} {sorted(__import__('collections').Counter(k[0] for k in unmatched_worlds(before, after, 0.05)).items())} problems: {plan.problems[:3]}")
            again = export_mod.plan_update(root2, index, protected=[game])
            check("real asset: the saved yard has nothing more to save", not again.changes and not again.problems, str(again.lines())[:400])
            shutil.rmtree(overlay, ignore_errors=True)
        report = run("matrioshka brain", [mod, game], "matrioshka_brain_05_entity", 5)
        check("real asset: matrioshka brain has nothing unresolved", not report.unresolved, str(report.unresolved))
        index = index_mod.AssetIndex([mod, game])
        strut = next(n for n in index.entity_blocks if index.entity(n).pdxmesh == "birch_sphere_strut_72_mesh")
        collection, _, _ = asset_mod.import_entity(index, strut)
        alpha = {}
        for obj in collection.objects:
            for material in (obj.data.materials if obj.type == "MESH" else []):
                alpha[material.get("shader")] = material.node_tree.nodes["Principled BSDF"].inputs["Alpha"].is_linked
        check("real asset: birch struts use the diffuse alpha only where the shader says so",
              alpha.get("PdxMeshShipDiffuseEmissiveAlpha") is True and alpha.get("PdxMeshShipDiffuseEmissive") is False, str(alpha))
        collection, _, _ = asset_mod.import_entity(index, "matrioshka_brain_p5_energy_entity")
        additive = [
            next(n for n in m.node_tree.nodes if n.type == "OUTPUT_MATERIAL").inputs["Surface"].links[0].from_node.type
            for o in collection.objects if o.type == "MESH" for m in o.data.materials if m.get("shader") == "OmniMeshMegaAlpha"
        ]
        check("real asset: the energy sphere is additive", additive and set(additive) == {"ADD_SHADER"}, str(additive))
        asset_mod.import_entity(index, "toxoid_01_matrioshka_brain_05_entity")
        used = {n.image.name for m in bpy.data.materials for n in m.node_tree.nodes if n.type == "TEX_IMAGE" and n.image}
        check("real asset: toxoid shipset textures are applied",
              {"toxoid_01_orbital_ring_diffuse.dds", "toxoid_01_hyper_relay_diffuse.dds", "toxoid_01_mega_shipyard_diffuse.dds"} <= used,
              str(sorted(u for u in used if "toxoid" in u)))
        collection, _, _ = asset_mod.import_entity(index, "behemoth_full_entity", textures=False)
        bpy.context.view_layer.update()
        corners = [o.matrix_world @ Vector(c) for o in collection.objects if o.type == "MESH" for c in o.bound_box]
        width, depth, height = (max(p[i] for p in corners) - min(p[i] for p in corners) for i in range(3))
        check("real asset: four ringworld quarters close into a flat circle",
              abs(width - depth) < 0.01 * width and height < 0.05 * width, f"{width:.0f} x {depth:.0f} x {height:.0f}")


def real_animation(game):
    import time

    from mathutils import Matrix

    models = os.path.join(game, "gfx", "models", "ships", "megastructures", "dyson_gun")
    mesh = os.path.join(models, "dyson_gun_01_stage_1.mesh")
    anim = os.path.join(models, "dyson_gun_01_stage_1_working_looping.anim")
    bie = importlib.import_module(MODULE + ".pdx_blender.blender_import_export")
    pdx_data = importlib.import_module(MODULE + ".pdx_data")

    clear_scene()
    import_mesh(mesh)
    rig = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    started = time.time()
    result = bie.import_animfile(anim, rig=rig)
    elapsed = time.time() - started
    check("real animation: imports quickly", elapsed < 3, f"{elapsed:.2f}s for {result['frames']} frames")
    check("real animation: ten channels per bone", sum(1 for _ in bie_fcurves(result["action"])) == 10 * len(rig.data.bones))

    doc = pdx_data.read_meshfile(anim)
    info, samples = doc.find("info"), doc.find("samples")
    names = [bie.clean_imported_name(b.tag) for b in info]
    pad = 4 - len(info[0].attrib["s"])
    sampled = {n: {a: [] for a in b.attrib["sa"][0]} for n, b in zip(names, info)}
    cursor, width = {"s": 0, "q": 0, "t": 0}, {"s": len(info[0].attrib["s"]), "q": 4, "t": 3}
    for _ in range(result["frames"]):
        for n in names:
            for a in sampled[n]:
                sampled[n][a].append(samples.attrib[a][cursor[a] : cursor[a] + width[a]])
                cursor[a] += width[a]

    def offset(bone, n, k):
        t = sampled[n]["t"][k] if "t" in sampled[n] else bone.attrib["t"]
        q = sampled[n]["q"][k] if "q" in sampled[n] else bone.attrib["q"]
        sc = sampled[n]["s"][k] if "s" in sampled[n] else bone.attrib["s"]
        sc = list(sc) * pad if len(sc) == 1 else list(sc)
        rotation = Quaternion((q[3], q[0], q[1], q[2])).to_matrix().to_4x4()
        return bie.swap_coord_space(Matrix.Translation(t) @ rotation @ Matrix.Diagonal(sc).to_4x4())

    worst = 0.0
    scene = bpy.context.scene
    for k in (0, 1, result["frames"] // 2, result["frames"] - 1):
        scene.frame_set(k + 1)
        bpy.context.view_layer.update()
        pose = {}
        for n, b in zip(names, info):
            parent = rig.data.bones[n].parent
            pose[n] = (pose[parent.name] if parent else Matrix()) @ offset(b, n, k)
            worst = max(worst, max(abs(x - y) for ra, rb in zip(rig.pose.bones[n].matrix, pose[n]) for x, y in zip(ra, rb)))
    check("real animation: poses match forward kinematics from the file", worst < 1e-3, f"max delta {worst:.1e}")


def bie_fcurves(action):
    return (fc for layer in action.layers for strip in layer.strips for cb in strip.channelbags for fc in cb.fcurves)


def finish():
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n=== {len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for name, _, detail in failed:
        print(f"  FAIL {name}  {detail}")
    sys.stdout.flush()
    sys.exit(1 if failed else 0)


def main():
    args = parse_args()
    os.makedirs(args.out, exist_ok=True)
    print(f"Blender {bpy.app.version_string}, Python {sys.version.split()[0]}")

    with stage("enable add-on"):
        bpy.ops.preferences.addon_enable(module=MODULE)
        check("add-on enabled", MODULE in bpy.context.preferences.addons)
    if not RESULTS or not RESULTS[-1][1]:
        finish()

    pdx = importlib.import_module(MODULE)
    bie = importlib.import_module(MODULE + ".pdx_blender.blender_import_export")
    pdx_data = importlib.import_module(MODULE + ".pdx_data")
    print(f"add-on version {pdx.IO_PDX_INFO['version']}")

    with stage("registration"):
        for op in ("import_mesh", "export_mesh", "import_anim", "export_anim"):
            check(f"operator io_pdx_mesh.{op} registered", hasattr(bpy.ops.io_pdx_mesh, op))
        props = ("io_pdx_material", "io_pdx_group", "io_pdx_export")
        check("scene properties registered", all(hasattr(bpy.types.Scene, p) for p in props))

    with stage("settings"):
        settings_mod = importlib.import_module(MODULE + ".settings")
        live = pdx.IO_PDX_SETTINGS
        folder = os.path.normpath(bpy.utils.extension_path_user(MODULE, path=""))
        check("settings: file is in the extension user folder",
              live.config_path and os.path.normpath(os.path.dirname(live.config_path)) == folder, str(live.config_path))
        live.last_import_mesh = "example.mesh"
        check("settings: saved and reloaded", settings_mod.PDXsettings(live.config_path).last_import_mesh == "example.mesh")
        memory = settings_mod.PDXsettings()
        memory.value = 1
        check("settings: in-memory mode writes nothing", memory.config_path is None and memory.value == 1)

    synthetic = os.path.join(args.out, "synthetic.mesh")
    with stage("synthetic scene export"):
        build_synthetic_scene(bie, args.out)
        orig, _, _ = scene_stats()
        print(f"synthetic scene {orig}")
        check("synthetic export", export_mesh(synthetic) == {"FINISHED"})
        raw = raw_counts(pdx_data, synthetic)
        print(f"synthetic raw file {raw}")
        check("synthetic raw: 2 mesh elements (one per material)", raw["meshes"] == 2, str(raw["meshes"]))
        check("synthetic raw: bones", raw["bones"] == 3, str(raw["bones"]))
        check("synthetic raw: locators", raw["locators"] == 1, str(raw["locators"]))
        want = {("diff", "t_diff.png"), ("spec", "t_spec.png"), ("n", "t_n.png"), ("diff", "t_diff2.png")}
        check("synthetic raw: texture slots / names", raw["textures"] == want, str(sorted(raw["textures"])))

    with stage("synthetic round trip"):
        expect = dict(materials=2, bones=3, vgroups=3, uv_layers=1, locators=1)
        mesh_roundtrip("synthetic", pdx_data, synthetic, args.out, expect=expect)

    for i, real_mesh in enumerate(args.mesh):
        label = f"real{i + 1}"
        with stage(f"real mesh round trip - {label} - {os.path.basename(real_mesh)}"):
            mesh_roundtrip(label, pdx_data, real_mesh, args.out, tol=2e-2)
    if not args.mesh:
        print("\n(no --mesh given: real-file stage skipped; the repo ships no sample .mesh files)")

    with stage("animation round trip"):
        clear_scene()
        import_mesh(synthetic)
        anim_roundtrip(args.out, bie)

    with stage("ui smoke test"):
        ui_smoke(pdx)

    with stage("export options and setup helpers"):
        options_and_helpers(pdx_data, bie, synthetic, args.out)

    with stage("updater"):
        updater_checks(pdx)

    with stage("asset import"):
        asset_import(pdx, bie, pdx_data, args.out)

    if os.environ.get("STELLARIS_DIR"):
        with stage("real animation"):
            real_animation(os.environ["STELLARIS_DIR"])
        with stage("real asset import"):
            real_asset_import(os.environ["STELLARIS_DIR"], os.environ.get("PDX_TEST_MOD"))

    finish()


main()
