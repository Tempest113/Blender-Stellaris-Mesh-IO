"""Blender Stellaris Mesh IO. Based on io_pdx_mesh by ross-g."""

import importlib
import inspect
import json
import logging
import os.path as path
import sys
import tomllib
import traceback
import zipfile
from collections import OrderedDict

from .settings import PDXsettings

root_path = path.abspath(path.dirname(inspect.getfile(inspect.currentframe())))
with open(path.join(root_path, "blender_manifest.toml"), "rb") as fh:
    IO_PDX_INFO = tomllib.load(fh)


log_name = "io_pdx"
log_format = "[%(name)s] %(levelname)s:  %(message)s"
log_lvl = logging.INFO

try:
    import bpy

    config_path = path.join(bpy.utils.extension_path_user(__package__, path="", create=True), "settings.json")
except (ImportError, ValueError):
    config_path = None
IO_PDX_SETTINGS = PDXsettings(config_path)

export_settings = path.join(root_path, "clausewitz.json")
ENGINE_SETTINGS = {}
try:
    if ".zip" in export_settings:
        zipped = export_settings.split(".zip")[0] + ".zip"
        with zipfile.ZipFile(zipped, "r") as z:
            f = z.open(f"{IO_PDX_INFO['id']}/clausewitz.json")
            ENGINE_SETTINGS = json.loads(f.read(), object_pairs_hook=OrderedDict)
    else:
        with open(export_settings, "rt") as f:
            ENGINE_SETTINGS = json.load(f, object_pairs_hook=OrderedDict)
except Exception as err:
    print(err)
    msg = (
        "CRITICAL ERROR! Your 'clausewitz.json' settings file has errors and is unreadable."
        "Some functions of the tool will not work without these settings."
    )
    raise RuntimeError(msg)


IO_PDX_LOG, running_from, version = None, None, None
environment = sys.executable.lower()

try:
    import bpy

    running_from, version = bpy.app.binary_path.lower(), bpy.app.version
except ImportError:
    pass
else:
    logging.basicConfig(level=log_lvl, format=log_format)
    IO_PDX_LOG = logging.getLogger(log_name)

    try:
        from .pdx_blender import register, unregister
    except Exception as e:
        traceback.print_exc()
        raise e

if running_from is not None:
    IO_PDX_LOG.info(f"Running {__package__} from {running_from} ({version})")
    IO_PDX_LOG.info(root_path)
else:
    logging.basicConfig(level=logging.DEBUG, format=log_format)
    IO_PDX_LOG = logging.getLogger(log_name)
    IO_PDX_LOG.warning(f'Running without UI from environment "{sys.executable}"')
