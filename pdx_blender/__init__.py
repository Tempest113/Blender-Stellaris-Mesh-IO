"""Blender registration. Author: ross-g."""

import importlib
import inspect

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import PropertyGroup

from .. import IO_PDX_LOG
from . import blender_asset, blender_asset_export, blender_import_export, blender_ui

importlib.reload(blender_import_export)
importlib.reload(blender_asset)
importlib.reload(blender_asset_export)
importlib.reload(blender_ui)


class PDXMaterial_settings(PropertyGroup):
    mat_name: StringProperty(
        name="Material name",
        description="Material name",
        default="",
    )
    mat_type: StringProperty(
        name="Material type",
        description="Material type",
        default="",
    )


class PDXObject_Pointer(PropertyGroup):
    ref: PointerProperty(
        name='pdx pointer',
        type=bpy.types.Object,
    )


class PDXObject_Group(PropertyGroup):
    coll: CollectionProperty(
        type=PDXObject_Pointer,
    )
    idx: IntProperty()


class PDXExport_settings(PropertyGroup):
    custom_range: BoolProperty(
        name='Custom range',
        description='Custom range',
        default=False,
    )


classes = [PDXMaterial_settings, PDXObject_Pointer, PDXObject_Group, PDXExport_settings]

for _name, obj in inspect.getmembers(blender_ui, inspect.isclass):
    if obj.__module__.startswith(__name__) and hasattr(obj, "bl_rna"):
        classes.append(obj)

classes.sort(key=lambda cls: cls.panel_order if hasattr(cls, "panel_order") else 0)


def register():
    IO_PDX_LOG.info("Loading Blender UI.")
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.io_pdx_material = PointerProperty(type=PDXMaterial_settings)
    bpy.types.Scene.io_pdx_group = PointerProperty(type=PDXObject_Group)
    bpy.types.Scene.io_pdx_export = PointerProperty(type=PDXExport_settings)


def unregister():
    for cls in classes:
        bpy.utils.unregister_class(cls)

    del bpy.types.Scene.io_pdx_material
    del bpy.types.Scene.io_pdx_group
    del bpy.types.Scene.io_pdx_export
