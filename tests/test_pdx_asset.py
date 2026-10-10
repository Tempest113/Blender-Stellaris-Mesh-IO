import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pdx_asset import AssetIndex, find_game_dir, find_mod_root, read_shader_effects

VANILLA_GFX = """
objectTypes = {
    pdxmesh = {
        name = "mesh_a"
        file = "gfx/models/a/a.mesh"
        scale = 2
        animation = { id = "idle" type = "anim_a" }
    }
}
"""

VANILLA_ASSET = """
@s = 3
entity = {
    name = "base"
    pdxmesh = "mesh_a"
    scale = @s
    locator = { name = "l1" position = { 1 2 3 } rotation = { 90 0 0 } scale = 5 }
}
entity = {
    name = "top"
    clone = "base"
    attach = { l1 = "child" l1 = "child" }
    state = { name = "idle" animation = "idle" }
    default_state = "idle"
}
entity = { name = "child" pdxmesh = "mesh_a" }
animation = { name = "anim_a" file = "a_idle.anim" }
"""

MOD_ASSET = """
entity = { name = "child" }
entity = { name = "mod_root" attach = { l1 = "top" } }
"""


def write(root, relative, text):
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        base = Path(self.folder.name)
        self.vanilla, self.mod = base / "vanilla", base / "mod"
        write(self.vanilla, "gfx/models/a/a.gfx", VANILLA_GFX)
        self.asset = write(self.vanilla, "gfx/models/a/a.asset", VANILLA_ASSET)
        write(self.vanilla, "gfx/models/a/a.mesh", "")
        self.mod_asset = write(self.mod, "gfx/models/b.asset", MOD_ASSET)
        self.index = AssetIndex([self.mod, self.vanilla])

    def tearDown(self):
        self.folder.cleanup()

    def test_clone_merges_into_the_derived_entity(self):
        top = self.index.entity("top")
        self.assertEqual((top.pdxmesh, top.scale, top.default_state), ("mesh_a", 3.0, "idle"))
        self.assertEqual(top.attach, [("l1", "child"), ("l1", "child")])
        self.assertEqual(top.states, [("idle", "idle")])
        locator = top.locators["l1"]
        self.assertEqual((locator.position, locator.rotation, locator.scale), ((1.0, 2.0, 3.0), (90.0, 0.0, 0.0), 5.0))

    def test_earlier_roots_override_later_ones(self):
        self.assertIsNone(self.index.entity("child").pdxmesh)
        self.assertTrue(any("'child'" in w for w in self.index.warnings))
        self.assertEqual(self.index.entity("mod_root").attach, [("l1", "top")])

    def test_pdxmesh_and_animation_definitions(self):
        mesh = self.index.pdxmeshes["mesh_a"]
        self.assertEqual((mesh.file, mesh.scale, mesh.animations), ("gfx/models/a/a.mesh", 2.0, {"idle": "anim_a"}))
        self.assertEqual(self.index.animations["anim_a"], self.asset.parent / "a_idle.anim")

    def test_files_resolve_through_the_roots_in_order(self):
        self.assertEqual(self.index.resolve_file("gfx/models/a/a.mesh"), self.vanilla / "gfx/models/a/a.mesh")
        write(self.mod, "gfx/models/a/a.mesh", "")
        self.assertEqual(self.index.resolve_file("gfx/models/a/a.mesh"), self.mod / "gfx/models/a/a.mesh")
        self.assertIsNone(self.index.resolve_file("gfx/models/a/missing.mesh"))

    def test_tree_size(self):
        self.assertEqual(self.index.tree_size("top"), (3, 1, 2))
        self.assertEqual(self.index.tree_size("mod_root"), (4, 1, 3))
        self.assertEqual(self.index.tree_size("not_defined"), (0, 0, 0))

    def test_unknown_attach_targets_are_not_instances(self):
        write(self.vanilla, "gfx/models/c.asset", 'entity = { name = "e" attach = { l = "nowhere" } }')
        self.assertEqual(AssetIndex([self.vanilla]).tree_size("e"), (1, 0, 1))

    def test_cycles_terminate(self):
        text = 'entity = { name = "x" attach = { l = "y" } } entity = { name = "y" attach = { l = "x" l = "y" } }'
        write(self.vanilla, "gfx/models/cycle.asset", text + 'entity = { name = "c1" clone = "c2" } entity = { name = "c2" clone = "c1" }')
        index = AssetIndex([self.vanilla])
        self.assertEqual(index.tree_size("x")[0], 2)
        self.assertIsNotNone(index.entity("c1"))

    def test_very_deep_trees_do_not_recurse(self):
        depth = 5000
        text = "".join(f'entity = {{ name = "e{i}" attach = {{ l = "e{i + 1}" }} }}\n' for i in range(depth))
        write(self.vanilla, "gfx/models/deep.asset", text + f'entity = {{ name = "e{depth}" }}')
        self.assertEqual(AssetIndex([self.vanilla]).tree_size("e0"), (depth + 1, 0, depth + 1))

    def test_root_entities_of_a_file(self):
        self.assertEqual(self.index.root_entities(self.asset), ["top"])

    def test_textures_are_found_by_name(self):
        near = write(self.vanilla, "gfx/models/a/near.dds", "")
        write(self.vanilla, "gfx/other/Far.DDS", "")
        write(self.mod, "gfx/elsewhere/far.dds", "")
        index = AssetIndex([self.mod, self.vanilla])
        self.assertEqual(index.find_texture("near.dds", near.parent), near)
        self.assertEqual(index.find_texture("far.dds"), self.mod / "gfx/elsewhere/far.dds")
        self.assertEqual(index.find_texture("FAR.dds", near.parent), self.mod / "gfx/elsewhere/far.dds")
        self.assertIsNone(index.find_texture("absent.dds", near.parent))

    def test_game_folder_is_found_through_steam_libraries(self):
        base = Path(self.folder.name)
        library = base / "library"
        listing = '"libraryfolders" { "0" { "path" "%s" } }' % str(library).replace("\\", "\\\\")
        write(base / "steam", "steamapps/libraryfolders.vdf", listing)
        (library / "steamapps/common/Stellaris/gfx").mkdir(parents=True)
        self.assertEqual(find_game_dir(steam_roots=[base / "steam"]), library / "steamapps/common/Stellaris")
        self.assertIsNone(find_game_dir(steam_roots=[base / "nowhere"]))
        self.assertIsNone(find_game_dir("Other game", steam_roots=[base / "steam"]))

    def test_find_mod_root(self):
        self.assertEqual(find_mod_root(self.asset), self.vanilla.resolve())
        self.assertIsNone(find_mod_root(Path(self.folder.name).anchor))


SHADER_STATES = """
BlendState BlendStateAlphaBlend
{
	BlendEnable = yes
	SourceBlend = "SRC_ALPHA"
	DestBlend = "INV_SRC_ALPHA"
}
BlendState BlendStateAdditiveBlend
{
	BlendEnable = yes
	SourceBlend = "SRC_ALPHA"
	DestBlend = "ONE"
}
BlendState BlendStateOff
{
	BlendEnable = no
}
"""

SHADER_EFFECTS = """
Effect Plain
{
	VertexShader = "VertexPdxMeshStandard"
	PixelShader = "PixelPdxMeshStandard"
}
Effect Glows
{
	PixelShader = "PixelPdxMeshStandard"
	Defines = { "EMISSIVE" }
}
Effect Ship
{
	PixelShader = "PixelPdxMeshShip"
	Defines = { "RIM_LIGHT" }
}
Effect OmniShip
{
	PixelShader = "PixelOmniMeshShip"
}
Effect Blended
{
	BlendState = "BlendStateAlphaBlend";
	Defines = { "NO_ALPHA_MULTIPLIED_EMISSIVE" }
}
Effect Additive
{
	BlendState = "BlendStateAdditiveBlend"
	DepthStencilState = "DepthStencilNoZWrite"
	Defines = { "DISSOLVE" }
}
Effect Cutout
{
	PixelShader = "PixelPdxMeshStandard"
	Defines = { "ALPHA_TEST" }
}
Effect Unknown
{
	BlendState = "BlendStateDefinedNowhere"
}
"""


class ShaderTests(unittest.TestCase):
    def test_blend_modes_and_emission_come_from_the_effects(self):
        effects = read_shader_effects([SHADER_EFFECTS, SHADER_STATES])
        got = {name: (info.blend, info.emissive) for name, info in effects.items()}
        self.assertEqual(got, {
            "Plain": (None, False), "Glows": (None, True), "Ship": (None, True), "OmniShip": (None, True),
            "Blended": ("blend", False), "Additive": ("additive", False), "Cutout": ("cutout", False),
            "Unknown": (None, False),
        })

    def test_earlier_files_win(self):
        mod = 'Effect Plain { BlendState = "BlendStateAdditiveBlend" }'
        self.assertEqual(read_shader_effects([mod, SHADER_EFFECTS, SHADER_STATES])["Plain"].blend, "additive")

    def test_shader_files_are_read_from_every_root(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        vanilla, mod = Path(folder.name, "vanilla"), Path(folder.name, "mod")
        write(vanilla, "gfx/FX/pdxmesh.shader", SHADER_EFFECTS + SHADER_STATES)
        write(mod, "gfx/FX/pdxmesh.shader", 'Effect Extra { BlendState = "BlendStateAdditiveBlend" }')
        index = AssetIndex([mod, vanilla])
        self.assertEqual(index.shader_info("Extra").blend, "additive")
        self.assertEqual(index.shader_info("Blended").blend, "blend")
        self.assertIsNone(index.shader_info("NoSuchEffect"))


@unittest.skipUnless(os.environ.get("STELLARIS_DIR"), "set STELLARIS_DIR to the game folder to index the real files")
class RealDataTests(unittest.TestCase):
    def test_dyson_gun(self):
        game = Path(os.environ["STELLARIS_DIR"])
        started = time.time()
        index = AssetIndex([game])
        self.assertLess(time.time() - started, 30)
        entity = index.entity("dyson_gun_01_stage_1_entity")
        self.assertEqual(entity.attach, [("part1", "dyson_gun_01_stage_1_section_entity")])
        mesh = index.pdxmeshes[entity.pdxmesh]
        self.assertTrue(index.resolve_file(mesh.file))
        self.assertGreater(index.tree_size(entity.name)[0], 1)

    def test_game_shaders(self):
        index = AssetIndex([Path(os.environ["STELLARIS_DIR"])])
        modes = {name: (index.shader_info(name).blend, index.shader_info(name).emissive) for name in (
            "PdxMeshShip", "PdxMeshShipDiffuseEmissiveAlpha", "PdxMeshAlphaBlend", "PdxMeshAlphaAdditive", "PdxMeshWPO", "PdxMeshStandard"
        )}
        self.assertEqual(modes, {
            "PdxMeshShip": (None, True), "PdxMeshShipDiffuseEmissiveAlpha": ("blend", True), "PdxMeshAlphaBlend": ("cutout", False),
            "PdxMeshAlphaAdditive": ("additive", False), "PdxMeshWPO": ("additive", False), "PdxMeshStandard": (None, False),
        })


if __name__ == "__main__":
    unittest.main()
