"""Writes .asset text. Edits go by character span, so everything not changed stays byte for byte."""

import re

try:
    from .clausewitz_text import TOKEN_RE
except ImportError:
    from clausewitz_text import TOKEN_RE

BARE_NAME_RE = re.compile(r"^[\w.\-]+$")
LOCATOR_KEYS = ("position", "rotation", "scale")
NEWLINE = chr(10)
CARRIAGE = chr(13)
INDENT = chr(9)


def number(value):
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def quote(text):
    return '"' + str(text).replace('"', "'") + '"'


def key_text(key):
    return key if BARE_NAME_RE.match(key) else quote(key)


def vector_text(values):
    return "{ " + " ".join(number(v) for v in values) + " }"


def format_locator(name, position=(0.0, 0.0, 0.0), rotation=(0.0, 0.0, 0.0), scale=1.0, parent_joint=None):
    parts = [f"name = {quote(name)}", f"position = {vector_text(position)}"]
    if any(number(a) != "0" for a in rotation):
        parts.append(f"rotation = {vector_text(rotation)}")
    if number(scale) != "1":
        parts.append(f"scale = {number(scale)}")
    if parent_joint:
        parts.append(f"parent_joint = {quote(parent_joint)}")
    return "locator = { " + " ".join(parts) + " }"


def format_entity(name, locators, attach, scale=1.0, cull_radius=0.0, newline="\n"):
    lines = ["entity = {", f"\tname = {quote(name)}"]
    if number(scale) != "1":
        lines.append(f"\tscale = {number(scale)}")
    if cull_radius:
        lines.append(f"\tcull_radius = {number(cull_radius)}")
    lines += ["\t" + format_locator(*locator) for locator in locators]
    lines += [f"\tattach = {{ {key_text(key)} = {quote(entity)} }}" for key, entity in attach]
    lines.append("}")
    return newline.join(lines)


class Entry:
    def __init__(self, key, value, start, end, open=None, close=None, value_span=None, key_end=None):
        self.key, self.value, self.start, self.end = key, value, start, end
        self.open, self.close, self.value_span, self.key_end = open, close, value_span, key_end

    @property
    def is_block(self):
        return isinstance(self.value, list)

    def children(self, key):
        return [e for e in self.value if e.key is not None and e.key.casefold() == key] if self.is_block else []

    def child(self, key):
        found = self.children(key)
        return found[0] if found else None

    def text(self, key):
        found = self.child(key)
        return found.value if found is not None and not found.is_block else None


def tokens_with_spans(text):
    for match in TOKEN_RE.finditer(text):
        if match.group("string") is not None:
            yield "str", match.group("string").strip('"'), match.start(), match.end()
        elif match.group("punct") is not None:
            yield match.group("punct"), match.group("punct"), match.start(), match.end()
        elif match.group("word") is not None:
            yield "word", match.group("word"), match.start(), match.end()


def parse_entries(tokens, pos, nested, warnings):
    entries, count = [], len(tokens)
    while pos < count:
        kind, text, start, end = tokens[pos]
        if kind == "}":
            pos += 1
            if nested:
                return entries, pos, start
            warnings.append("unmatched '}'")
        elif kind == "{":
            children, pos, close = parse_entries(tokens, pos + 1, True, warnings)
            entries.append(Entry(None, children, start, (close + 1) if close is not None else end, start, close))
        elif kind == "=":
            warnings.append("stray '='")
            pos += 1
        elif pos + 1 < count and tokens[pos + 1][0] == "=":
            pos += 2
            if pos >= count:
                warnings.append(f"'{text}' has no value")
            elif tokens[pos][0] == "{":
                open_at = tokens[pos][2]
                children, pos, close = parse_entries(tokens, pos + 1, True, warnings)
                entries.append(Entry(text, children, start, (close + 1) if close is not None else open_at + 1, open_at, close, key_end=end))
            elif tokens[pos][0] in ("word", "str"):
                value_start, value_end = tokens[pos][2], tokens[pos][3]
                entries.append(Entry(text, tokens[pos][1], start, value_end, value_span=(value_start, value_end), key_end=end))
                pos += 1
            else:
                warnings.append(f"'{text}' has no value")
        else:
            entries.append(Entry(None, text, start, end, value_span=(start, end)))
            pos += 1
    if nested:
        warnings.append("unclosed '{'")
    return entries, pos, None


def scan(text):
    warnings = []
    entries, _, _ = parse_entries(list(tokens_with_spans(text)), 0, False, warnings)
    return entries, warnings


def read_text(path):
    data = path.read_bytes()
    bom = data.startswith(b"\xef\xbb\xbf")
    return data[3:].decode("utf-8", errors="surrogateescape") if bom else data.decode("utf-8", errors="surrogateescape"), bom


def write_text(path, text, bom=False):
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8", errors="surrogateescape"))


class AssetText:
    def __init__(self, text):
        self.text = text
        self.newline = "\r\n" if "\r\n" in text else "\n"
        self.entries, self.warnings = scan(text)
        self.edits = []
        self.inserts = []

    def entity(self, name):
        for entry in self.entries:
            if entry.key and entry.key.casefold() == "entity" and entry.is_block and entry.text("name") == name:
                return entry
        return None

    def locator(self, entity, name):
        for entry in entity.children("locator"):
            if entry.text("name") == name:
                return entry
        return None

    def indent_of(self, position):
        line_start = self.text.rfind("\n", 0, position) + 1
        return self.text[line_start:position] if not self.text[line_start:position].strip() else ""

    def set_locator(self, locator, position=None, rotation=None, scale=None):
        added = []
        for key, value in (("position", position), ("rotation", rotation), ("scale", scale)):
            if value is None:
                continue
            new = number(value) if key == "scale" else vector_text(value)
            old = locator.child(key)
            if old is None:
                added.append(f"{key} = {new}")
            elif old.is_block != (key != "scale"):
                raise ValueError(f"locator '{locator.text('name')}' has an unexpected {key}")
            elif old.is_block:
                self.edits.append((old.open, old.close + 1, new))
            else:
                self.edits.append((old.value_span[0], old.value_span[1], new))
        if added:
            last = locator.value[-1] if locator.value else None
            at = last.end if last is not None else locator.open + 1
            if "\n" in self.text[locator.open : locator.close]:
                indent = self.indent_of(last.start) if last is not None else "\t\t"
                insertion = "".join(self.newline + indent + item for item in added)
            else:
                insertion = "".join(" " + item for item in added)
            self.edits.append((at, at, insertion))

    def pairs(self, entity):
        return [pair for block in entity.children("attach") for pair in block.value if pair.key is not None and not pair.is_block]

    def rename_pair(self, pair, new_key):
        self.edits.append((pair.start, pair.key_end, key_text(new_key)))

    def remove_pairs(self, entity, pairs):
        doomed = {id(pair) for pair in pairs}
        for block in entity.children("attach"):
            members = [pair for pair in block.value if pair.key is not None and not pair.is_block]
            if members and all(id(pair) in doomed for pair in members) and len(members) == len([e for e in block.value]):
                self.cut(block.start, block.end)
            else:
                for pair in members:
                    if id(pair) in doomed:
                        self.cut(pair.start, pair.end)

    def remove_child(self, entity, key):
        found = entity.child(key)
        if found is not None:
            self.cut(found.start, found.end)
        return found is not None

    def cut(self, start, end):
        line_start = self.text.rfind(NEWLINE, 0, start) + 1
        line_end = self.text.find(NEWLINE, end)
        line_end = len(self.text) if line_end < 0 else line_end + 1
        if not self.text[line_start:start].strip() and not self.text[end:line_end].strip():
            start, end = line_start, line_end
        self.edits.append((start, end, ""))

    def set_pair_value(self, pair, new_entity):
        self.edits.append((pair.value_span[0], pair.value_span[1], quote(new_entity)))

    def set_entity_name(self, entity, new_name):
        name = entity.child("name")
        if name is None or name.is_block:
            raise ValueError("the entity has no name to change")
        self.edits.append((name.value_span[0], name.value_span[1], quote(new_name)))

    def add_locators(self, entity, lines):
        multiline = NEWLINE in self.text[entity.open : entity.close]
        locators, attaches = entity.children("locator"), entity.children("attach")
        if locators:
            anchor = locators[-1]
            at, indent = anchor.end, self.indent_of(anchor.start)
            text = "".join((self.newline + indent if multiline else " ") + line for line in lines)
        elif attaches:
            at, indent = attaches[0].start, self.indent_of(attaches[0].start)
            text = "".join(line + (self.newline + indent if multiline else " ") for line in lines)
        else:
            last = entity.value[-1] if entity.value else None
            at, indent = (last.end, self.indent_of(last.start)) if last is not None else (entity.open + 1, INDENT)
            text = "".join((self.newline + indent if multiline else " ") + line for line in lines)
        self.inserts.append((at, text))

    def put_entity(self, name, block):
        block = block.replace(CARRIAGE + NEWLINE, NEWLINE).replace(NEWLINE, self.newline)
        found = self.entity(name)
        if found is not None:
            self.edits.append((found.start, found.end, block))
        else:
            prefix = "" if not self.text.strip() else ("" if self.text.endswith("\n") else self.newline) + self.newline
            self.inserts.append((len(self.text), prefix + block + self.newline))

    def result(self):
        text = self.text
        joined = {}
        for at, added in self.inserts:
            joined[at] = joined.get(at, "") + added
        edits = self.edits + [(at, at, added) for at, added in joined.items()]
        for start, end, replacement in sorted(edits, key=lambda edit: (edit[0], edit[1]), reverse=True):
            text = text[:start] + replacement + text[end:]
        return text
