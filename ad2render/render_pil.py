"""Dependency-light preview renderer: orthographic, flat shaded, painter's algorithm (Pillow only)."""
from __future__ import annotations

import math

from PIL import Image, ImageDraw

from . import gltf

VIEWS = {  # (yaw, pitch) of the camera position around the build, degrees
    "iso": (45.0, 30.0),
    "iso2": (225.0, 30.0),
    "front": (0.0, 0.0),
    "back": (180.0, 0.0),
    "side": (90.0, 0.0),
    "top": (0.0, 89.9),
}


def camera_basis(yaw: float, pitch: float):
    a, e = math.radians(yaw), math.radians(pitch)
    d = (math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e))  # towards camera
    r = (-math.sin(a), math.cos(a), 0.0)  # screen right = forward x up
    u = (d[1] * r[2] - d[2] * r[1], d[2] * r[0] - d[0] * r[2], d[0] * r[1] - d[1] * r[0])
    return d, r, u


def _xf(m, v):
    return (m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2] + m[0][3],
            m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2] + m[1][3],
            m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2] + m[2][3])


IDENTITY = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def _tri_list(scene, glb_cache):
    tris = []  # (p0, p1, p2, rgba)
    for ent in scene["entities"]:
        path = ent["glb"]
        if path not in glb_cache:
            glb_cache[path] = gltf.read_glb(path)[1]
        prims = glb_cache[path]
        m = IDENTITY if ent.get("posed") else ent["parts"][0]["matrix"]
        tint = ent["color"]
        ov = ent.get("override")
        for pos, _nrm, idx, mat in prims:
            avg = (mat.get("extras") or {}).get("avg") or [200, 200, 200, 255]
            if ov and not ent.get("placeholder"):
                avg = ov["avg"]
            if ent.get("placeholder"):
                col, alpha = [255, 0, 255], 140
            else:
                col = [avg[i] * tint[i] / 255 for i in range(3)]
                alpha = tint[3] if ent["rendermode"] != 0 else 255  # GMod ignores alpha in RenderMode 0
                alpha = alpha * avg[3] / 255 if mat.get("alphaMode") == "BLEND" else alpha
            wp = [_xf(m, v) for v in pos]
            for t in range(0, len(idx), 3):
                tris.append((wp[idx[t]], wp[idx[t + 1]], wp[idx[t + 2]], col, alpha,
                             mat.get("doubleSided", False)))
    return tris


CONSTRAINT_COLORS = {"Weld": (255, 196, 0), "Rope": (190, 120, 60), "Elastic": (90, 200, 255),
                     "Axis": (255, 80, 80), "Ballsocket": (120, 255, 120), "NoCollide": (180, 180, 180)}


def draw_constraints(img, segs, width=None):
    """segs: [(type, (x, y), (x, y))] in pixels."""
    if not segs:
        return
    draw = ImageDraw.Draw(img, "RGBA")
    w = width or max(1, round(min(img.size) / 400))
    for typ, a, b in segs:
        col = CONSTRAINT_COLORS.get(typ, (255, 0, 255))
        draw.line([a, b], fill=col + (220,), width=w)
        for p in (a, b):
            draw.ellipse([p[0] - w * 1.5, p[1] - w * 1.5, p[0] + w * 1.5, p[1] + w * 1.5], fill=col + (255,))


def anchor(ent, lpos=None):
    """World point of a constraint end: its saved local position, else the middle of the prop (the origin of many
    models sits at a corner or the bottom). A ragdoll's mesh and bounds are already in dupe space."""
    if lpos:
        return _xf(ent["parts"][0]["matrix"], lpos)
    if ent.get("bounds"):
        (lo, hi), m = ent["bounds"], IDENTITY if ent.get("posed") else ent["parts"][0]["matrix"]
        return _xf(m, [(a + b) / 2 for a, b in zip(lo, hi)])
    return _xf(ent["parts"][0]["matrix"], (0, 0, 0))


def constraint_segments(scene):
    """World-space (type, a, b) for constraints between rendered entities."""
    ents = {str(e["id"]): e for e in scene["entities"]}
    segs = []
    for c in scene.get("constraints", []):
        pts = [anchor(ents[str(end["entity"])], end["lpos"]) for end in c["ends"] if str(end["entity"]) in ents]
        segs += [(c["type"], pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    return segs


def render(scene, out_path, width=800, height=600, view="iso", yaw=None, pitch=None,
           background=(0, 0, 0, 0), ssaa=2, outline=True, glb_cache=None, constraints=False):
    y0, p0 = VIEWS.get(view, VIEWS["iso"])
    yaw = y0 if yaw is None else yaw
    pitch = p0 if pitch is None else pitch
    d, r, u = camera_basis(yaw, pitch)
    tris = _tri_list(scene, glb_cache if glb_cache is not None else {})
    W, H = width * ssaa, height * ssaa
    img = Image.new("RGBA", (W, H), tuple(background))
    if not tris:
        img.resize((width, height), Image.LANCZOS).save(out_path)
        return

    def proj(p):
        return (p[0] * r[0] + p[1] * r[1] + p[2] * r[2],
                p[0] * u[0] + p[1] * u[1] + p[2] * u[2],
                p[0] * d[0] + p[1] * d[1] + p[2] * d[2])

    light = _norm((d[0] + r[0] * -0.5 + u[0] * 0.8, d[1] + r[1] * -0.5 + u[1] * 0.8,
                   d[2] + r[2] * -0.5 + u[2] * 0.8))
    items = []
    xs, ys = [], []
    for a, b, c, col, alpha, two_sided in tris:
        pa, pb, pc = proj(a), proj(b), proj(c)
        # world-space face normal; the glb cache stores counter-clockwise front faces
        e1 = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        e2 = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
        n = _norm((e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]))
        facing = n[0] * d[0] + n[1] * d[1] + n[2] * d[2]
        if facing < 0:
            if not two_sided:  # also for translucent props: stacked back faces would add up to opaque
                continue
            n = (-n[0], -n[1], -n[2])
        xs += (pa[0], pb[0], pc[0])
        ys += (pa[1], pb[1], pc[1])
        lam = max(0.0, n[0] * light[0] + n[1] * light[1] + n[2] * light[2])
        sky = 0.5 + 0.5 * n[2]
        shade = 0.28 + 0.62 * lam + 0.18 * sky
        rgba = tuple(min(255, int(ch * shade)) for ch in col) + (int(alpha),)
        items.append(((pa[2] + pb[2] + pc[2]) / 3, (pa, pb, pc), rgba))
    if not items:
        img.resize((width, height), Image.LANCZOS).save(out_path)
        return
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    margin = 0.06
    span = max((maxx - minx) / (W * (1 - 2 * margin)), (maxy - miny) / (H * (1 - 2 * margin)), 1e-6)
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2

    def scr(p):
        return ((p[0] - cx) / span + W / 2, H / 2 - (p[1] - cy) / span)

    items.sort(key=lambda t: t[0])
    draw = ImageDraw.Draw(img, "RGBA")
    lw = max(1, ssaa // 2)
    g = scene.get("ground")
    if g and pitch > 0:  # flat-coloured ground, drawn first: the build sits on top of it
        (gx, gy), h = g["center"], g["size"] / 2
        quad = [proj((gx + sx * h, gy + sy * h, g["z"])) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        shade = 0.28 + 0.62 * max(0.0, light[2]) + 0.18
        draw.polygon([scr(p) for p in quad], fill=tuple(min(255, int(c * shade)) for c in g["color"]) + (255,))
    for _z, pts, rgba in items:
        poly = [scr(p) for p in pts]
        draw.polygon(poly, fill=rgba)
        if outline and rgba[3] == 255:
            # hairline in the fill colour hides cracks between adjacent triangles
            draw.line(poly + [poly[0]], fill=rgba, width=lw)
    if constraints:
        draw_constraints(img, [(t, scr(proj(a)), scr(proj(b))) for t, a, b in constraint_segments(scene)],
                         width=max(1, ssaa * round(min(width, height) / 400)))
    img = img.resize((width, height), Image.LANCZOS)
    img.save(out_path)


def _norm(v):
    length = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) or 1.0
    return (v[0] / length, v[1] / length, v[2] / length)
