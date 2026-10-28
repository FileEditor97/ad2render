"""Minimal Source engine studio model reader (MDL v44-49 + VVD + DX90 VTX).

Reads LOD0 geometry in the reference pose, honouring the skin table and bodygroups.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field


class ModelError(Exception):
    pass


@dataclass
class Mesh:
    texture: str            # material name without directory, e.g. "plate"
    positions: list         # [(x, y, z)] Source model space, inches
    normals: list
    uvs: list
    indices: list           # flat triangle list
    bones: list = field(default_factory=list)   # [((bone, ...), (weight, ...))] per vertex


@dataclass
class Model:
    name: str
    version: int
    hull: tuple             # ((minx, miny, minz), (maxx, maxy, maxz))
    view_bb: tuple
    cdmaterials: list
    textures: list
    skins: list             # [[texture index per skinref]]
    bodyparts: list         # [(name, [model_struct_offset, ...])]
    bone_names: list
    bone_parents: list
    num_bones: int
    pose_to_bone: list = field(default_factory=list)   # 3x4 row-major inverse bind matrices


def _s(buf, off):
    end = buf.find(b"\0", off)
    return buf[off:end if end >= 0 else len(buf)].decode("utf-8", "replace")


def _i(buf, off):
    return struct.unpack_from("<i", buf, off)[0]


def read_header(mdl: bytes) -> Model:
    if len(mdl) < 240 or mdl[:4] != b"IDST":
        raise ModelError("not an MDL file")
    ver = _i(mdl, 4)
    if not 44 <= ver <= 49:
        raise ModelError(f"unsupported MDL version {ver}")
    v = struct.unpack_from("<12f", mdl, 104)
    hull = (v[0:3], v[3:6])
    view = (v[6:9], v[9:12])
    num_bones, bone_index = _i(mdl, 156), _i(mdl, 160)
    num_tex, tex_index = _i(mdl, 204), _i(mdl, 208)
    num_cd, cd_index = _i(mdl, 212), _i(mdl, 216)
    num_ref, num_fam, skin_index = _i(mdl, 220), _i(mdl, 224), _i(mdl, 228)
    num_bp, bp_index = _i(mdl, 232), _i(mdl, 236)

    textures = []
    for t in range(num_tex):
        o = tex_index + t * 64
        textures.append(_s(mdl, o + _i(mdl, o)).replace("\\", "/"))
    cds = [_s(mdl, _i(mdl, cd_index + i * 4)).replace("\\", "/") for i in range(num_cd)]
    skins = []
    for f in range(num_fam):
        skins.append(list(struct.unpack_from(f"<{num_ref}h", mdl, skin_index + f * num_ref * 2)))
    bodyparts = []
    for b in range(num_bp):
        o = bp_index + b * 16
        name_off, nmodels, _base, model_index = struct.unpack_from("<iiii", mdl, o)
        bodyparts.append((_s(mdl, o + name_off), [o + model_index + m * 148 for m in range(nmodels)]))
    bone_names, bone_parents, pose = [], [], []
    for b in range(num_bones):
        o = bone_index + b * 216
        bone_names.append(_s(mdl, o + _i(mdl, o)))
        bone_parents.append(_i(mdl, o + 4))
        m = struct.unpack_from("<12f", mdl, o + 96)
        pose.append([list(m[0:4]), list(m[4:8]), list(m[8:12])])
    return Model(_s(mdl, 12), ver, hull, view, cds, textures, skins, bodyparts,
                 bone_names, bone_parents, num_bones, pose)


def _vvd_vertices(vvd: bytes):
    if vvd[:4] != b"IDSV":
        raise ModelError("not a VVD file")
    (_ver, _ck, _nlods) = struct.unpack_from("<iii", vvd, 4)
    lod0 = _i(vvd, 16)
    num_fix, fix_start, vert_start = struct.unpack_from("<iii", vvd, 48)

    def vert(i):
        o = vert_start + i * 48
        w = struct.unpack_from("<3f3BB", vvd, o)
        p = struct.unpack_from("<8f", vvd, o + 16)
        nb = max(1, min(3, w[6]))
        return (p[0:3], p[3:6], (p[6], p[7]), (tuple(w[3:3 + nb]), tuple(w[0:nb])))

    if num_fix == 0:
        return [vert(i) for i in range(lod0)]
    out = []
    for f in range(num_fix):
        lod, src, n = struct.unpack_from("<iii", vvd, fix_start + f * 12)
        if lod >= 0:
            out.extend(vert(src + k) for k in range(n))
    return out


def load_meshes(model: Model, mdl: bytes, vvd: bytes, vtx: bytes,
                skin: int = 0, bodygroups: dict | None = None) -> list[Mesh]:
    verts = _vvd_vertices(vvd)
    bodygroups = bodygroups or {}
    skin_row = model.skins[skin] if 0 <= skin < len(model.skins) else (model.skins[0] if model.skins else [])
    _ver, _cache, _mbs, _mbt, _mbv, _ck, _nlods, _mro, nbp, bp_off = struct.unpack_from("<iiHHiiiiii", vtx, 0)
    if nbp != len(model.bodyparts):
        raise ModelError("VTX/MDL bodypart count mismatch")

    last_err = None
    for sg_size in ((25, 33) if model.version >= 49 else (25,)):
        try:
            return _build(model, mdl, vtx, verts, skin_row, bodygroups, bp_off, sg_size)
        except (ModelError, struct.error, IndexError) as e:
            last_err = e
    raise ModelError(f"failed to parse VTX: {last_err}")


def _build(model, mdl, vtx, verts, skin_row, bodygroups, bp_off, sg_size):
    meshes: list[Mesh] = []
    for bp, (_name, model_offsets) in enumerate(model.bodyparts):
        if not model_offsets:
            continue
        choice = int(bodygroups.get(bp, 0) or 0)
        if not 0 <= choice < len(model_offsets):
            choice = 0
        m_off = model_offsets[choice]
        nmeshes, mesh_index, _nverts, vertex_index = struct.unpack_from("<iiii", mdl, m_off + 72)
        vbase = vertex_index // 48

        bpo = bp_off + bp * 8
        nmodels, model_off = struct.unpack_from("<ii", vtx, bpo)
        if choice >= nmodels:
            raise ModelError("VTX model index out of range")
        mo = bpo + model_off + choice * 8
        nlods, lod_off = struct.unpack_from("<ii", vtx, mo)
        if nlods == 0:
            continue
        lo = mo + lod_off  # LOD0
        nvmeshes, vmesh_off, _sw = struct.unpack_from("<iif", vtx, lo)
        if nvmeshes != nmeshes:
            raise ModelError("VTX/MDL mesh count mismatch")
        for mi in range(nmeshes):
            mdl_mesh = m_off + mesh_index + mi * 116
            skinref, _mi, _nv, vert_offset = struct.unpack_from("<iiii", mdl, mdl_mesh)
            tex_i = skin_row[skinref] if skinref < len(skin_row) else skinref
            texture = model.textures[tex_i] if 0 <= tex_i < len(model.textures) else ""

            vmo = lo + vmesh_off + mi * 9
            nsg, sg_off = struct.unpack_from("<ii", vtx, vmo)
            pos, nrm, uv, bones, idx = [], [], [], [], []
            for g in range(nsg):
                so = vmo + sg_off + g * sg_size
                nv, v_off, ni, i_off = struct.unpack_from("<iiii", vtx, so)
                if nv < 0 or ni < 0 or ni % 3:
                    raise ModelError("bad strip group")
                base = len(pos)
                for k in range(nv):
                    orig = struct.unpack_from("<H", vtx, so + v_off + k * 9 + 4)[0]
                    p, n, t, b = verts[orig + vert_offset + vbase]
                    pos.append(p)
                    nrm.append(n)
                    uv.append(t)
                    bones.append(b)
                ind = struct.unpack_from(f"<{ni}H", vtx, so + i_off)
                if ind and max(ind) >= nv:
                    raise ModelError("strip group index out of range")
                idx.extend(base + i for i in ind)
            if idx:
                meshes.append(Mesh(texture, pos, nrm, uv, idx, bones))
    return meshes
