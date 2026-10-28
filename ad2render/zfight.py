"""Deterministic resolution of z-fighting between coplanar props.

Builders often place props so that faces coincide exactly (a coloured panel flush with a wall block).
Renderers then flicker between the two materials depending on view and sample jitter, so a different
prop looks "wrong" in every render. For each coincident face pair with different looks we pick a
winner and grow it by a tiny absolute margin (Z_BIAS per level), baked into its transform so the web
every renderer agrees.

Winner: the prop with a deliberate look (non-white colour or a material override) beats a plain one;
otherwise the later-placed prop (higher entity id) wins.
"""
from __future__ import annotations

import itertools

Z_BIAS = 0.02          # inches (~0.5 mm) of growth per level
PLANE_EPS = 0.05       # inches: faces closer than this count as coplanar
MIN_OVERLAP = 0.1      # inches of shared extent needed within the face plane before faces can fight
MAX_LEVEL = 4


def _xf(m, p):
    return [m[i][0] * p[0] + m[i][1] * p[1] + m[i][2] * p[2] + m[i][3] for i in range(3)]


def _faces(ent):
    """(outward unit normal, plane offset) for the 6 faces of the model's bounds, in world space."""
    (lo, hi), m = ent["bounds"], ent["parts"][0]["matrix"]
    out = []
    for ax in range(3):
        scale = sum(m[i][ax] ** 2 for i in range(3)) ** 0.5 or 1.0
        for v, sign in ((lo[ax], -1.0), (hi[ax], 1.0)):  # outward normals: -axis for the low face
            n = [sign * m[i][ax] / scale for i in range(3)]
            c = [(lo[k] + hi[k]) / 2 if k != ax else v for k in range(3)]
            wc = _xf(m, c)
            out.append((n, sum(n[i] * wc[i] for i in range(3))))
    return out


def _world_box(ent):
    (lo, hi), m = ent["bounds"], ent["parts"][0]["matrix"]
    cs = [_xf(m, c) for c in itertools.product(*zip(lo, hi))]
    return [min(c[i] for c in cs) for i in range(3)], [max(c[i] for c in cs) for i in range(3)]


def _look(ent):
    return (ent.get("material_override") or "", tuple(ent["color"]), ent.get("rendermode", 0))


def _styled(ent):
    return bool(ent.get("material_override")) or tuple(ent["color"][:3]) != (255, 255, 255)


def _priority(ent):
    eid = ent["id"]
    return (_styled(ent), eid if isinstance(eid, (int, float)) else -1)


def _fights(a, b, overlap) -> bool:
    """True if a and b have coincident faces that also overlap in area within that face's plane
    (props merely touching edge to edge share a plane but can't fight)."""
    for na, da in _faces(a):
        for nb, db in _faces(b):
            # same plane and facing the same way; back-to-back faces sit inside a joint and never show
            if sum(na[i] * nb[i] for i in range(3)) > 1 - 1e-3 and abs(da - db) < PLANE_EPS:
                normal_axis = max(range(3), key=lambda i: abs(na[i]))
                if all(overlap[i] > MIN_OVERLAP for i in range(3) if i != normal_axis):
                    return True
    return False


def resolve(scene: dict) -> int:
    """Adjust entity matrices in place; returns how many entities were grown."""
    if any("zbias" in e for e in scene["entities"]):
        return 0  # already resolved (e.g. a Scene JSON written by `ad2render scene`)
    ents = [e for e in scene["entities"]
            if e.get("bounds") and not e.get("placeholder") and not e.get("posed")]
    if len(ents) < 2:
        return 0
    boxes = [_world_box(e) for e in ents]
    order = sorted(range(len(ents)), key=lambda i: boxes[i][0][0])
    beats: dict[int, set[int]] = {}
    for oi, a in enumerate(order):        # sweep along x so large dupes stay fast
        alo, ahi = boxes[a]
        for b in order[oi + 1:]:
            blo, bhi = boxes[b]
            if blo[0] > ahi[0] + PLANE_EPS:
                break
            if any(ahi[i] < blo[i] - PLANE_EPS or bhi[i] < alo[i] - PLANE_EPS for i in (1, 2)):
                continue
            overlap = [min(ahi[i], bhi[i]) - max(alo[i], blo[i]) for i in range(3)]
            if _look(ents[a]) == _look(ents[b]) or not _fights(ents[a], ents[b], overlap):
                continue
            win, lose = (a, b) if _priority(ents[a]) > _priority(ents[b]) else (b, a)
            beats.setdefault(win, set()).add(lose)

    # level = 1 + the highest level among the props it covers (stacked panels). Priority is a strict
    # total order, so "beats" has no cycles; resolve lowest-priority winners first.
    level: dict[int, int] = {}
    for i in sorted(beats, key=lambda i: _priority(ents[i])):
        level[i] = min(MAX_LEVEL, 1 + max((level.get(j, 0) for j in beats[i]), default=0))
        _grow(ents[i], level[i] * Z_BIAS)
        ents[i]["zbias"] = level[i]
    return len(level)


def _grow(ent, margin: float):
    """Scale about the bounds centre so every face moves outward by `margin` inches (world units)."""
    (lo, hi), m = ent["bounds"], ent["parts"][0]["matrix"]
    c = [(lo[k] + hi[k]) / 2 for k in range(3)]
    s = []
    for k in range(3):
        col = sum(m[i][k] ** 2 for i in range(3)) ** 0.5 or 1.0   # existing model scale on this axis
        ext = max(hi[k] - lo[k], 1e-3) * col
        s.append(1 + 2 * margin / ext)
    # M' = M * T(c) * S * T(-c)
    new = [[m[i][k] * s[k] for k in range(3)] + [m[i][3] + sum(m[i][k] * c[k] * (1 - s[k]) for k in range(3))]
           for i in range(3)]
    ent["parts"][0]["matrix"] = new + [[0.0, 0.0, 0.0, 1.0]]
