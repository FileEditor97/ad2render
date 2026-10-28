"""VMT material lookup and VTF texture decoding (via Pillow)."""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass

from PIL import Image

_KV = re.compile(r'"?(\$?[\w%]+)"?\s+"([^"]*)"|"?(\$?[\w%]+)"?\s+([^\s"{}]+)')


@dataclass
class Material:
    path: str                    # "materials/hunter/myplastic.vmt"
    shader: str = ""
    basetexture: str | None = None
    translucent: bool = False
    alphatest: bool = False
    nocull: bool = False
    wireframe: bool = False      # the "Wireframe" shader (models/wireframe): draw the prop's edges only
    color: tuple = (1.0, 1.0, 1.0)


def parse_vmt(text: str, path: str = "") -> Material:
    text = re.sub(r"//[^\n]*", "", text)
    m = re.match(r'\s*"?([\w]+)"?', text)
    mat = Material(path, shader=(m.group(1).lower() if m else ""))
    params = {}
    for k1, v1, k2, v2 in _KV.findall(text):
        k, v = (k1, v1) if k1 else (k2, v2)
        params.setdefault(k.lower(), v)
    mat.basetexture = params.get("$basetexture")
    mat.translucent = params.get("$translucent", "0") not in ("0", "")
    mat.alphatest = params.get("$alphatest", "0") not in ("0", "")
    mat.nocull = params.get("$nocull", "0") not in ("0", "")
    mat.wireframe = mat.shader == "wireframe" or params.get("$wireframe", "0") not in ("0", "")
    col = params.get("$color2") or params.get("$color")
    if col:
        nums = re.findall(r"[-\d.]+", col)
        if len(nums) >= 3:
            vals = [float(n) for n in nums[:3]]
            if "{" in col:
                vals = [v / 255 for v in vals]
            mat.color = tuple(vals)
    mat.include = params.get("include")
    return mat


def find_material(fs, name: str, dirs: list[str]) -> Material | None:
    """Resolve a model texture name against its $cdmaterials dirs (or an absolute override)."""
    name = name.replace("\\", "/").lstrip("/")
    cands = [d.rstrip("/") + "/" + name for d in dirs] + [name]
    for c in cands:
        c = re.sub(r"/+", "/", c).lower()
        p = f"materials/{c}.vmt"
        data = fs.read(p)
        if data is None:
            continue
        mat = parse_vmt(data.decode("utf-8", "replace"), p)
        if mat.shader == "patch" and getattr(mat, "include", None):
            base = fs.read(mat.include)
            if base is not None:
                inc = parse_vmt(base.decode("utf-8", "replace"), mat.include)
                patch = mat
                mat = inc
                mat.path = p
                mat.basetexture = patch.basetexture or inc.basetexture
                mat.translucent |= patch.translucent
                mat.alphatest |= patch.alphatest
        return mat
    return None


# VTF -------------------------------------------------------------------------

# format id -> (bits per pixel, pillow raw mode, decoder)
_FORMATS = {
    0: (32, "RGBA", None), 1: (32, "ABGR", None), 2: (24, "RGB", None), 3: (24, "BGR", None),
    4: (16, "RGB;16", None), 5: (8, "L", None), 6: (16, "LA", None), 8: (8, "A", None),
    11: (32, "ARGB", None), 12: (32, "BGRA", None), 13: (4, None, 1), 14: (8, None, 2),
    15: (8, None, 3), 16: (32, "BGRX", None), 17: (16, "BGR;16", None), 20: (4, None, 1),
}


def _mip_size(fmt, w, h):
    bpp = _FORMATS[fmt][0]
    if _FORMATS[fmt][2]:
        return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * (8 if fmt in (13, 20) else 16)
    return w * h * bpp // 8


def decode_vtf(data: bytes) -> Image.Image:
    if data[:4] != b"VTF\0":
        raise ValueError("not a VTF")
    vmaj, vmin, hdr = struct.unpack_from("<III", data, 4)
    w, h, flags, frames, _first = struct.unpack_from("<HHIHH", data, 16)
    fmt, mips, lfmt, lw, lh = struct.unpack_from("<iBiBB", data, 52)
    depth = struct.unpack_from("<H", data, 63)[0] if vmin >= 2 else 1
    depth = max(depth, 1)
    if fmt not in _FORMATS:
        raise ValueError(f"unsupported VTF format {fmt}")
    faces = 6 if flags & 0x4000 else 1
    if faces == 6 and vmin < 5 and _first == 0xFFFF:
        faces = 7
    if vmin >= 3:
        nres = struct.unpack_from("<I", data, 68)[0]
        start = None
        for r in range(nres):
            tag, off = struct.unpack_from("<3sxI", data, 80 + r * 8)
            if tag == b"\x30\0\0":
                start = off
        if start is None:
            raise ValueError("VTF has no high-res image resource")
    else:
        low = _mip_size(lfmt, lw, lh) if lfmt in _FORMATS and lw else 0
        start = hdr + low
    off = start
    for m in range(mips - 1, 0, -1):
        mw, mh = max(1, w >> m), max(1, h >> m)
        off += _mip_size(fmt, mw, mh) * frames * faces * max(1, depth >> m)
    size = _mip_size(fmt, w, h)
    raw = data[off:off + size]
    if len(raw) < size:
        raise ValueError("VTF truncated")
    bpp, mode, bcn = _FORMATS[fmt]
    if bcn:
        img = Image.frombytes("RGBA", (w, h), raw, "bcn", bcn)
    elif mode in ("L", "A"):
        img = Image.frombytes("L", (w, h), raw).convert("RGBA")
    elif mode == "LA":
        img = Image.frombytes("LA", (w, h), raw).convert("RGBA")
    else:
        out = "RGBA" if mode in ("RGBA", "ABGR", "ARGB", "BGRA") else "RGB"
        img = Image.frombytes(out, (w, h), raw, "raw", mode).convert("RGBA")
    if fmt == 13:  # plain DXT1 carries no alpha
        img.putalpha(255)
    return img


def load_texture(fs, name: str) -> Image.Image | None:
    p = "materials/" + name.replace("\\", "/").lstrip("/").lower()
    if not p.endswith(".vtf"):
        p += ".vtf"
    data = fs.read(p)
    if data is None:
        return None
    try:
        return decode_vtf(data)
    except (ValueError, struct.error, OSError):
        return None
