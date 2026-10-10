"""Index of the entity, pdxmesh and animation definitions in the game and mod folders."""

import re
from pathlib import Path

try:
    from .clausewitz_text import Block, parse_file, to_float
except ImportError:
    from clausewitz_text import Block, parse_file, to_float

MODEL_FOLDER = ("gfx", "models")
TEXTURE_EXTENSIONS = (".dds", ".tga", ".png")


class Locator:
    def __init__(self, name, position=(0.0, 0.0, 0.0), rotation=(0.0, 0.0, 0.0), scale=1.0, parent_joint=None):
        self.name = name
        self.position = position
        self.rotation = rotation
        self.scale = scale
        self.parent_joint = parent_joint


class Entity:
    def __init__(self, name, source=None):
        self.name = name
        self.source = source
        self.pdxmesh = None
        self.scale = 1.0
        self.locators = {}
        self.attach = []
        self.attach_sources = []
        self.meshsettings = []
        self.states = []
        self.default_state = None


class PdxMesh:
    def __init__(self, name, source, block):
        self.name = name
        self.source = source
        self.file = block.get("file")
        self.scale = to_float(block.get("scale"), 1.0)
        self.animations = {b.get("id"): b.get("type") for b in block.blocks("animation") if b.get("id")}
        self.meshsettings = block.blocks("meshsettings")


def find_mod_root(path):
    for folder in Path(path).resolve().parents:
        if (folder / MODEL_FOLDER[0]).is_dir():
            return folder
    return None


def find_game_dir(folder="Stellaris", steam_roots=None):
    if steam_roots is None:
        steam_roots = [
            Path("C:/Program Files (x86)/Steam"),
            Path.home() / ".steam" / "steam",
            Path.home() / ".local" / "share" / "Steam",
            Path.home() / "Library" / "Application Support" / "Steam",
        ]
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                steam_roots.insert(0, Path(winreg.QueryValueEx(key, "SteamPath")[0]))
        except (ImportError, OSError):
            pass
    libraries = []
    for steam in steam_roots:
        libraries.append(steam)
        listing = steam / "steamapps" / "libraryfolders.vdf"
        if listing.is_file():
            text = listing.read_text(encoding="utf-8", errors="replace")
            libraries += [Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', text)]
    for library in libraries:
        candidate = library / "steamapps" / "common" / folder
        if (candidate / MODEL_FOLDER[0]).is_dir():
            return candidate
    return None


def vector(block, key, default):
    value = block.get(key)
    values = value.numbers() if isinstance(value, Block) else []
    return tuple(values) if len(values) == 3 and None not in values else default


class ShaderInfo:
    def __init__(self, blend=None, emissive=False):
        self.blend = blend
        self.emissive = emissive


def read_blocks(text, keyword):
    blocks = []
    for match in re.finditer(r"^" + keyword + r"\s+(\w+)\s*\{", text, re.MULTILINE):
        depth, pos = 1, match.end()
        while depth and pos < len(text):
            depth += {"{": 1, "}": -1}.get(text[pos], 0)
            pos += 1
        blocks.append((match.group(1), text[match.end() : pos - 1]))
    return blocks


def read_shader_effects(texts):
    states = {}
    for text in texts:
        for name, body in read_blocks(text, "BlendState"):
            if re.search(r"BlendEnable\s*=\s*yes", body):
                states.setdefault(name, "additive" if re.search(r"DestBlend\s*=\s*\"ONE\"", body) else "blend")
            elif re.search(r"AlphaTest\s*=\s*yes", body):
                states.setdefault(name, "cutout")
    effects = {}
    for text in texts:
        for name, body in read_blocks(text, "Effect"):
            state = re.search(r"BlendState\s*=\s*\"(\w+)\"", body)
            blend = states.get(state.group(1)) if state else None
            if blend is None and "\"ALPHA_TEST\"" in body:
                blend = "cutout"
            ship = re.search(r"PixelShader\s*=\s*\"Pixel\w*Ship\"", body)
            effects.setdefault(name, ShaderInfo(blend, bool(ship or "\"EMISSIVE\"" in body)))
    return effects


class AssetIndex:
    def __init__(self, roots):
        self.roots = [Path(r) for r in roots if r]
        self._shaders = None
        self.warnings = []
        self.entity_blocks = {}
        self.pdxmeshes = {}
        self.animations = {}
        self.entities_by_file = {}
        self._entities = {}
        self._textures = None
        for root in self.roots:
            self.scan(root)

    def scan(self, root):
        models = root.joinpath(*MODEL_FOLDER)
        for pattern in ("*.asset", "*.gfx"):
            for path in sorted(models.rglob(pattern)):
                doc = parse_file(path)
                if pattern == "*.asset":
                    self.add_asset(path, doc.root)
                else:
                    self.add_gfx(path, doc.root)

    def add(self, table, kind, name, path, value):
        if name is None:
            return
        if name in table:
            self.warnings.append(f"{kind} '{name}' in {path.name} ignored, it is already defined elsewhere")
        else:
            table[name] = value

    def add_asset(self, path, root):
        names = self.entities_by_file.setdefault(path, [])
        for block in root.blocks("entity"):
            name = block.get("name")
            self.add(self.entity_blocks, "entity", name, path, (block, path))
            names.append(name)
        for block in root.blocks("animation"):
            file = block.get("file")
            if file:
                self.add(self.animations, "animation", block.get("name"), path, path.parent / file)

    def add_gfx(self, path, root):
        for types in root.blocks("objectTypes"):
            for block in types.blocks("pdxmesh"):
                name = block.get("name")
                self.add(self.pdxmeshes, "pdxmesh", name, path, PdxMesh(name, path, block))

    def resolve_file(self, relative):
        for root in self.roots:
            candidate = root / relative
            if candidate.is_file():
                return candidate
        return None

    def shader_info(self, name):
        if self._shaders is None:
            texts = []
            for root in self.roots:
                folder = root / "gfx" / "FX"
                for pattern in ("*.shader", "*.fxh"):
                    texts += [p.read_text(encoding="utf-8-sig", errors="replace") for p in sorted(folder.glob(pattern))]
            self._shaders = read_shader_effects(texts)
        return self._shaders.get(name)

    def find_texture(self, name, near=None):
        if near is not None and (Path(near) / name).is_file():
            return Path(near) / name
        if self._textures is None:
            self._textures = {}
            for root in self.roots:
                for path in sorted((root / MODEL_FOLDER[0]).rglob("*")):
                    if path.suffix.lower() in TEXTURE_EXTENSIONS:
                        self._textures.setdefault(path.name.lower(), path)
        return self._textures.get(Path(name).name.lower())

    def entity(self, name):
        if name in self._entities:
            return self._entities[name]
        chain, seen, link = [], set(), name
        while link is not None and link in self.entity_blocks and link not in seen:
            seen.add(link)
            chain.append(link)
            link = self.entity_blocks[link][0].get("clone")
        merged = None
        if chain:
            merged = Entity(name, self.entity_blocks[name][1])
            for link in reversed(chain):
                self.apply(merged, *self.entity_blocks[link])
        self._entities[name] = merged
        return merged

    def locator_source(self, name, locator):
        seen, link = set(), name
        while link is not None and link in self.entity_blocks and link not in seen:
            seen.add(link)
            block, path = self.entity_blocks[link]
            if any(loc.get("name") == locator for loc in block.blocks("locator")):
                return link, path
            link = block.get("clone")
        return None

    def setting_source(self, name, key):
        seen, link = set(), name
        while link is not None and link in self.entity_blocks and link not in seen:
            seen.add(link)
            block, path = self.entity_blocks[link]
            if block.get(key) is not None:
                return link, path
            link = block.get("clone")
        return None

    @staticmethod
    def apply(entity, block, source):
        if block.get("pdxmesh") is not None:
            entity.pdxmesh = block.get("pdxmesh")
        if to_float(block.get("scale")) is not None:
            entity.scale = to_float(block.get("scale"))
        if block.get("default_state") is not None:
            entity.default_state = block.get("default_state")
        for loc in block.blocks("locator"):
            name = loc.get("name")
            if name:
                entity.locators[name] = Locator(
                    name,
                    vector(loc, "position", (0.0, 0.0, 0.0)),
                    vector(loc, "rotation", (0.0, 0.0, 0.0)),
                    to_float(loc.get("scale"), 1.0),
                    loc.get("parent_joint"),
                )
        ordinal = 0
        for attach in block.blocks("attach"):
            for k, v in attach:
                if k is not None and isinstance(v, str):
                    entity.attach.append((k, v))
                    entity.attach_sources.append((block.get("name"), ordinal))
                    ordinal += 1
        entity.meshsettings.extend(block.blocks("meshsettings"))
        for state in block.blocks("state"):
            if state.get("name"):
                entity.states.append((state.get("name"), state.get("animation")))

    def tree_size(self, name):
        if self.entity(name) is None:
            return (0, 0, 0)
        order, state = [], {}
        stack = [(name, iter(self.children(name)))]
        state[name] = 1
        while stack:
            current, children = stack[-1]
            for child in children:
                if child not in state:
                    state[child] = 1
                    stack.append((child, iter(self.children(child))))
                    break
            else:
                state[current] = 2
                order.append(current)
                stack.pop()
        counts = {}
        for current in order:
            entity = self.entity(current)
            instances, meshes, depth = 1, 1 if entity and entity.pdxmesh else 0, 1
            for child in self.children(current):
                if child in counts:
                    child_instances, child_meshes, child_depth = counts[child]
                    instances += child_instances
                    meshes += child_meshes
                    depth = max(depth, child_depth + 1)
            counts[current] = (instances, meshes, depth)
        return counts[name]

    def children(self, name):
        entity = self.entity(name)
        return [child for _, child in entity.attach if self.entity(child)] if entity else []

    def root_entities(self, path):
        names = [n for n in self.entities_by_file.get(Path(path).resolve(), []) if n]
        used = set()
        for n in names:
            block = self.entity_blocks[n][0]
            used.add(block.get("clone"))
            for attach in block.blocks("attach"):
                used.update(v for _, v in attach if isinstance(v, str))
        return [n for n in names if n not in used]
