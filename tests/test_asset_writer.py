import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asset_writer import AssetText, format_entity, format_locator, number, read_text, scan, write_text
from clausewitz_text import parse

SOURCE = """# header comment
@s = 3
entity = {
\tname = "first"   # trailing comment
\tscale = @s
\tlocator = { name = "a" position = { 1 2 3 } rotation = { 90 0 0 } scale = 5 }
\tlocator = { name = "b" position = { 0 0 0 } }
\tlocator = {
\t\tname = "c"
\t\tposition = { 4 5 6 }
\t}
\tattach = { a = "other" }
}

entity = {
\tname = "second"
\tlocator = { name = "a" position = { 9 9 9 } }
}
"""


def locator_values(text, entity, name):
    found = next(b for b in parse(text).root.blocks("entity") if b.get("name") == entity)
    loc = next(b for b in found.blocks("locator") if b.get("name") == name)
    return {k: (v.numbers() if hasattr(v, "numbers") else v) for k, v in loc}


class FormatTests(unittest.TestCase):
    def test_numbers_are_short_and_never_negative_zero(self):
        self.assertEqual([number(v) for v in (1350.0, -0.0, 0.5, 1 / 3, -2.25, 1e-9, 89.999989)], ["1350", "0", "0.5", "0.3333", "-2.25", "0", "90"])

    def test_locator_leaves_out_defaults(self):
        self.assertEqual(format_locator("a", (1, 2, 3)), 'locator = { name = "a" position = { 1 2 3 } }')
        self.assertEqual(
            format_locator("a", (0, 0, -1350), (180, 0, 0), 2, "joint"),
            'locator = { name = "a" position = { 0 0 -1350 } rotation = { 180 0 0 } scale = 2 parent_joint = "joint" }',
        )

    def test_entity_is_read_back_by_the_parser(self):
        text = format_entity("giga", [("n", (1350, 0, 0), (90, 0, 0), 1.0)], [("n", "quarter"), ("n", "other")], scale=2, cull_radius=9999)
        entity = parse(text).root.blocks("entity")[0]
        self.assertEqual(entity.get("name"), "giga")
        self.assertEqual(entity.get("scale"), "2")
        self.assertEqual(entity.get("cull_radius"), "9999")
        self.assertEqual([(k, v) for a in entity.blocks("attach") for k, v in a], [("n", "quarter"), ("n", "other")])
        self.assertEqual(entity.blocks("locator")[0].get("rotation").numbers(), [90.0, 0.0, 0.0])
        self.assertNotIn("scale", format_entity("x", [], []))

    def test_odd_names_are_quoted(self):
        self.assertIn('"a b" = "e"', format_entity("x", [], [("a b", "e")]))


class ScanTests(unittest.TestCase):
    def test_spans_cover_the_text_of_each_statement(self):
        entries, warnings = scan(SOURCE)
        self.assertEqual(warnings, [])
        entities = [e for e in entries if e.key == "entity"]
        self.assertEqual(len(entities), 2)
        first = entities[0]
        self.assertEqual(SOURCE[first.start : first.end][:8], "entity =")
        self.assertTrue(SOURCE[first.start : first.end].endswith("}"))
        loc = first.children("locator")[0]
        self.assertEqual(SOURCE[loc.start : loc.end], 'locator = { name = "a" position = { 1 2 3 } rotation = { 90 0 0 } scale = 5 }')
        position = loc.child("position")
        self.assertEqual(SOURCE[position.open : position.close + 1], "{ 1 2 3 }")
        scale = loc.child("scale")
        self.assertEqual(SOURCE[scale.value_span[0] : scale.value_span[1]], "5")

    def test_problems_are_reported_not_raised(self):
        self.assertTrue(scan("entity = { name = x")[1])
        self.assertTrue(scan("entity = { name = x } }")[1])


class EditTests(unittest.TestCase):
    def test_unchanged_text_is_returned_as_is(self):
        self.assertEqual(AssetText(SOURCE).result(), SOURCE)

    def test_only_the_edited_values_change(self):
        doc = AssetText(SOURCE)
        loc = doc.locator(doc.entity("first"), "a")
        doc.set_locator(loc, position=(10, 20, 30))
        result = doc.result()
        self.assertEqual(result, SOURCE.replace("position = { 1 2 3 }", "position = { 10 20 30 }"))
        self.assertEqual(locator_values(result, "first", "a")["rotation"], [90.0, 0.0, 0.0])
        self.assertEqual(locator_values(result, "second", "a")["position"], [9.0, 9.0, 9.0])

    def test_rotation_and_scale_replace_in_place(self):
        doc = AssetText(SOURCE)
        doc.set_locator(doc.locator(doc.entity("first"), "a"), rotation=(0, 45, 0), scale=2.5)
        values = locator_values(doc.result(), "first", "a")
        self.assertEqual((values["rotation"], values["scale"]), ([0.0, 45.0, 0.0], "2.5"))

    def test_missing_keys_are_added_on_a_single_line(self):
        doc = AssetText(SOURCE)
        doc.set_locator(doc.locator(doc.entity("first"), "b"), rotation=(0, 90, 0), scale=2)
        result = doc.result()
        self.assertIn('locator = { name = "b" position = { 0 0 0 } rotation = { 0 90 0 } scale = 2 }', result)
        self.assertEqual(result.count("\n"), SOURCE.count("\n"))

    def test_missing_keys_are_added_on_their_own_line_in_a_multi_line_locator(self):
        doc = AssetText(SOURCE)
        doc.set_locator(doc.locator(doc.entity("first"), "c"), rotation=(1, 2, 3))
        self.assertIn("\t\tposition = { 4 5 6 }\n\t\trotation = { 1 2 3 }\n\t}", doc.result())

    def test_several_locators_in_one_pass(self):
        doc = AssetText(SOURCE)
        entity = doc.entity("first")
        doc.set_locator(doc.locator(entity, "a"), position=(0, 0, 1))
        doc.set_locator(doc.locator(entity, "b"), position=(0, 0, 2), rotation=(5, 0, 0))
        doc.set_locator(doc.locator(entity, "c"), scale=3)
        result = doc.result()
        self.assertEqual(locator_values(result, "first", "a")["position"], [0.0, 0.0, 1.0])
        self.assertEqual(locator_values(result, "first", "b")["rotation"], [5.0, 0.0, 0.0])
        self.assertEqual(locator_values(result, "first", "c")["scale"], "3")
        self.assertIn("# header comment", result)
        self.assertIn("scale = @s", result)

    def test_attach_pairs_are_listed_in_order_and_can_be_moved_to_another_locator(self):
        text = 'entity = { name = "x" attach = { "a" = e1 a = e2 } attach = { b = e1 } }'
        doc = AssetText(text)
        pairs = doc.pairs(doc.entity("x"))
        self.assertEqual([(p.key, p.value) for p in pairs], [("a", "e1"), ("a", "e2"), ("b", "e1")])
        doc.rename_pair(pairs[0], "new one")
        doc.rename_pair(pairs[1], "z")
        self.assertEqual(doc.result(), 'entity = { name = "x" attach = { "new one" = e1 z = e2 } attach = { b = e1 } }')

    def test_locators_are_added_after_the_last_one_in_order(self):
        doc = AssetText(SOURCE)
        doc.add_locators(doc.entity("first"), ["locator = { name = \"n1\" }", "locator = { name = \"n2\" }"])
        result = doc.result()
        lines = result.splitlines()
        at = next(i for i, line in enumerate(lines) if '"c"' in line)
        self.assertEqual([l.strip() for l in lines[at + 2 : at + 5]], ["}", 'locator = { name = "n1" }', 'locator = { name = "n2" }'])
        self.assertEqual(lines[at + 3][:1], "\t")
        self.assertEqual(sorted(b.get("name") for b in parse(result).root.blocks("entity")[0].blocks("locator")), ["a", "b", "c", "n1", "n2"])

    def test_locators_go_before_the_first_attach_when_there_are_none(self):
        doc = AssetText('entity = { name = "x" attach = { root = e } }')
        doc.add_locators(doc.entity("x"), ['locator = { name = "n" }'])
        self.assertEqual(doc.result(), 'entity = { name = "x" locator = { name = "n" } attach = { root = e } }')
        doc = AssetText('entity = { name = "x" }')
        doc.add_locators(doc.entity("x"), ['locator = { name = "n" }'])
        self.assertEqual(doc.result(), 'entity = { name = "x" locator = { name = "n" } }')

    def test_a_pair_can_point_at_another_entity_and_an_entity_can_be_renamed(self):
        doc = AssetText('entity = { name = "x" attach = { a = "e1" b = e2 } }')
        entity = doc.entity("x")
        pairs = doc.pairs(entity)
        doc.set_pair_value(pairs[0], "e1_v2")
        doc.set_pair_value(pairs[1], "e2_v2")
        doc.set_entity_name(entity, "x_v2")
        self.assertEqual(doc.result(), 'entity = { name = "x_v2" attach = { a = "e1_v2" b = "e2_v2" } }')

    def test_pairs_can_be_removed_and_so_can_blocks_left_empty(self):
        text = 'entity = {\n\tname = "x"\n\tattach = {\n\t\ta = e1\n\t\tb = e2\n\t}\n\tattach = { c = e3 }\n\tscale = 2\n}\n'
        doc = AssetText(text)
        entity = doc.entity("x")
        pairs = doc.pairs(entity)
        doc.remove_pairs(entity, [pairs[0], pairs[2]])
        result = doc.result()
        self.assertEqual(result, 'entity = {\n\tname = "x"\n\tattach = {\n\t\tb = e2\n\t}\n\tscale = 2\n}\n')
        again = AssetText('entity = { name = "x" attach = { a = e1 b = e2 } }')
        pairs = again.pairs(again.entity("x"))
        again.remove_pairs(again.entity("x"), [pairs[0]])
        parsed = parse(again.result()).root.blocks("entity")[0]
        self.assertEqual([(k, v) for block in parsed.blocks("attach") for k, v in block], [("b", "e2")])
        everything = AssetText('entity = { name = "x" attach = { a = e1 b = e2 } }')
        everything.remove_pairs(everything.entity("x"), everything.pairs(everything.entity("x")))
        self.assertEqual(parse(everything.result()).root.blocks("entity")[0].blocks("attach"), [])

    def test_bytes_that_are_not_utf8_and_a_bom_survive_a_rewrite(self):
        import tempfile

        body = b'entity = { name = "x" locator = { name = "l" position = { 1 2 3 } } }'
        before = bytes([0xEF, 0xBB, 0xBF]) + b"# caf" + bytes([0xE9, 0x0D, 0x0A]) + body + bytes([0x0D, 0x0A])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "a.asset"
            path.write_bytes(before)
            text, bom = read_text(path)
            doc = AssetText(text)
            doc.set_locator(doc.locator(doc.entity("x"), "l"), position=(4, 5, 6))
            write_text(path, doc.result(), bom)
            self.assertEqual(path.read_bytes(), before.replace(b"1 2 3", b"4 5 6"))

    def test_new_entities_are_added_in_the_order_they_were_put(self):
        doc = AssetText(SOURCE)
        for name in ("n1", "n2", "n3"):
            doc.put_entity(name, format_entity(name, [], []))
        names = [b.get("name") for b in parse(doc.result()).root.blocks("entity")]
        self.assertEqual(names, ["first", "second", "n1", "n2", "n3"])

    def test_wrong_shaped_values_are_refused(self):
        doc = AssetText("entity = { name = x locator = { name = a position = 1 } }")
        with self.assertRaises(ValueError):
            doc.set_locator(doc.locator(doc.entity("x"), "a"), position=(1, 2, 3))

    def test_entity_lookup_ignores_case_of_the_keyword(self):
        self.assertIsNotNone(AssetText('ENTITY = { name = "x" }').entity("x"))
        self.assertIsNone(AssetText(SOURCE).entity("missing"))

    def test_put_entity_replaces_or_appends(self):
        block = format_entity("second", [("n", (1, 1, 1), (0, 0, 0), 1.0)], [])
        doc = AssetText(SOURCE)
        doc.put_entity("second", block)
        result = doc.result()
        self.assertIn(block, result)
        self.assertEqual(result.count("entity = {"), 2)
        self.assertNotIn("9 9 9", result)
        self.assertTrue(result.startswith(SOURCE[: SOURCE.index('name = "second"')][:60]))

        doc = AssetText(SOURCE)
        doc.put_entity("third", format_entity("third", [], []))
        result = doc.result()
        self.assertTrue(result.startswith(SOURCE) and result.endswith('entity = {\n\tname = "third"\n}\n'), repr(result[-60:]))

        doc = AssetText("")
        doc.put_entity("x", format_entity("x", [], []))
        self.assertEqual(doc.result(), 'entity = {\n\tname = "x"\n}\n')

    def test_line_endings_are_kept(self):
        crlf = SOURCE.replace("\n", "\r\n")
        doc = AssetText(crlf)
        doc.set_locator(doc.locator(doc.entity("first"), "c"), rotation=(1, 2, 3))
        doc.put_entity("third", format_entity("third", [], []))
        result = doc.result()
        self.assertEqual(result.replace("\r\n", "\n").count("\r"), 0)
        self.assertEqual(result.count("\n"), result.count("\r\n"))


if __name__ == "__main__":
    unittest.main()
