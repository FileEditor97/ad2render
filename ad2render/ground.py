"""Ground plane under a build: level from the dupe, surface from the game's own materials."""
from __future__ import annotations

import io
import json
import re

from PIL import Image

from .material import find_material, load_texture

# kind -> game material (the gm_construct ones are what players know from gm_flatgrass / gm_construct)
GROUND_MATERIALS = {
    "grass": "gm_construct/flatgrass",
    "concrete": "gm_construct/construct_concrete_ground",
    "dirt": "nature/dirtfloor003a",
}
GROUND_KINDS = ("none", "solid", *GROUND_MATERIALS)
TEXEL_SCALE = 0.25          # Hammer's default texture scale: units per texel
MAX_GROUND_TEX = 1024
EXTENT_FACTOR = 3.0         # ground side = this many times the build's footprint (plus a margin)


def ground_level(scene: dict) -> float:
    """Ground height in dupe space (inches).

    AdvDupe2 stores HeadEnt.Z: the height of the head prop above the surface under it when the dupe was
    copied. Positions are relative to the head prop, so the ground is at -Z. Props that poke below it (a
    console sunk into the floor) don't lift the build off the ground. Z is 0 when the copy trace hit
    nothing; then fall back to the lowest point of the build."""
    z = scene.get("head", {}).get("z")
    if isinstance(z, (int, float)) and z > 0:
        return -float(z)
    lows = [p[2] for p in _corners(scene)]
    return min(lows) if lows else 0.0


def footprint(scene: dict) -> tuple[float, float, float]:
    """(centre x, centre y, side) in inches for a square ground under the build."""
    pts = _corners(scene)
    if not pts:
        return 0.0, 0.0, 512.0
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    size = max(max(xs) - min(xs), max(ys) - min(ys))
    return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, size * EXTENT_FACTOR + 256.0


def _corners(scene):
    out = []
    for e in scene["entities"]:
        if not e.get("bounds"):
            continue
        (lo, hi), m = e["bounds"], e["parts"][0]["matrix"]
        for c in ((x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])):
            out.append([sum(m[i][k] * c[k] for k in range(3)) + m[i][3] for i in range(3)])
    return out


def ground_texture(conv, kind: str) -> dict | None:
    """Cached PNG for a ground kind: {"png": path, "tile": inches per texture repeat, "avg": [r, g, b]}."""
    if kind not in GROUND_MATERIALS:
        return None
    from .convert import CONVERTER_VERSION, _h
    path = conv.cache / "materials" / f"{_h('ground', kind, CONVERTER_VERSION)}.png"
    meta_path = path.with_suffix(".json")
    if path.exists() and meta_path.exists() and not conv.force:
        return json.loads(meta_path.read_text())
    mat = find_material(conv.fs, GROUND_MATERIALS[kind], [])
    img = load_texture(conv.fs, mat.basetexture) if mat and mat.basetexture else None
    if img is None:
        return None
    tile = img.size[0] * TEXEL_SCALE
    img = img.convert("RGB")
    if max(img.size) > MAX_GROUND_TEX:
        img.thumbnail((MAX_GROUND_TEX, MAX_GROUND_TEX), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=3)
    path.write_bytes(buf.getvalue())
    meta = {"png": str(path), "tile": tile, "avg": list(img.resize((1, 1), Image.BOX).getpixel((0, 0)))}
    meta_path.write_text(json.dumps(meta))
    return meta


def parse_color(s: str) -> list[int]:
    if not re.fullmatch(r"#?[0-9a-fA-F]{6}", s or ""):
        raise ValueError("colour must be #rrggbb")
    s = s.lstrip("#")
    return [int(s[i:i + 2], 16) for i in (0, 2, 4)]


def ground_spec(scene: dict, conv, kind: str, color: str = "#6e6e6e") -> dict | None:
    """Everything a renderer needs to draw the ground, or None for no ground."""
    if kind in (None, "none", "grid"):
        return None
    if kind not in GROUND_KINDS:
        raise ValueError(f"ground must be one of {', '.join(GROUND_KINDS)}")
    cx, cy, side = footprint(scene)
    spec = {"kind": kind, "z": ground_level(scene), "center": [cx, cy], "size": side}
    if kind == "solid":
        spec["color"] = parse_color(color)
    else:
        tex = ground_texture(conv, kind)
        if tex is None:  # material missing from this install: plain colour instead of failing the render
            spec.update(kind="solid", color=[110, 110, 110])
        else:
            spec.update(texture=tex["png"], tile=tex["tile"], color=tex["avg"])
    return spec
