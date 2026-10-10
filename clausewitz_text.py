"""Parser for Clausewitz script text (.asset, .gfx), tolerant of the broken files in the game data."""

import re
from pathlib import Path

TOKEN_RE = re.compile(r'\s+|#[^\n]*|(?P<string>"[^"\n]*"?)|(?P<punct>[={}])|(?P<word>[^\s={}"#]+)')
NUMBER_RE = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?[fF]?$")


def to_float(text, default=None):
    if isinstance(text, str) and NUMBER_RE.match(text):
        return float(text.rstrip("fF"))
    return default


class Block(list):
    def get(self, key, default=None):
        key = key.casefold()
        for k, v in self:
            if k is not None and k.casefold() == key:
                return v
        return default

    def get_all(self, key):
        key = key.casefold()
        return [v for k, v in self if k is not None and k.casefold() == key]

    def blocks(self, key):
        return [v for v in self.get_all(key) if isinstance(v, Block)]

    def numbers(self):
        return [to_float(v) for k, v in self if k is None and isinstance(v, str)]


class Document:
    def __init__(self, root, variables, warnings):
        self.root = root
        self.variables = variables
        self.warnings = warnings


def tokenize(text):
    tokens = []
    for match in TOKEN_RE.finditer(text):
        if match.group("string") is not None:
            tokens.append(("str", match.group("string").strip('"')))
        elif match.group("punct") is not None:
            tokens.append((match.group("punct"), match.group("punct")))
        elif match.group("word") is not None:
            tokens.append(("word", match.group("word")))
    return tokens


def parse_block(tokens, pos, nested, warnings):
    block = Block()
    count = len(tokens)
    while pos < count:
        kind, text = tokens[pos]
        if kind == "}":
            pos += 1
            if nested:
                return block, pos
            warnings.append("unmatched '}' ignored")
        elif kind == "{":
            child, pos = parse_block(tokens, pos + 1, True, warnings)
            block.append((None, child))
        elif kind == "=":
            warnings.append("stray '=' ignored")
            pos += 1
        elif pos + 1 < count and tokens[pos + 1][0] == "=":
            pos += 2
            if pos >= count:
                warnings.append(f"'{text}' has no value")
            elif tokens[pos][0] == "{":
                child, pos = parse_block(tokens, pos + 1, True, warnings)
                block.append((text, child))
            elif tokens[pos][0] in ("word", "str"):
                block.append((text, tokens[pos][1]))
                pos += 1
            else:
                warnings.append(f"'{text}' has no value")
        else:
            block.append((None, text))
            pos += 1
    if nested:
        warnings.append("unclosed '{' at end of file")
    return block, pos


def substitute(block, variables):
    for i, (key, value) in enumerate(block):
        if isinstance(value, Block):
            substitute(value, variables)
        elif value.startswith("@") and value in variables:
            block[i] = (key, variables[value])


def parse(text):
    warnings = []
    root, _ = parse_block(tokenize(text), 0, False, warnings)

    variables = {key: value for key, value in root if key and key.startswith("@") and isinstance(value, str)}
    for _ in range(4):
        variables = {k: variables.get(v, v) for k, v in variables.items()}
    substitute(root, variables)
    return Document(root, variables, warnings)


def parse_file(path):
    return parse(Path(path).read_text(encoding="utf-8-sig", errors="replace"))
