"""Model conversion (MDL -> cached .glb) and asset resolution for a scene.

The cache lives outside the repository (default %LOCALAPPDATA%/ad2render/cache) because it
holds converted Valve / workshop content that must never be redistributed.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import struct
from pathlib import Path

from PIL import Image

from . import gltf
from .gltf import Prim
from .material import find_material, load_texture
from .mdl import ModelError, load_meshes, read_header
from .pose import apply_rest_pose

CONVERTER_VERSION = 4  # 4: meshes posed with their default sequence
MAX_TEX = 512


def default_cache_dir() -> Path:
    if env := os.environ.get("AD2RENDER_CACHE"):
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "ad2render" / "cache"


def _h(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:20]


def _png(img: Image.Image) -> bytes:
    if max(img.size) > MAX_TEX:
        img = img.copy()
        img.thumbnail((MAX_TEX, MAX_TEX), Image.LANCZOS)
    b = io.BytesIO()
    img.save(b, "PNG", optimize=False, compress_level=3)
    return b.getvalue()


def _avg(img: Image.Image) -> list[int]:
    return list(img.convert("RGBA").resize((1, 1), Image.BOX).getpixel((0, 0)))


class Converter:
    def __init__(self, fs, cache_dir: Path | None = None, force: bool = False):
        self.fs = fs
        self.cache = Path(cache_dir) if cache_dir else default_cache_dir()
        (self.cache / "models").mkdir(parents=True, exist_ok=True)
        (self.cache / "materials").mkdir(parents=True, exist_ok=True)
        self.force = force
        self._mat_memo: dict = {}

    def forget_materials(self):
        """Drop remembered material lookups (including "not found"): call after new content was mounted."""
        self._mat_memo.clear()

    # Materials ------------------------------------------------------------
    def material(self, name: str, dirs: list[str]) -> dict:
        key = (name.lower(), tuple(d.lower() for d in dirs))
        if key in self._mat_memo:
            return self._mat_memo[key]
        mat = find_material(self.fs, name, dirs)
        out = {"name": name, "png": None, "alpha": "OPAQUE", "avg": [200, 200, 200, 255],
               "double_sided": False, "wireframe": False, "found": mat is not None}
        if mat:
            out["double_sided"] = mat.nocull
            out["wireframe"] = mat.wireframe
            img = load_texture(self.fs, mat.basetexture) if mat.basetexture else None
            if img is None and mat.basetexture:
                out["texture_error"] = f"texture '{mat.basetexture}' missing or unsupported"
            if img is not None:
                out["png"] = _png(img)
                out["avg"] = _avg(img)
                if mat.translucent:
                    out["alpha"] = "BLEND"
                elif mat.alphatest:
                    out["alpha"] = "MASK"
            if mat.color != (1.0, 1.0, 1.0):
                out["avg"] = [int(c * a) for c, a in zip(mat.color, out["avg"])] + out["avg"][3:]
            if not out["png"] and mat.shader in ("unlitgeneric", "vertexlitgeneric") and not mat.basetexture:
                out["avg"] = [255, 255, 255, 255]
            if not mat.translucent:
                out["avg"][3] = 255
        self._mat_memo[key] = out
        return out

    def override_texture(self, override: str) -> dict | None:
        """Resolve an EntityMods material override to a cached PNG."""
        m = self.material(override, [])
        if not m["found"]:
            return None
        path = self.cache / "materials" / (_h(override.lower(), CONVERTER_VERSION) + ".png")
        if m["png"] and (self.force or not path.exists()):
            path.write_bytes(m["png"])
        return {"png": str(path) if m["png"] else None, "avg": m["avg"], "alpha": m["alpha"],
                "wireframe": m["wireframe"]}

    # Models ---------------------------------------------------------------
    def model(self, model: str, skin: int = 0, bodygroups: dict | None = None,
              pose: list | None = None) -> dict:
        """Convert a model to .glb. With `pose` (list of 4x4 per physobj) the mesh is skinned
        into dupe space and the result is not shared between entities."""
        bg = {int(k): int(v) for k, v in (bodygroups or {}).items()}
        key = _h(model.lower(), skin, sorted(bg.items()), json.dumps(pose) if pose else "", CONVERTER_VERSION)
        out = self.cache / "models" / f"{key}.glb"
        meta_path = out.with_suffix(".json")
        if not self.force and out.exists() and meta_path.exists():
            meta = json.loads(meta_path.read_text())
            # a placeholder may be stale: the user could have installed the addon since
            if not meta["placeholder"] or not self.fs.exists(model):
                return meta
        meta = {"model": model, "glb": str(out), "placeholder": False, "warnings": []}
        try:
            prims, bounds = self._convert(model, skin, bg, pose, meta)
        except (ModelError, ValueError, KeyError, IndexError, OSError, struct.error, MemoryError) as e:
            prims, bounds = None, None
            meta["warnings"].append(f"{model}: {e}")
        if not prims:
            meta["placeholder"] = True
            prims, bounds = _placeholder(self.fs, model, meta)
        meta["bounds"] = bounds
        gltf.write_glb(out, prims, {"model": model, "placeholder": meta["placeholder"]})
        meta_path.write_text(json.dumps(meta))
        return meta

    def _convert(self, model, skin, bg, pose, meta):
        mdl = self.fs.read(model)
        if mdl is None:
            raise ModelError("model not found")
        base = model[:-4] if model.lower().endswith(".mdl") else model
        vvd = self.fs.read(base + ".vvd")
        vtx = next((v for v in (self.fs.read(base + s) for s in (".dx90.vtx", ".vtx", ".dx80.vtx", ".sw.vtx"))
                    if v is not None), None)
        if vvd is None or vtx is None:
            raise ModelError("missing .vvd/.vtx")
        hdr = read_header(mdl)
        meshes = load_meshes(hdr, mdl, vvd, vtx, skin, bg)
        if pose and len(pose) > 1 and hdr.num_bones > 1:
            _skin_meshes(hdr, meshes, pose, _phy_bones(self.fs.read(base + ".phy")))
        else:
            # draw the model as the game does: its default sequence, not the raw bind pose
            apply_rest_pose(hdr, mdl, meshes)
        prims = []
        for m in meshes:
            mat = self.material(m.texture, hdr.cdmaterials)
            if not mat["found"]:
                meta["warnings"].append(f"{model}: material '{m.texture}' not found")
            elif mat.get("texture_error"):
                meta["warnings"].append(f"{model}: material '{m.texture}': {mat['texture_error']}")
            prims.append(Prim(m.positions, m.normals, m.uvs, m.indices, mat))
        pts = [p for m in meshes for p in m.positions]
        bounds = [[min(p[i] for p in pts) for i in range(3)], [max(p[i] for p in pts) for i in range(3)]]
        return prims, bounds


def _placeholder(fs, model, meta):
    """Box from the MDL hull if the .mdl is readable, otherwise a small marker cube."""
    lo, hi = (-6.0, -6.0, -6.0), (6.0, 6.0, 6.0)
    data = fs.read(model)
    if data:
        try:
            h = read_header(data)
            a, b = h.hull if any(h.hull[1]) else h.view_bb
            if all(b[i] > a[i] for i in range(3)):
                lo, hi = a, b
        except (ModelError, struct.error):
            pass
    else:
        meta["warnings"].append(f"{model}: model not found, drawing marker")
    corners = [(x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
    faces = [((0, 1, 3, 2), (-1, 0, 0)), ((4, 6, 7, 5), (1, 0, 0)), ((0, 4, 5, 1), (0, -1, 0)),
             ((2, 3, 7, 6), (0, 1, 0)), ((0, 2, 6, 4), (0, 0, -1)), ((1, 5, 7, 3), (0, 0, 1))]
    pos, nrm, uv, idx = [], [], [], []
    for quad, n in faces:
        b = len(pos)
        for c, t in zip(quad, ((0, 0), (1, 0), (1, 1), (0, 1))):
            pos.append(corners[c])
            nrm.append(n)
            uv.append(t)
        # clockwise, matching what the MDL path produces
        idx += [b, b + 2, b + 1, b, b + 3, b + 2]
    mat = {"name": "__placeholder", "png": None, "alpha": "BLEND", "avg": [255, 0, 255, 140],
           "double_sided": True}
    return [Prim(pos, nrm, uv, idx, mat)], [list(lo), list(hi)]


# Ragdoll skinning ------------------------------------------------------------

def _phy_bones(phy: bytes | None) -> dict[int, str]:
    if not phy:
        return {}
    text = phy.decode("latin-1")
    out = {}
    for m in re.finditer(r'solid\s*\{(.*?)\}', text, re.S):
        idx = re.search(r'"index"\s*"(\d+)"', m.group(1))
        name = re.search(r'"name"\s*"([^"]+)"', m.group(1))
        if idx and name:
            out[int(idx.group(1))] = name.group(1).lower()
    return out


def _mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _inv_rigid(m):
    r = [[m[j][i] for j in range(3)] for i in range(3)]  # transpose of rotation
    t = [-sum(r[i][k] * m[k][3] for k in range(3)) for i in range(3)]
    return [r[0] + [t[0]], r[1] + [t[1]], r[2] + [t[2]], [0, 0, 0, 1]]


def _skin_meshes(hdr, meshes, pose, phy_bones):
    p2b = [m + [[0, 0, 0, 1]] for m in hdr.pose_to_bone]
    bind = [_inv_rigid(m) for m in p2b]
    by_name = {n.lower(): i for i, n in enumerate(hdr.bone_names)}
    phys_for_bone = {}
    for pi, bname in phy_bones.items():
        if bname in by_name and pi < len(pose) and pose[pi] is not None:
            phys_for_bone[by_name[bname]] = pose[pi]
    world = [None] * hdr.num_bones
    for b in range(hdr.num_bones):  # parents always precede children
        if b in phys_for_bone:
            world[b] = phys_for_bone[b]
        else:
            par = hdr.bone_parents[b]
            if par < 0 or world[par] is None:
                world[b] = _mul(pose[0], bind[b])
            else:
                world[b] = _mul(world[par], _mul(_inv_rigid(bind[par]), bind[b]))
    skin = [_mul(world[b], p2b[b]) for b in range(hdr.num_bones)]
    for m in meshes:
        for vi, (bones, weights) in enumerate(m.bones):
            x, y, z = m.positions[vi]
            nx, ny, nz = m.normals[vi]
            px = py = pz = qx = qy = qz = 0.0
            tw = sum(weights) or 1.0
            for b, w in zip(bones, weights):
                s = skin[b] if b < len(skin) else skin[0]
                w /= tw
                px += w * (s[0][0] * x + s[0][1] * y + s[0][2] * z + s[0][3])
                py += w * (s[1][0] * x + s[1][1] * y + s[1][2] * z + s[1][3])
                pz += w * (s[2][0] * x + s[2][1] * y + s[2][2] * z + s[2][3])
                qx += w * (s[0][0] * nx + s[0][1] * ny + s[0][2] * nz)
                qy += w * (s[1][0] * nx + s[1][1] * ny + s[1][2] * nz)
                qz += w * (s[2][0] * nx + s[2][1] * ny + s[2][2] * nz)
            m.positions[vi] = (px, py, pz)
            m.normals[vi] = (qx, qy, qz)


# Scene-level resolution -------------------------------------------------------

def resolve_scene(scene: dict, conv: Converter, log=None) -> dict:
    """Attach cached glb paths / bounds to every entity and collect a missing-asset report."""
    report = {"missing_models": [], "missing_materials": [], "warnings": list(scene.get("warnings", []))}
    for ent in scene["entities"]:
        ragdoll = len(ent["parts"]) > 1
        pose = [p["matrix"] for p in ent["parts"]] if ragdoll else None
        meta = conv.model(ent["model"], ent["skin"], ent["bodygroups"], pose)
        ent["glb"] = meta["glb"]
        ent["bounds"] = meta["bounds"]
        ent["placeholder"] = meta["placeholder"]
        ent["posed"] = bool(pose) and not meta["placeholder"]
        if meta["placeholder"] and ent["model"] not in report["missing_models"]:
            report["missing_models"].append(ent["model"])
        for w in meta["warnings"]:
            if "material" in w and w not in report["missing_materials"]:
                report["missing_materials"].append(w)
            elif w not in report["warnings"] and "model not found" not in w:
                report["warnings"].append(w)
        if ent.get("material_override"):
            ov = conv.override_texture(ent["material_override"])
            ent["override"] = ov
            if ov is None or not (ov["png"] or ov["wireframe"]):
                msg = f"material override '{ent['material_override']}' not found or has no texture"
                if msg not in report["missing_materials"]:
                    report["missing_materials"].append(msg)
    from .zfight import resolve as resolve_zfight
    if n := resolve_zfight(scene):
        report["warnings"].append(f"{n} props overlap coplanar with other props; nudged to avoid z-fighting")
    from .ground import ground_level
    scene["ground_z"] = ground_level(scene)
    scene["report"] = report
    if log:
        for m in report["missing_models"]:
            log(f"warning: missing model {m} (placeholder drawn)")
    return scene
