"""Rest pose of a studio model: frame 0 of its default sequence.

The engine draws props with their default sequence applied. Models compiled with a rotated root
(e.g. exported Y-up) keep the correcting rotation only in that animation, so the VVD bind-pose mesh is
turned relative to what players see and to the physics object that AdvDupe2 records. Skinning the
mesh with world[b] * poseToBone[b] for that frame gives the mesh exactly as it is drawn in game.
"""
from __future__ import annotations

import math
import struct

from .mdl import Mesh, Model, ModelError

_RAWPOS, _RAWROT, _ANIMPOS, _ANIMROT, _RAWROT2 = 0x01, 0x02, 0x04, 0x08, 0x20
_BONE_SIZE = 216
_ANIMDESC_SIZE = 100


def _i(b, o):
    return struct.unpack_from("<i", b, o)[0]


def _quat_matrix(q, p):
    x, y, z, w = q
    return [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w), p[0]],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w), p[1]],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y), p[2]],
            [0.0, 0.0, 0.0, 1.0]]


def _euler_quat(r):
    """Source AngleQuaternion(RadianEuler)."""
    sr, cr = math.sin(r[0] * 0.5), math.cos(r[0] * 0.5)
    sp, cp = math.sin(r[1] * 0.5), math.cos(r[1] * 0.5)
    sy, cy = math.sin(r[2] * 0.5), math.cos(r[2] * 0.5)
    return (sr * cp * cy - cr * sp * sy, cr * sp * cy + sr * cp * sy,
            cr * cp * sy - sr * sp * cy, cr * cp * cy + sr * sp * sy)


def _quat48(b, o):
    x, y, zw = struct.unpack_from("<HHH", b, o)
    qx, qy, qz = (x - 32768) / 32768.0, (y - 32768) / 32768.0, ((zw & 0x7FFF) - 16384) / 16384.0
    qw = math.sqrt(max(0.0, 1 - qx * qx - qy * qy - qz * qz))
    return qx, qy, qz, -qw if zw & 0x8000 else qw


def _quat64(b, o):
    v = struct.unpack_from("<Q", b, o)[0]
    qx = ((v & 0x1FFFFF) - 1048576) / 1048576.5
    qy = (((v >> 21) & 0x1FFFFF) - 1048576) / 1048576.5
    qz = (((v >> 42) & 0x1FFFFF) - 1048576) / 1048576.5
    qw = math.sqrt(max(0.0, 1 - qx * qx - qy * qy - qz * qz))
    return qx, qy, qz, -qw if v >> 63 else qw


def _frame0(b, valueptr):
    """Frame-0 values of an mstudioanim_valueptr_t (3 RLE channels); 0 where a channel is absent."""
    out = []
    for k in range(3):
        off = struct.unpack_from("<h", b, valueptr + k * 2)[0]
        out.append(struct.unpack_from("<h", b, valueptr + off + 2)[0] if off > 0 else 0)
    return out


def _mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def bone_world_matrices(model: Model, mdl: bytes) -> list:
    """World matrix of every bone at frame 0 of sequence 0 (bone defaults where data is missing)."""
    nb = model.num_bones
    bone_index = _i(mdl, 160)
    num_anim, anim_index, num_seq, seq_index = _i(mdl, 180), _i(mdl, 184), _i(mdl, 188), _i(mdl, 192)
    local = []
    for bi in range(nb):
        o = bone_index + bi * _BONE_SIZE
        local.append([struct.unpack_from("<4f", mdl, o + 44), struct.unpack_from("<3f", mdl, o + 32)])
    try:
        if num_seq <= 0 or num_anim <= 0:
            raise ModelError("no sequences")
        anim = struct.unpack_from("<h", mdl, seq_index + _i(mdl, seq_index + 60))[0]  # seq 0, blend (0,0)
        if not 0 <= anim < num_anim:
            raise ModelError("bad animation index")
        desc = anim_index + anim * _ANIMDESC_SIZE
        if _i(mdl, desc + 52) != 0:
            raise ModelError("animation is in an external .ani block")
        data = desc + _i(mdl, desc + 56)
        if _i(mdl, desc + 84):  # sectionframes: frame 0 lives in section 0
            data = desc + _i(mdl, desc + _i(mdl, desc + 80) + 4)
        for _ in range(nb):
            bone, flags, nxt = struct.unpack_from("<BBh", mdl, data)
            if bone >= nb:
                break
            o = data + 4
            bo = bone_index + bone * _BONE_SIZE
            if flags & _RAWROT:
                local[bone][0] = _quat48(mdl, o)
            elif flags & _RAWROT2:
                local[bone][0] = _quat64(mdl, o)
            elif flags & _ANIMROT:
                v = _frame0(mdl, o)
                rot = struct.unpack_from("<3f", mdl, bo + 60)
                scale = struct.unpack_from("<3f", mdl, bo + 84)
                local[bone][0] = _euler_quat([rot[k] + v[k] * scale[k] for k in range(3)])
            if flags & _RAWPOS:
                po = o + (6 if flags & _RAWROT else 0) + (8 if flags & _RAWROT2 else 0)
                local[bone][1] = tuple(float(h) for h in struct.unpack_from("<3e", mdl, po))
            elif flags & _ANIMPOS:
                v = _frame0(mdl, o + (6 if flags & _ANIMROT else 0))
                pos = struct.unpack_from("<3f", mdl, bo + 32)
                scale = struct.unpack_from("<3f", mdl, bo + 72)
                local[bone][1] = tuple(pos[k] + v[k] * scale[k] for k in range(3))
            if nxt <= 0:
                break
            data += nxt
    except (ModelError, struct.error, IndexError):
        pass  # bone defaults; for most models they equal the bind pose
    world = []
    for bi in range(nb):
        m = _quat_matrix(*local[bi])
        par = model.bone_parents[bi]
        world.append(_mul(world[par], m) if 0 <= par < bi else m)
    return world


def apply_rest_pose(model: Model, mdl: bytes, meshes: list[Mesh]) -> bool:
    """Skin meshes into their frame-0 pose in place. Returns False when that equals the bind pose."""
    if model.num_bones == 0:
        return False
    world = bone_world_matrices(model, mdl)
    skin = [_mul(world[b], model.pose_to_bone[b] + [[0.0, 0.0, 0.0, 1.0]]) for b in range(model.num_bones)]
    ident = ((1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0))
    if all(abs(s[i][j] - ident[i][j]) < 1e-4 for s in skin for i in range(3) for j in range(4)):
        return False
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
    return True
