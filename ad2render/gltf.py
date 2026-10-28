"""Tiny glTF 2.0 (.glb) writer and reader for the model cache.

Geometry is stored the glTF way (Y-up, metres): gltf = (x, z, -y) * INCH.
"""
from __future__ import annotations

import io
import json
import struct
from dataclasses import dataclass

INCH = 0.0254


@dataclass
class Prim:
    positions: list      # Source space, inches
    normals: list
    uvs: list
    indices: list
    material: dict       # {"name", "png": bytes|None, "alpha": "OPAQUE"|"MASK"|"BLEND", "avg": [r,g,b,a], "double_sided"}
    colors: list | None = None   # optional per-vertex (r, g, b, a), linear floats -> COLOR_0


def _src_to_gltf(v, s=1.0):
    return (v[0] * s, v[2] * s, -v[1] * s)


def write_glb(path, prims: list[Prim], extras: dict | None = None, node_name: str | None = None):
    bin_ = bytearray()
    views, accessors, meshes_prims, materials, images, textures = [], [], [], [], [], []

    def view(data: bytes, target=None):
        while len(bin_) % 4:
            bin_.append(0)
        v = {"buffer": 0, "byteOffset": len(bin_), "byteLength": len(data)}
        if target:
            v["target"] = target
        bin_.extend(data)
        views.append(v)
        return len(views) - 1

    def accessor(data: bytes, ctype, count, typ, target, mn=None, mx=None):
        a = {"bufferView": view(data, target), "componentType": ctype, "count": count, "type": typ}
        if mn is not None:
            a["min"], a["max"] = mn, mx
        accessors.append(a)
        return len(accessors) - 1

    mat_ids: dict[str, int] = {}
    for p in prims:
        m = p.material
        if m["name"] not in mat_ids:
            pbr = {"baseColorFactor": [1, 1, 1, 1], "metallicFactor": 0.0, "roughnessFactor": 0.6}
            if m.get("png"):
                images.append({"bufferView": view(m["png"]), "mimeType": "image/png"})
                textures.append({"source": len(images) - 1, "sampler": 0})
                pbr["baseColorTexture"] = {"index": len(textures) - 1}
            else:
                pbr["baseColorFactor"] = [c / 255 for c in m.get("avg", (200, 200, 200, 255))]
            mat = {"name": m["name"], "pbrMetallicRoughness": pbr, "alphaMode": m.get("alpha", "OPAQUE"),
                   "doubleSided": bool(m.get("double_sided")), "extras": {"avg": m.get("avg")}}
            if mat["alphaMode"] == "MASK":
                mat["alphaCutoff"] = 0.5
            materials.append(mat)
            mat_ids[m["name"]] = len(materials) - 1

        pos = [_src_to_gltf(v, INCH) for v in p.positions]
        nrm = [_src_to_gltf(v) for v in p.normals]
        mn = [min(v[i] for v in pos) for i in range(3)]
        mx = [max(v[i] for v in pos) for i in range(3)]
        n = len(pos)
        a_pos = accessor(struct.pack(f"<{3 * n}f", *[c for v in pos for c in v]), 5126, n, "VEC3", 34962, mn, mx)
        a_nrm = accessor(struct.pack(f"<{3 * n}f", *[c for v in nrm for c in v]), 5126, n, "VEC3", 34962)
        a_uv = accessor(struct.pack(f"<{2 * n}f", *[c for v in p.uvs for c in v]), 5126, n, "VEC2", 34962)
        # Source winds front faces clockwise; glTF expects counter-clockwise.
        idx = [i for t in range(0, len(p.indices), 3)
               for i in (p.indices[t], p.indices[t + 2], p.indices[t + 1])]
        big = n > 65535
        a_idx = accessor(struct.pack(f"<{len(idx)}{'I' if big else 'H'}", *idx), 5125 if big else 5123,
                         len(idx), "SCALAR", 34963)
        attrs = {"POSITION": a_pos, "NORMAL": a_nrm, "TEXCOORD_0": a_uv}
        if p.colors:
            attrs["COLOR_0"] = accessor(struct.pack(f"<{4 * n}f", *[c for v in p.colors for c in v]), 5126, n,
                                        "VEC4", 34962)
        meshes_prims.append({"attributes": attrs, "indices": a_idx, "material": mat_ids[m["name"]]})

    doc = {
        "asset": {"version": "2.0", "generator": "ad2render"},
        "scene": 0, "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": node_name or (extras or {}).get("model", "model")}],
        "meshes": [{"primitives": meshes_prims, "extras": extras or {}}],
        "materials": materials,
        "buffers": [{"byteLength": len(bin_)}],
        "bufferViews": views, "accessors": accessors,
    }
    if images:
        doc["images"], doc["textures"] = images, textures
        doc["samplers"] = [{"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}]
    js = json.dumps(doc, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    while len(bin_) % 4:
        bin_.append(0)
    out = io.BytesIO()
    out.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(bin_)))
    out.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
    out.write(struct.pack("<II", len(bin_), 0x004E4942) + bytes(bin_))
    with open(path, "wb") as f:
        f.write(out.getvalue())


def read_glb(path):
    """Return (doc, [(positions_src_inches, normals_src, indices, material_dict)])."""
    with open(path, "rb") as f:
        data = f.read()
    jl = struct.unpack_from("<I", data, 12)[0]
    doc = json.loads(data[20:20 + jl])
    bin_ = data[20 + jl + 8:]

    def acc(i):
        a = doc["accessors"][i]
        v = doc["bufferViews"][a["bufferView"]]
        width = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}[a["type"]]
        fmt = {5126: "f", 5123: "H", 5125: "I"}[a["componentType"]]
        vals = struct.unpack_from(f"<{a['count'] * width}{fmt}", bin_, v["byteOffset"])
        return [vals[k:k + width] for k in range(0, len(vals), width)] if width > 1 else list(vals)

    prims = []
    for p in doc["meshes"][0]["primitives"]:
        pos = [(x / INCH, -z / INCH, y / INCH) for x, y, z in acc(p["attributes"]["POSITION"])]
        nrm = [(x, -z, y) for x, y, z in acc(p["attributes"]["NORMAL"])]
        prims.append((pos, nrm, acc(p["indices"]), doc["materials"][p["material"]]))
    return doc, prims


def read_prims(path) -> list[Prim]:
    """Read a cached model back as full Prims (Source space, with UVs, texture bytes and clockwise winding)."""
    with open(path, "rb") as f:
        data = f.read()
    jl = struct.unpack_from("<I", data, 12)[0]
    doc = json.loads(data[20:20 + jl])
    bin_ = data[20 + jl + 8:]

    def acc(i):
        a = doc["accessors"][i]
        v = doc["bufferViews"][a["bufferView"]]
        width = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}[a["type"]]
        fmt = {5126: "f", 5123: "H", 5125: "I"}[a["componentType"]]
        vals = struct.unpack_from(f"<{a['count'] * width}{fmt}", bin_, v["byteOffset"])
        return [tuple(vals[k:k + width]) for k in range(0, len(vals), width)] if width > 1 else list(vals)

    out = []
    for p in doc["meshes"][0]["primitives"]:
        at = p["attributes"]
        m = doc["materials"][p["material"]]
        png = None
        tex = m["pbrMetallicRoughness"].get("baseColorTexture")
        if tex is not None:
            v = doc["bufferViews"][doc["images"][doc["textures"][tex["index"]]["source"]]["bufferView"]]
            png = bin_[v["byteOffset"]:v["byteOffset"] + v["byteLength"]]
        idx = acc(p["indices"])
        idx = [i for t in range(0, len(idx), 3) for i in (idx[t], idx[t + 2], idx[t + 1])]  # back to clockwise
        out.append(Prim(
            [(x / INCH, -z / INCH, y / INCH) for x, y, z in acc(at["POSITION"])],
            [(x, -z, y) for x, y, z in acc(at["NORMAL"])],
            acc(at["TEXCOORD_0"]), idx,
            {"name": m["name"], "png": png, "alpha": m.get("alphaMode", "OPAQUE"),
             "avg": (m.get("extras") or {}).get("avg") or [200, 200, 200, 255],
             "double_sided": bool(m.get("doubleSided"))},
            acc(at["COLOR_0"]) if "COLOR_0" in at else None))
    return out
