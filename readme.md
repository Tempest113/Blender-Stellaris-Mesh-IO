# Blender Stellaris Mesh IO

A Blender add-on for working with Stellaris models. It imports and exports `.mesh` and `.anim` files, and loads whole entities from `.asset` files (megastructures, gigastructures, ships, the lot) with everything placed where the game puts it. You can also rearrange those entities in Blender and save the changes back to your mod.

It started out as [io_pdx_mesh](https://github.com/ross-g/io_pdx_mesh) by ross-g, which did the heavy lifting for meshes and animations. This version is Stellaris only, updated for Blender 5.2 and adds the `.asset` side of things. It's GPL-3.0-or-later like the original, see `license.txt` and `NOTICE`.

Built and tested on Blender 5.2.2 LTS. 4.2 to 5.1 should work too but haven't been re-tested.

Disclaimer: This is a vibecoded update to ross-g's original project, I have no claim to any of the code here.

## Install
1. Grab `blender_stellaris_mesh_io-<version>.zip` from the [latest release](https://github.com/Tempest113/Blender-Stellaris-Mesh-IO/releases/latest).
2. In Blender go to `Edit > Preferences > Get Extensions`, open the dropdown in the top right and pick `Install from Disk...`, then select the zip.
3. Make sure it's ticked. There'll be a `Stellaris` tab in the 3D viewport's sidebar (press `N` if the sidebar is hidden).

## Loading entities (.asset)
`Load Asset ...` in the File panel. Pick the `.asset` file, then the entity you want, entities nothing else in the file uses are listed first.

- Names are looked up in your mod first (the folder holding the file's `gfx` folder), then any extra folders you add, then the game folder. The game folder is found through Steam, you can set it in the dialog if it gets it wrong.
- Each entity ends up as an empty in a collection named after it, with its locators and attached parts under it. Parts that share a mesh share the mesh data, so big things like the Hyperstructural Assembly Yard stay manageable.
- Textures are found by name, anything missing gets a placeholder.
- It tells you how big the import is and asks before going ahead if it's over 250 entities.
- **Include Animations** is on by default and plays each entity's default animation. I'd leave it on, some models (the matter decompressor for one) only sit the right way round with their animation playing, and the dialog warns you when it's off. **All States** pulls in the other animations too, which you can switch between in the Animation panel.
- Collision meshes are left out unless you tick **Include Collision Meshes**, they come in as wireframes in their own collection, hidden by default.
- Anything it couldn't find (entities, meshes, attach points) gets listed once it's done.

## Saving entities
There are two buttons for this in the File panel.

### Save Asset
Makes a new entity out of a group of parts.
1. Import the parts you want, select their top empties and hit **Group Selected** in the Tools panel (or parent them to an empty yourself).
2. Move, rotate and scale things however you like.
3. With the group's empty selected, **Save Asset** writes an entity named after it, with a locator and an attach line for each part. Saving again with the same name replaces it.

It warns you about anything that won't work in game, like entities or meshes that don't exist yet (models made in Blender need exporting with **Save Mesh** and an entity of their own first), a name that's already taken or a file outside a `gfx` folder.

### Update Asset
Writes whatever you've changed on an imported entity back to your mod. Select any part of it, move, rotate, scale or delete stuff, then hit **Update Asset**. It tells you what it's going to change before writing anything.

- The entity you imported is edited in place and only the numbers that changed get rewritten, so comments, `@variables` and formatting are left alone. The first time a file is changed the original is kept as `<name>.asset.bak`.
- A lot of a structure like the HAY is made of shared pieces (one chunk can be used 60 times), so changing one of them can't just edit the shared definition. Anything you changed below the top is saved as a copy (`<name>_v2`, `_v3` and so on) and whatever uses it is pointed at the copy instead. Things only your entity uses are edited directly.
- Copies of vanilla entities go into your mod's file, the game folder is never touched.
- Moving a part's mesh counts as moving the part, so clicking things in the viewport and dragging them around works fine.
- Deleting a part's mesh, its empty or the locator it hangs from removes it. Deleting an empty on its own leaves whatever was under it loose (Blender moves those parts when that happens), and they're saved wherever they end up.
- Running it again straight after finds nothing to change, and re-importing gives you back what you saw.

A few things it can't do yet, it'll tell you if you run into one:
- moving the mesh of a part that has other parts attached to it (they move with it in game)
- moving pieces of a model with several meshes separately
- moving parts that hang from a locator inside the `.mesh` file

## Rotations
Locator rotations are `{ yaw pitch roll }` in degrees, applied roll first, then yaw, then pitch. This was checked in game with `tests/ingame/blender_io_rotation_test.asset`, which lines up eight ships with different rotations, handy if you ever want to double check. Copy it into a mod, attach `blender_io_rotation_test_entity` to something you can see in game and load the same entity in Blender to compare.

## Development
Blender can load the add-on straight from this folder, no zip needed:
1. In `Edit > Preferences > Get Extensions`, open the `Repositories` dropdown, pick `Add Local Repository...` and choose an empty folder.
2. Link this repo into it. On Windows, from a command prompt in that folder: `mklink /J blender_stellaris_mesh_io <path to this repo>`
3. Enable the add-on. After changing the code, run this in Blender's Python console to reload it without restarting:

```python
import sys, bpy
p = next(m for m in sys.modules if m.startswith("bl_ext.") and m.endswith(".blender_stellaris_mesh_io"))
bpy.ops.preferences.addon_disable(module=p)
for m in [m for m in sys.modules if m == p or m.startswith(p + ".")]:
    del sys.modules[m]
bpy.ops.preferences.addon_enable(module=p)
```

### Tests
`tests/run_tests.py` builds the add-on, installs it into a throwaway Blender profile and runs everything headless:

```
python tests/run_tests.py --blender <path to blender.exe>
```

Set `STELLARIS_DIR` to the game folder and `PDX_TEST_MOD` to a mod folder to include real game data. Nothing in either folder gets written to, edits are tested on copies.

### Releases
The update check follows the latest GitHub release. To put one out, set `version` in `blender_manifest.toml`, build the zip with `blender --command extension build --source-dir . --output-dir dist` (make the `dist` folder first) and attach it to a release tagged with the same version.

## Credits
- [ross-g](https://github.com/ross-g) (Ross Garfoot) for io_pdx_mesh, which this is built on, and its contributors Jonathon Shire and Jon Tetrino.
