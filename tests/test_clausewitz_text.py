import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clausewitz_text import Block, parse, parse_file, to_float


class ParseTests(unittest.TestCase):
    def test_statements_and_nesting(self):
        doc = parse('entity = { name = "a b" pdxmesh = mesh_name locator = { name = x position = { 0 0 -210 } } }')
        entity = doc.root.get("entity")
        self.assertEqual(entity.get("name"), "a b")
        self.assertEqual(entity.get("pdxmesh"), "mesh_name")
        self.assertEqual(entity.get("locator").get("position").numbers(), [0.0, 0.0, -210.0])
        self.assertEqual(doc.warnings, [])

    def test_repeated_keys_keep_their_order(self):
        doc = parse("attach = { root = a root = b other = c }")
        self.assertEqual(list(doc.root.get("attach")), [("root", "a"), ("root", "b"), ("other", "c")])
        doc = parse("state = { name = x } state = { name = y }")
        self.assertEqual([b.get("name") for b in doc.root.blocks("state")], ["x", "y"])

    def test_whitespace_around_equals_is_optional(self):
        doc = parse("meshsettings=  {\n\tindex=0\n\tname =\"n\"\n}")
        self.assertEqual(doc.root.get("meshsettings").get("index"), "0")
        self.assertEqual(doc.root.get("meshsettings").get("name"), "n")

    def test_quoted_keys_and_pipes_in_strings(self):
        doc = parse('attach = { "root" = "birch_entity" } name = "mesh|g_mesh|g_mesh_stag"')
        self.assertEqual(list(doc.root.get("attach")), [("root", "birch_entity")])
        self.assertEqual(doc.root.get("name"), "mesh|g_mesh|g_mesh_stag")

    def test_comments(self):
        doc = parse('# whole line\nname = a # trailing\n# entity = { name = hidden }\nnote = "not # a comment"')
        self.assertEqual([k for k, _ in doc.root], ["name", "note"])
        self.assertEqual(doc.root.get("note"), "not # a comment")

    def test_values_are_not_coerced(self):
        doc = parse("a = 007 b = 1_0 c = nan d = 0.0f e = -3.5")
        self.assertEqual([v for _, v in doc.root], ["007", "1_0", "nan", "0.0f", "-3.5"])
        self.assertEqual([to_float(v) for _, v in doc.root], [7.0, None, None, 0.0, -3.5])

    def test_variables(self):
        doc = parse("@scale = 5\n@alias = @scale\nentity = { scale = @scale animation = @unknown other = @alias }")
        entity = doc.root.get("entity")
        self.assertEqual(entity.get("scale"), "5")
        self.assertEqual(entity.get("other"), "5")
        self.assertEqual(entity.get("animation"), "@unknown")
        self.assertEqual(doc.variables["@scale"], "5")

    def test_keys_are_case_insensitive(self):
        doc = parse("ObjectTypes = { pdxmesh = { name = a } }")
        self.assertEqual(len(doc.root.get("objectTypes").blocks("pdxmesh")), 1)

    def test_unbalanced_braces_recover(self):
        doc = parse("a = { b = 1 } } c = 2")
        self.assertEqual(doc.root.get("c"), "2")
        self.assertEqual(len(doc.warnings), 1)
        doc = parse("a = { b = { c = 1 }\nd = 2")
        self.assertEqual(doc.root.get("a").get("d"), "2")
        self.assertEqual(len(doc.warnings), 1)

    def test_junk_does_not_raise(self):
        for text in ("", "=", "= = =", "a =", "a = =", "{ }", "}", '"unterminated', "a = { b ="):
            self.assertIsInstance(parse(text).root, Block)

    def test_unterminated_string_stops_at_end_of_line(self):
        doc = parse('a = "oops\nb = 2')
        self.assertEqual(doc.root.get("b"), "2")

    def test_files_with_bom_and_crlf(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "x.asset"
            path.write_bytes(b"\xef\xbb\xbfentity = {\r\n\tname = a\r\n}\r\n")
            doc = parse_file(path)
        self.assertEqual(doc.root.get("entity").get("name"), "a")


@unittest.skipUnless(os.environ.get("STELLARIS_DIR"), "set STELLARIS_DIR to the game folder to parse the real files")
class RealDataTests(unittest.TestCase):
    def test_every_model_file_parses(self):
        models = Path(os.environ["STELLARIS_DIR"]) / "gfx" / "models"
        files = [p for pattern in ("*.asset", "*.gfx") for p in models.rglob(pattern)]
        entities = pdxmeshes = unbalanced = 0
        for path in files:
            doc = parse_file(path)
            unbalanced += bool(doc.warnings)
            entities += len(doc.root.blocks("entity"))
            for types in doc.root.blocks("objectTypes"):
                pdxmeshes += len(types.blocks("pdxmesh"))
        self.assertGreater(len(files), 500)
        self.assertGreater(entities, 5000)
        self.assertGreater(pdxmeshes, 2000)
        self.assertLessEqual(unbalanced, 5)


if __name__ == "__main__":
    unittest.main()
