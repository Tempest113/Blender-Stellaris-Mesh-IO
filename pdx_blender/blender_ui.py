"""Panels, operators and dialogs. Author: ross-g, .asset parts added for this project."""

import importlib
from pathlib import Path
from textwrap import wrap

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import Menu, Operator, Panel, UIList
from bpy_extras.io_utils import ExportHelper, ImportHelper

from .. import ENGINE_SETTINGS, IO_PDX_INFO, IO_PDX_LOG, IO_PDX_SETTINGS
from ..pdx_asset import AssetIndex, find_game_dir, find_mod_root
from ..pdx_data import PDXData
from ..updater import github

try:
    from . import blender_asset, blender_asset_export, blender_import_export

    importlib.reload(blender_import_export)
    importlib.reload(blender_asset)
    importlib.reload(blender_asset_export)
    from .blender_asset import action_label, actions_for, assign_action, import_entity, rig_of
    from .blender_asset_export import apply_update, commit_scene, plan_update, read_assembly, sanitize, tops_of, write_assembly
    from .blender_import_export import (
        PDX_MESHINDEX,
        PDX_SHADER,
        create_shader,
        export_animfile,
        export_meshfile,
        get_mesh_index,
        import_animfile,
        import_meshfile,
        list_scene_pdx_meshes,
        set_ignore_joints,
        set_local_axis_display,
    )
except Exception as err:
    IO_PDX_LOG.error(err)
    raise


def get_material_list(self, context):
    material_list = [(material, material, material) for material in ENGINE_SETTINGS["stellaris"]["material"]]
    material_list.insert(0, ("__NONE__", "", ""))

    return material_list


def get_scene_material_list(self, context):
    material_list = [(mat.name, mat.name, mat.name) for mat in bpy.data.materials if mat.get(PDX_SHADER, None)]

    return material_list


class IOPDX_OT_popup_message(Operator):
    bl_idname = "io_pdx_mesh.popup_message"
    bl_label = IO_PDX_INFO["name"]
    bl_description = "Popup Message"
    bl_options = {"REGISTER"}
    msg_text: StringProperty(
        default="NOT YET IMPLEMENTED!",
    )
    msg_icon: StringProperty(
        default="ERROR",
    )
    msg_width: IntProperty(
        default=300,
    )

    def execute(self, context):
        return {"FINISHED"}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=self.msg_width)

    def draw(self, context):
        self.layout.operator_context = "INVOKE_DEFAULT"

        txt_lines = []
        for line in self.msg_text.splitlines():
            txt_lines.extend(wrap(line, self.msg_width / 6))
            txt_lines.append("")

        col = self.layout.column(align=True)
        col.label(text=txt_lines[0], icon=self.msg_icon)
        for line in txt_lines[1:]:
            if line:
                col.label(text=line)
            else:
                col.separator()

        col.label(text="")


class material_popup(object):
    bl_options = {"REGISTER"}
    mat_name: StringProperty(
        name="Material Name",
        default="",
    )
    mat_type: EnumProperty(
        name="Material Type",
        items=get_material_list,
    )
    use_custom: BoolProperty(
        name="Custom Type:",
        default=False,
    )
    custom_type: StringProperty(
        name="Shader",
        default="",
    )
    apply_mat: BoolProperty(
        name="Apply Material to Selected?",
        default=False,
    )


class IOPDX_OT_material_create_popup(material_popup, Operator):
    bl_idname = "io_pdx_mesh.material_create_popup"
    bl_description = bl_label = "Create a PDX Material"

    def check(self, context):
        return True

    def execute(self, context):
        mat_name = self.mat_name
        mat_type = self.mat_type
        if self.use_custom or mat_type == "__NONE__":
            mat_type = self.custom_type
        mat_pdx = type("Material", (PDXData, object), {"shader": [mat_type]})
        shader = create_shader(mat_pdx, mat_name, None, template_only=True)
        IO_PDX_LOG.info(f"Created material - {mat_name} ({mat_type})")
        if self.apply_mat:
            selected_objs = [obj for obj in context.selected_objects if isinstance(obj.data, bpy.types.Mesh)]
            for obj in selected_objs:
                obj.data.materials.append(shader)
                IO_PDX_LOG.info(f"Applied material '{shader.name}' to object - {obj.name}")
        return {"FINISHED"}

    def invoke(self, context, event):
        self.mat_name = ""
        self.mat_type = "__NONE__"
        self.use_custom = False
        self.custom_type = ""
        return context.window_manager.invoke_props_dialog(self, width=350)

    def draw(self, context):
        box = self.layout.box()
        box.prop(self, "mat_name")
        mat_type = box.row()
        mat_type.prop(self, "mat_type")
        split = box.split(factor=0.3)
        col1, col2 = split.column(), split.column()
        col1.prop(self, "use_custom")
        col2.prop(self, "custom_type", text="")
        col2.enabled = False
        if self.use_custom:
            mat_type.enabled = False
            col2.enabled = True
        self.layout.separator()
        apply = self.layout.row()
        apply.prop(self, "apply_mat")
        self.layout.separator()


class IOPDX_OT_material_edit_popup(material_popup, Operator):
    bl_idname = "io_pdx_mesh.material_edit_popup"
    bl_description = bl_label = "Edit a PDX Material"

    def mat_select(self, context):
        mat = bpy.data.materials[self.scene_mats]
        curr_mat = context.scene.io_pdx_material
        curr_mat.mat_name = mat.name
        curr_mat.mat_type = mat[PDX_SHADER]

    scene_mats: EnumProperty(
        name="Selected Material",
        items=get_scene_material_list,
        update=mat_select,
    )

    def check(self, context):
        return True

    def execute(self, context):
        mat = bpy.data.materials[self.scene_mats]
        curr_mat = context.scene.io_pdx_material
        mat.name = curr_mat.mat_name
        mat[PDX_SHADER] = curr_mat.mat_type
        IO_PDX_LOG.info(f"Edited material - {curr_mat.mat_name} ({curr_mat.mat_type})")
        return {"FINISHED"}

    def invoke(self, context, event):
        pdx_scene_materials = get_scene_material_list(self, context)
        if pdx_scene_materials:
            if self.scene_mats in bpy.data.materials:
                self.mat_select(context)
                mat = bpy.data.materials[self.scene_mats]
                self.mat_name = mat.name
                self.custom_type = mat[PDX_SHADER]
                return context.window_manager.invoke_props_dialog(self, width=350)
            else:
                return {"CANCELLED"}
        else:
            bpy.ops.io_pdx_mesh.popup_message("INVOKE_DEFAULT", msg_text="NO PDX MATERIALS FOUND IN THE SCENE!")
            return {"CANCELLED"}

    def draw(self, context):
        curr_mat = context.scene.io_pdx_material

        self.layout.prop(self, "scene_mats")
        self.layout.separator()

        box = self.layout.box()
        box.prop(curr_mat, "mat_name")
        box.prop(curr_mat, "mat_type")
        self.layout.separator()


class IOPDX_UL_mesh_index_list(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.prop(item, "name", text="", emboss=False)


class IOPDX_OT_mesh_index_actions(Operator):
    bl_idname = "io_pdx_mesh.mesh_index_actions"
    bl_description = bl_label = "Mesh Index List Actions"
    bl_options = {"REGISTER"}
    action: EnumProperty(
        items=(("UP", "Up", ""), ("DOWN", "Down", "")),
    )

    @classmethod
    def poll(cls, context):
        return context.scene.io_pdx_group

    def move_index(self):
        list_index = bpy.context.scene.io_pdx_group.idx
        list_length = len(bpy.context.scene.io_pdx_group.coll) - 1

        new_index = list_index + (-1 if self.action == "UP" else 1)
        bpy.context.scene.io_pdx_group.idx = max(0, min(new_index, list_length))

    def execute(self, context):
        collection = context.scene.io_pdx_group.coll
        index = context.scene.io_pdx_group.idx
        neighbor = index + (-1 if self.action == "UP" else 1)
        collection.move(neighbor, index)
        self.move_index()

        return {"FINISHED"}


class IOPDX_OT_mesh_index_popup(Operator):
    bl_idname = "io_pdx_mesh.mesh_index_popup"
    bl_description = bl_label = "Set Mesh Index on PDX Meshes"
    bl_options = {"REGISTER"}

    def check(self, context):
        return True

    def execute(self, context):
        for i, item in enumerate(context.scene.io_pdx_group.coll):
            item.ref.data[PDX_MESHINDEX] = i
        return {"FINISHED"}

    def invoke(self, context, event):
        obj_group = context.scene.io_pdx_group

        obj_group.coll.clear()
        pdx_scenemeshes = list_scene_pdx_meshes()
        pdx_scenemeshes.sort(key=lambda obj: get_mesh_index(obj.data))

        for obj in pdx_scenemeshes:
            item = obj_group.coll.add()
            item.name = obj.name
            item.ref = obj
        return context.window_manager.invoke_props_dialog(self, width=200)

    def draw(self, context):
        obj_group = context.scene.io_pdx_group
        row = self.layout.row()
        row.template_list("IOPDX_UL_mesh_index_list", "", obj_group, "coll", obj_group, "idx", rows=8)

        col = row.column(align=True)
        col.operator("io_pdx_mesh.mesh_index_actions", icon="TRIA_UP", text="").action = "UP"
        col.operator("io_pdx_mesh.mesh_index_actions", icon="TRIA_DOWN", text="").action = "DOWN"
        self.layout.separator()


class IOPDX_OT_import_mesh(Operator, ImportHelper):
    bl_idname = "io_pdx_mesh.import_mesh"
    bl_description = bl_label = "Import PDX Mesh"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".mesh"
    filter_glob: StringProperty(
        default="*.mesh",
        options={"HIDDEN"},
        maxlen=255,
    )
    filepath: StringProperty(
        name="Import File Path",
        maxlen=1024,
    )
    chk_mesh: BoolProperty(
        name="Import Mesh",
        description="Import mesh",
        default=True,
    )
    chk_skel: BoolProperty(
        name="Import Skeleton",
        description="Import skeleton",
        default=True,
    )
    chk_locs: BoolProperty(
        name="Import Locators",
        description="Import locators",
        default=True,
    )
    chk_joinmats: BoolProperty(
        name="Join Materials",
        description="Join materials",
        default=True,
    )
    chk_bonespace: BoolProperty(
        name="Convert Bone Orientation - WARNING",
        description="Convert bone orientation - WARNING: this re-orients bones authored at source and will BREAK ALL "
                    "EXISTING ANIMATIONS. Only use this option if you are going to fully re-animate the model.",
        default=False,
    )

    def draw(self, context):
        box = self.layout.box()
        box.label(text="Settings:", icon="IMPORT")
        box.prop(self, "chk_mesh")
        if self.chk_mesh:
            mesh_settings = box.box()
            split = mesh_settings.split(factor=0.1)
            _, col = split.column(), split.column()
            col.prop(self, "chk_joinmats")
        box.prop(self, "chk_skel")
        box.prop(self, "chk_locs")

    def execute(self, context):
        try:
            import_meshfile(
                self.filepath,
                imp_mesh=self.chk_mesh,
                imp_skel=self.chk_skel,
                imp_locs=self.chk_locs,
                join_materials=self.chk_joinmats,
                bonespace=self.chk_bonespace,
            )
            self.report({"INFO"}, f"[io_pdx_mesh] Mesh import finished '{self.filepath}'")
            IO_PDX_SETTINGS.last_import_mesh = self.filepath

        except Exception as err:
            IO_PDX_LOG.warning(f"FAILED to import '{self.filepath}'")
            IO_PDX_LOG.error(err)
            self.report({"WARNING"}, "Mesh import failed!")
            self.report({"ERROR"}, str(err))
            raise

        return {"FINISHED"}

    def invoke(self, context, event):
        self.filepath = IO_PDX_SETTINGS.last_import_mesh or ""
        context.window_manager.fileselect_add(self)

        return {"RUNNING_MODAL"}


class IOPDX_OT_import_anim(Operator, ImportHelper):
    bl_idname = "io_pdx_mesh.import_anim"
    bl_description = bl_label = "Import PDX Animation"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".anim"
    filter_glob: StringProperty(
        default="*.anim",
        options={"HIDDEN"},
        maxlen=255,
    )
    filepath: StringProperty(
        name="Import File Path",
        maxlen=1024,
    )
    int_start: IntProperty(
        name="Start Frame",
        description="Start frame",
        default=1,
    )

    def draw(self, context):
        box = self.layout.box()
        box.label(text="Settings:", icon="IMPORT")
        box.prop(self, "int_start")

    def execute(self, context):
        try:
            import_animfile(self.filepath, frame_start=self.int_start)
            self.report({"INFO"}, f"[io_pdx_mesh] Animation import finished {self.filepath}")
            IO_PDX_SETTINGS.last_import_anim = self.filepath

        except Exception as err:
            IO_PDX_LOG.warning(f"FAILED to import '{self.filepath}'")
            IO_PDX_LOG.error(err)
            self.report({"WARNING"}, "Animation import failed!")
            self.report({"ERROR"}, str(err))
            raise

        return {"FINISHED"}

    def invoke(self, context, event):
        self.filepath = IO_PDX_SETTINGS.last_import_anim or ""
        context.window_manager.fileselect_add(self)

        return {"RUNNING_MODAL"}


LARGE_IMPORT = 250
ASSET_INDEX = {"key": None, "index": None, "filepath": None}


def get_asset_index(filepath, game_dir, extra_dirs, rebuild=False):
    folders = [find_mod_root(filepath)] + [p.strip() for p in extra_dirs.split(";")] + [game_dir]
    roots = []
    for folder in folders:
        if folder and Path(folder).is_dir() and Path(folder).resolve() not in roots:
            roots.append(Path(folder).resolve())
    if rebuild or ASSET_INDEX["key"] != (filepath, tuple(roots)):
        ASSET_INDEX.update(key=(filepath, tuple(roots)), index=AssetIndex(roots), filepath=filepath)
    return ASSET_INDEX["index"]


def search_entities(self, context, edit_text):
    index = ASSET_INDEX["index"]
    if not index:
        return []
    filepath = Path(ASSET_INDEX["filepath"])
    roots = index.root_entities(filepath)
    here = roots + [n for n in index.entities_by_file.get(filepath.resolve(), []) if n and n not in roots]
    if not edit_text or edit_text in index.entity_blocks:
        return here
    text = edit_text.lower()
    found = [name for name in here if text in name.lower()]
    if len(text) >= 3:
        found += [name for name in index.entity_blocks if text in name.lower() and name not in here][:200]
    return found


class IOPDX_OT_import_asset(Operator, ImportHelper):
    bl_idname = "io_pdx_mesh.import_asset"
    bl_description = bl_label = "Import PDX Asset"
    bl_options = {"REGISTER"}

    filename_ext = ".asset"
    filter_glob: StringProperty(
        default="*.asset",
        options={"HIDDEN"},
        maxlen=255,
    )
    filepath: StringProperty(
        name="Import File Path",
        maxlen=1024,
    )

    def execute(self, context):
        bpy.ops.io_pdx_mesh.import_asset_options("INVOKE_DEFAULT", filepath=self.filepath)
        return {"FINISHED"}

    def invoke(self, context, event):
        self.filepath = IO_PDX_SETTINGS.last_import_asset or ""
        context.window_manager.fileselect_add(self)

        return {"RUNNING_MODAL"}


class IOPDX_OT_import_asset_options(Operator):
    bl_idname = "io_pdx_mesh.import_asset_options"
    bl_description = bl_label = "Import PDX Asset"
    bl_options = {"INTERNAL"}

    filepath: StringProperty(
        options={"HIDDEN"},
    )
    entity: StringProperty(
        name="Entity",
        description="Entity to import with everything attached to it",
        search=search_entities,
        search_options={"SUGGESTION"},
    )
    chk_textures: BoolProperty(
        name="Load Textures",
        description="Load textures, missing ones are replaced by a placeholder",
        default=True,
    )
    chk_collision: BoolProperty(
        name="Include Collision Meshes",
        description="Import the meshes the game uses for collisions, as wireframes in a collection of their own that starts hidden",
        default=False,
    )
    chk_animations: BoolProperty(
        name="Include Animations",
        description="Import the animation of each entity's default state and play it. Some models, like the matter decompressor, only face the way they do in the game with their animation",
        default=True,
    )
    chk_all_states: BoolProperty(
        name="All States",
        description="Also import the animation of every other state, as actions to pick from the Action editor",
        default=False,
    )
    game_dir: StringProperty(
        name="Game Folder",
        description="Game folder, searched for anything the mod does not define itself",
        subtype="DIR_PATH",
    )
    extra_dirs: StringProperty(
        name="Extra Folders",
        description="Other mod folders to search, separated by ;",
    )

    def draw(self, context):
        index = ASSET_INDEX["index"]
        layout = self.layout
        layout.prop(self, "entity")
        if index and index.entity(self.entity):
            instances, meshes, _ = index.tree_size(self.entity)
            layout.label(text=f"{instances} entities, {meshes} meshes")
        elif self.entity:
            layout.label(text="No entity with that name", icon="ERROR")
        layout.prop(self, "chk_textures")
        layout.prop(self, "chk_collision")
        layout.prop(self, "chk_animations")
        if self.chk_animations:
            layout.prop(self, "chk_all_states")
        else:
            layout.label(text="Some models may be placed incorrectly when imported without their animations", icon="ERROR")
        layout.prop(self, "game_dir")
        layout.prop(self, "extra_dirs")

    def invoke(self, context, event):
        self.game_dir = IO_PDX_SETTINGS.game_directory or str(find_game_dir() or "")
        self.extra_dirs = IO_PDX_SETTINGS.extra_asset_directories or ""
        index = get_asset_index(self.filepath, self.game_dir, self.extra_dirs, rebuild=True)
        roots = index.root_entities(self.filepath)
        self.entity = roots[0] if roots else ""

        return context.window_manager.invoke_props_dialog(self, width=520)

    def execute(self, context):
        options = dict(
            filepath=self.filepath,
            entity=self.entity,
            chk_textures=self.chk_textures,
            chk_collision=self.chk_collision,
            chk_animations=self.chk_animations,
            chk_all_states=self.chk_all_states,
            game_dir=self.game_dir,
            extra_dirs=self.extra_dirs,
        )
        index = get_asset_index(self.filepath, self.game_dir, self.extra_dirs)
        if not index.entity(self.entity):
            self.report({"ERROR"}, f"Entity '{self.entity}' is not defined in the game or mod folders.")
            return {"CANCELLED"}

        mode = "INVOKE_DEFAULT" if index.tree_size(self.entity)[0] > LARGE_IMPORT else "EXEC_DEFAULT"
        bpy.ops.io_pdx_mesh.import_asset_run(mode, **options)
        return {"FINISHED"}


class IOPDX_OT_import_asset_run(Operator):
    bl_idname = "io_pdx_mesh.import_asset_run"
    bl_description = bl_label = "Import PDX Asset"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    filepath: StringProperty(
        options={"HIDDEN"},
    )
    entity: StringProperty(
        options={"HIDDEN"},
    )
    chk_textures: BoolProperty(
        default=True,
        options={"HIDDEN"},
    )
    chk_collision: BoolProperty(
        options={"HIDDEN"},
    )
    chk_animations: BoolProperty(
        options={"HIDDEN"},
    )
    chk_all_states: BoolProperty(
        options={"HIDDEN"},
    )
    game_dir: StringProperty(
        options={"HIDDEN"},
    )
    extra_dirs: StringProperty(
        options={"HIDDEN"},
    )

    def invoke(self, context, event):
        index = get_asset_index(self.filepath, self.game_dir, self.extra_dirs)
        instances, meshes, _ = index.tree_size(self.entity)
        return context.window_manager.invoke_confirm(
            self,
            event,
            title="Large import",
            message=f"'{self.entity}' has {instances} entities and {meshes} meshes. This can take a while. Import it?",
            confirm_text="Yes, Import",
        )

    def execute(self, context):
        try:
            index = get_asset_index(self.filepath, self.game_dir, self.extra_dirs)
            animations = ("all" if self.chk_all_states else "default") if self.chk_animations else None
            _, _, report = import_entity(
                index, self.entity, textures=self.chk_textures, animations=animations, collision=self.chk_collision
            )
            for line in report.lines():
                IO_PDX_LOG.info(line)
            self.report({"WARNING" if report.unresolved else "INFO"}, "[io_pdx_mesh] " + "; ".join(report.lines()))
            IO_PDX_SETTINGS.last_import_asset = self.filepath
            IO_PDX_SETTINGS.game_directory = self.game_dir
            IO_PDX_SETTINGS.extra_asset_directories = self.extra_dirs

        except Exception as err:
            IO_PDX_LOG.warning(f"FAILED to import '{self.entity}' from '{self.filepath}'")
            IO_PDX_LOG.error(err)
            self.report({"WARNING"}, "Asset import failed!")
            self.report({"ERROR"}, str(err))
            raise

        return {"FINISHED"}


class IOPDX_OT_export_mesh(Operator, ExportHelper):
    bl_idname = "io_pdx_mesh.export_mesh"
    bl_description = bl_label = "Export PDX Mesh"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".mesh"
    filter_glob: StringProperty(
        default="*.mesh",
        options={"HIDDEN"},
        maxlen=255,
    )
    filepath: StringProperty(
        name="Export File Path",
        maxlen=1024,
    )
    chk_mesh: BoolProperty(
        name="Meshes",
        description="Export meshes",
        default=True,
    )
    chk_mesh_blendshape: BoolProperty(
        name="As Blendshape",
        description="Export meshes as blendshapes",
        default=False,
    )
    chk_skel: BoolProperty(
        name="Skeleton",
        description="Export related armatures",
        default=True,
    )
    chk_locs: BoolProperty(
        name="Locators",
        description="Export empties data",
        default=True,
    )
    chk_selected: BoolProperty(
        name="Selected Only",
        description="Filter export by selection",
        default=False,
    )
    chk_debug: BoolProperty(
        name="[Debug Options]",
        description="Non-standard options",
        default=False,
    )
    chk_split_vtx: BoolProperty(
        name="Split All Vertices",
        description="Splits all vertices (per triangle) during export",
        default=False,
    )
    ddl_sort_vtx: EnumProperty(
        name="Sort Vertices",
        description="Sort all vertex data by id during export",
        items=(
            ("+", "Incr", "Ascending id sort"),
            ("~", "Native", "Blender native order"),
            ("-", "Decr", "Descending id sort")
        ),
        default="+",
    )
    chk_plain_txt: BoolProperty(
        name="Also Export Plain Text",
        description="Exports a plain text file along with binary",
        default=False,
    )

    def draw(self, context):
        box = self.layout.box()
        box.label(text="Settings:", icon="EXPORT")
        box.prop(self, "chk_mesh")
        if self.chk_mesh:
            mesh_settings = box.box()
            split = mesh_settings.split(factor=0.1)
            _, col = split.column(), split.column()
            col.prop(self, "chk_mesh_blendshape")
        box.prop(self, "chk_skel")
        box.prop(self, "chk_locs")
        box.prop(self, "chk_selected")
        box.prop(self, "chk_debug")
        if self.chk_debug:
            debug_settings = box.box()
            split = debug_settings.split(factor=0.1)
            _, col = split.column(), split.column()
            col.alignment = "RIGHT"
            col.prop(self, "chk_split_vtx")
            col.prop(self, "ddl_sort_vtx")
            col.prop(self, "chk_plain_txt")

    def execute(self, context):
        try:
            export_meshfile(
                self.filepath,
                exp_mesh=self.chk_mesh,
                exp_skel=self.chk_skel,
                exp_locs=self.chk_locs,
                exp_selected=self.chk_selected,
                as_blendshape=self.chk_mesh_blendshape,
                debug_mode=self.chk_debug,
                split_verts=self.chk_split_vtx,
                sort_verts=self.ddl_sort_vtx,
                plain_txt=self.chk_plain_txt,
            )
            self.report({"INFO"}, f"[io_pdx_mesh] Mesh export finished {self.filepath}")
            IO_PDX_SETTINGS.last_export_mesh = self.filepath

        except Exception as err:
            IO_PDX_LOG.warning(f"FAILED to export '{self.filepath}'")
            IO_PDX_LOG.error(err)
            self.report({"WARNING"}, "Mesh export failed!")
            self.report({"ERROR"}, str(err))
            raise

        return {"FINISHED"}

    def invoke(self, context, event):
        self.filepath = IO_PDX_SETTINGS.last_export_mesh or ""
        context.window_manager.fileselect_add(self)

        return {"RUNNING_MODAL"}


def show_problems(operator, headline, problems):
    for problem in problems:
        IO_PDX_LOG.warning(problem)
    operator.report({"WARNING" if problems else "INFO"}, "[io_pdx_mesh] " + headline + (f" ({len(problems)} warnings)" if problems else ""))
    if problems and not bpy.app.background:
        bpy.ops.io_pdx_mesh.popup_message(
            "INVOKE_DEFAULT", msg_text=headline + "\n" + "\n".join(problems), msg_icon="ERROR", msg_width=600
        )


def asset_search_folders():
    return IO_PDX_SETTINGS.game_directory or str(find_game_dir() or ""), IO_PDX_SETTINGS.extra_asset_directories or ""


class IOPDX_OT_export_asset(Operator, ExportHelper):
    bl_idname = "io_pdx_mesh.export_asset"
    bl_description = bl_label = "Export PDX Asset"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".asset"
    filter_glob: StringProperty(
        default="*.asset",
        options={"HIDDEN"},
        maxlen=255,
    )
    filepath: StringProperty(
        name="Export File Path",
        maxlen=1024,
    )
    entity: StringProperty(
        name="Entity Name",
        description="Name of the entity to write, replacing an entity of that name in the file. Defaults to the empty's name",
    )
    cull_radius: FloatProperty(
        name="Cull Radius",
        description="Distance at which the game stops drawing the entity, left out when 0",
        default=0.0,
        min=0.0,
    )

    def draw(self, context):
        box = self.layout.box()
        box.label(text="Settings:", icon="EXPORT")
        box.prop(self, "entity")
        box.prop(self, "cull_radius")

    def execute(self, context):
        root = context.active_object
        try:
            if root is None:
                raise ValueError("Select the empty that holds the parts first")
            game_dir, extra_dirs = asset_search_folders()
            index = get_asset_index(self.filepath, game_dir, extra_dirs, rebuild=True)
            assembly = read_assembly(root, index, self.entity or None)
            replaced, problems = write_assembly(assembly, self.filepath, index, self.cull_radius)
        except ValueError as err:
            IO_PDX_LOG.warning(f"FAILED to export '{self.filepath}'")
            IO_PDX_LOG.error(err)
            self.report({"ERROR"}, str(err))
            return {"CANCELLED"}
        IO_PDX_SETTINGS.last_export_asset = self.filepath
        verb = "Replaced" if replaced else "Wrote"
        show_problems(self, f"{verb} entity '{assembly.name}': {len(assembly.locators)} locators, {len(assembly.attach)} parts", problems)
        return {"FINISHED"}

    def invoke(self, context, event):
        root = context.active_object
        self.entity = sanitize(root.name) if root is not None else ""
        last = IO_PDX_SETTINGS.last_export_asset or IO_PDX_SETTINGS.last_import_asset
        self.filepath = str(Path(last).parent / f"{self.entity or 'assembly'}.asset") if last else ""
        context.window_manager.fileselect_add(self)

        return {"RUNNING_MODAL"}


class IOPDX_OT_update_asset(Operator):
    bl_idname = "io_pdx_mesh.update_asset"
    bl_description = bl_label = "Update PDX Asset"
    bl_options = {"REGISTER", "UNDO"}

    chk_backup: BoolProperty(
        name="Keep Backup",
        description="Keep the first version of each file next to it, as <name>.asset.bak",
        default=True,
    )

    @staticmethod
    def make_plan(context):
        objects = list(context.selected_objects)
        if context.active_object is not None and context.active_object not in objects:
            objects.append(context.active_object)
        tops = tops_of(objects)
        if not tops:
            raise ValueError("Select an imported entity, or something that belongs to one")
        game_dir, extra_dirs = asset_search_folders()
        index = get_asset_index(tops[0].get("pdx_file", ""), game_dir, extra_dirs, rebuild=True)
        return plan_update(tops, index, protected=[game_dir])

    def invoke(self, context, event):
        try:
            plan = self.make_plan(context)
        except ValueError as err:
            self.report({"ERROR"}, str(err))
            return {"CANCELLED"}
        if not plan.changes:
            show_problems(self, plan.lines()[0][:1].upper() + plan.lines()[0][1:], plan.problems)
            return {"CANCELLED"}
        names = ", ".join(path.name for path in plan.files())
        return context.window_manager.invoke_confirm(
            self,
            event,
            title="Update asset files",
            message=f"{plan.lines()[0]} ({names}). Write them?",
            confirm_text="Yes, Update",
        )

    def execute(self, context):
        try:
            plan = self.make_plan(context)
        except ValueError as err:
            self.report({"ERROR"}, str(err))
            return {"CANCELLED"}
        written = apply_update(plan, backup=self.chk_backup)
        if not written:
            commit_scene(plan)
        show_problems(self, f"Updated: {plan.lines()[0]}", plan.problems + written)
        return {"FINISHED"}


class IOPDX_OT_create_assembly(Operator):
    bl_idname = "io_pdx_mesh.create_assembly"
    bl_label = "Group PDX Entities"
    bl_description = "Group the selected entities under a new empty, ready for Export PDX Asset"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(
        name="Name",
        description="Name of the empty, and of the entity it becomes",
        default="assembly",
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        parts = [obj for obj in context.selected_objects if obj.get("pdx_entity") and obj.parent is None]
        if not parts:
            self.report({"ERROR"}, "Select the imported entities to group first (their top empties)")
            return {"CANCELLED"}
        empty = bpy.data.objects.new(self.name, None)
        empty.empty_display_type = "PLAIN_AXES"
        empty.empty_display_size = 2.0
        context.collection.objects.link(empty)
        for part in parts:
            part.parent = empty
            part.matrix_parent_inverse = empty.matrix_world.inverted()
        for obj in context.selected_objects:
            obj.select_set(False)
        empty.select_set(True)
        context.view_layer.objects.active = empty
        self.report({"INFO"}, f"[io_pdx_mesh] Grouped {len(parts)} entities under '{empty.name}'")
        return {"FINISHED"}


class IOPDX_OT_export_anim(Operator, ExportHelper):
    bl_idname = "io_pdx_mesh.export_anim"
    bl_description = bl_label = "Export PDX Animation"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".anim"
    filter_glob: StringProperty(
        default="*.anim",
        options={"HIDDEN"},
        maxlen=255,
    )
    filepath: StringProperty(
        name="Export File Path",
        maxlen=1024,
    )
    int_start: IntProperty(
        name="Start Frame",
        description="Custom start frame",
        default=1,
    )
    int_end: IntProperty(
        name="End Frame",
        description="Custom end frame",
        default=100,
    )
    chk_uniform_scale: BoolProperty(
        name="Uniform Scale Only",
        description="Exports only uniform scale animation data, newer games support non-uniformly scaled bones",
        default=True,
    )
    chk_debug: BoolProperty(
        name="[Debug Options]",
        description="Non-standard options",
        default=False,
    )
    chk_plain_txt: BoolProperty(
        name="Also Export Plain Text",
        description="Exports a plain text file along with binary",
        default=False,
    )

    def draw(self, context):
        settings = context.scene.io_pdx_export

        box = self.layout.box()
        box.label(text="Settings:", icon="EXPORT")
        box.prop(settings, "custom_range")
        if settings.custom_range:
            range_settings = box.box()
            range_settings.use_property_split = True
            col = range_settings.column()
            col.prop(self, "int_start")
            col.prop(self, "int_end")
        box.prop(self, "chk_uniform_scale")
        box.prop(self, "chk_debug")
        if self.chk_debug:
            debug_settings = box.box()
            split = debug_settings.split(factor=0.1)
            _, col = split.column(), split.column()
            col.alignment = "RIGHT"
            col.prop(self, "chk_plain_txt")

    def execute(self, context):
        settings = context.scene.io_pdx_export

        try:
            if settings.custom_range:
                start, end = self.int_start, self.int_end
            else:
                start, end = context.scene.frame_start, context.scene.frame_end

            export_animfile(
                self.filepath,
                frame_start=start,
                frame_end=end,
                debug_mode=self.chk_debug,
                uniform_scale=self.chk_uniform_scale,
                plain_txt=self.chk_plain_txt,
            )

            self.report({"INFO"}, f"[io_pdx_mesh] Animation export finished! '{self.filepath}'")
            IO_PDX_SETTINGS.last_export_anim = self.filepath

        except Exception as err:
            IO_PDX_LOG.warning(f"FAILED to export '{self.filepath}'")
            IO_PDX_LOG.error(err)
            self.report({"WARNING"}, "Animation export failed!")
            self.report({"ERROR"}, str(err))
            raise

        return {"FINISHED"}

    def invoke(self, context, event):
        self.filepath = IO_PDX_SETTINGS.last_export_anim or ""
        context.window_manager.fileselect_add(self)

        return {"RUNNING_MODAL"}


class IOPDX_OT_show_axis(Operator):
    bl_idname = "io_pdx_mesh.show_axis"
    bl_description = bl_label = "Show / Hide Local Axis"
    bl_options = {"REGISTER"}

    show: BoolProperty(
        default=True,
    )
    data_type: EnumProperty(
        name="Data Type",
        items=(
            ("EMPTY", "Empty", "Empty", 1),
            ("ARMATURE", "Armature", "Armature", 2)
        ),
    )

    def execute(self, context):
        set_local_axis_display(self.show, self.data_type)
        return {"FINISHED"}


class IOPDX_OT_ignore_bone(Operator):
    bl_idname = "io_pdx_mesh.ignore_bone"
    bl_description = bl_label = "Ignore / Unignore Selected Bones"
    bl_options = {"REGISTER"}

    state: BoolProperty(
        default=False,
    )

    def execute(self, context):
        set_ignore_joints(self.state)
        return {"FINISHED"}


class IOPDX_OT_set_animation(Operator):
    bl_idname = "io_pdx_mesh.set_animation"
    bl_description = bl_label = "Play a PDX Animation on the Selected Rig"
    bl_options = {"REGISTER", "UNDO"}

    action: StringProperty(
        name="Animation",
        description="Name of the action to play, empty for the rest pose",
        default="",
        options={"HIDDEN"},
    )

    def execute(self, context):
        rig = rig_of(context.active_object)
        action = bpy.data.actions.get(self.action) if self.action else None
        if rig is None or (self.action and action not in actions_for(rig)):
            self.report({"ERROR"}, "Select a rig, or a mesh on it, that this animation was imported for")
            return {"CANCELLED"}
        assign_action(rig, action)
        if action is not None:
            context.scene.frame_end = max(context.scene.frame_end, int(action.frame_range[1]))
        return {"FINISHED"}


class IOPDX_MT_animations(Menu):
    bl_label = "Animations"

    def draw(self, context):
        rig = rig_of(context.active_object)
        if rig is None:
            return
        current = rig.animation_data.action if rig.animation_data else None
        self.layout.operator("io_pdx_mesh.set_animation", text="Rest Pose", icon="DOT" if current is None else "BLANK1").action = ""
        for action in actions_for(rig):
            icon = "DOT" if action is current else "BLANK1"
            self.layout.operator("io_pdx_mesh.set_animation", text=action_label(action), icon=icon).action = action.name


class PDXUI(object):
    bl_category = "Stellaris"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"


class IOPDX_PT_PDXblender_info(PDXUI, Panel):
    bl_label = "Info"
    bl_options = {"HIDE_HEADER"}
    panel_order = 0

    def draw(self, context):
        self.layout.label(text=f"Stellaris Mesh IO - v{github.CURRENT_VERSION}", icon="TOOL_SETTINGS")

        if github.AT_LATEST is False:
            btn_txt = f"UPDATE - v{github.LATEST_VERSION}"
            latest_urls = github.LATEST_URL if isinstance(github.LATEST_URL, dict) else {}
            self.layout.operator("wm.url_open", icon="OUTLINER_OB_LIGHT", text=btn_txt).url = str(
                latest_urls.get("blender", github.LATEST_RELEASE)
            )


class IOPDX_PT_PDXblender_file(PDXUI, Panel):
    bl_label = "File"
    panel_order = 1

    def draw(self, context):
        self.layout.label(text="Import:", icon="IMPORT")
        row = self.layout.row(align=True)
        row.operator("io_pdx_mesh.import_mesh", icon="MESH_CUBE", text="Load Mesh ...")
        row.operator("io_pdx_mesh.import_anim", icon="RENDER_ANIMATION", text="Load Anim ...")
        self.layout.operator("io_pdx_mesh.import_asset", icon="OUTLINER_COLLECTION", text="Load Asset ...")

        self.layout.label(text="Export:", icon="EXPORT")
        row = self.layout.row(align=True)
        row.operator("io_pdx_mesh.export_mesh", icon="MESH_CUBE", text="Save Mesh ...")
        row.operator("io_pdx_mesh.export_anim", icon="RENDER_ANIMATION", text="Save Anim ...")
        row = self.layout.row(align=True)
        row.operator("io_pdx_mesh.export_asset", icon="OUTLINER_COLLECTION", text="Save Asset ...")
        row.operator("io_pdx_mesh.update_asset", icon="FILE_REFRESH", text="Update Asset")


class IOPDX_PT_PDXblender_animation(PDXUI, Panel):
    bl_label = "Animation"
    panel_order = 2

    def draw(self, context):
        rig = rig_of(context.active_object)
        if rig is None:
            self.layout.label(text="Select a rig, or a mesh on one")
        elif not actions_for(rig):
            self.layout.label(text="No animations imported for this rig")
        else:
            current = rig.animation_data.action if rig.animation_data else None
            self.layout.label(text=rig.name, icon="ARMATURE_DATA")
            self.layout.menu("IOPDX_MT_animations", text=action_label(current) if current else "Rest pose", icon="ACTION")


class IOPDX_PT_PDXblender_tools(PDXUI, Panel):
    bl_label = "Tools"
    panel_order = 3

    def draw(self, context):
        col = self.layout.column(align=True)

        col.label(text="PDX Materials:")
        row = col.row(align=True)
        row.operator("io_pdx_mesh.material_create_popup", icon="MATERIAL", text="Create")
        row.operator("io_pdx_mesh.material_edit_popup", icon="SHADING_TEXTURE", text="Edit")
        col.separator()

        col.label(text="PDX Bones:")
        row = col.row(align=True)
        op_ignore_bone = row.operator("io_pdx_mesh.ignore_bone", icon="GROUP_BONE", text="Ignore Bones")
        op_ignore_bone.state = True
        op_unignore_bone = row.operator("io_pdx_mesh.ignore_bone", icon="BONE_DATA", text="Unignore Bones")
        op_unignore_bone.state = False
        col.separator()


        col.label(text="PDX Assemblies:")
        row = col.row(align=True)
        row.operator("io_pdx_mesh.create_assembly", icon="OUTLINER_OB_EMPTY", text="Group Selected")
        col.separator()

        col.label(text="PDX Meshes:")
        row = col.row(align=True)
        row.operator("io_pdx_mesh.mesh_index_popup", icon="SORTALPHA", text="Set Mesh Order")


class IOPDX_PT_PDXblender_display(PDXUI, Panel):
    bl_label = "Display"
    bl_options = {"DEFAULT_CLOSED"}
    panel_order = 4

    def draw(self, context):
        col = self.layout.column(align=True)

        col.label(text="Display Local Axes:")
        row = col.row(align=True)
        op_show_bone_axis = row.operator("io_pdx_mesh.show_axis", icon="OUTLINER_OB_ARMATURE", text="Show on Bones")
        op_show_bone_axis.show = True
        op_show_bone_axis.data_type = "ARMATURE"
        op_hide_bone_axis = row.operator("io_pdx_mesh.show_axis", icon="OUTLINER_DATA_ARMATURE", text="Hide on Bones")
        op_hide_bone_axis.show = False
        op_hide_bone_axis.data_type = "ARMATURE"
        row = col.row(align=True)
        op_show_loc_axis = row.operator("io_pdx_mesh.show_axis", icon="OUTLINER_OB_EMPTY", text="Show on Empties")
        op_show_loc_axis.show = True
        op_show_loc_axis.data_type = "EMPTY"
        op_hide_loc_axis = row.operator("io_pdx_mesh.show_axis", icon="OUTLINER_DATA_EMPTY", text="Hide on Empties")
        op_hide_loc_axis.show = False
        op_hide_loc_axis.data_type = "EMPTY"
