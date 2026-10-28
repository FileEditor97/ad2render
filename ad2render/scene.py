"""Normalize a decoded dupe tree into the Scene JSON pivot format.

Coordinates stay in Source space: right-handed, Z-up, +X forward, units in inches.
Each part carries a row-major 4x4 matrix built from Source angles as
R = Rz(yaw) * Ry(pitch) * Rx(roll), so that pitch +90 points local +X at world -Z.
"""
from __future__ import annotations

import math

from .codec import Angle, Dupe, Vector

SCENE_VERSION = 1


def angle_matrix(p: float, y: float, r: float) -> list[list[float]]:
    sp, cp = math.sin(math.radians(p)), math.cos(math.radians(p))
    sy, cy = math.sin(math.radians(y)), math.cos(math.radians(y))
    sr, cr = math.sin(math.radians(r)), math.cos(math.radians(r))
    # Columns are the local forward (X), left (Y) and up (Z) axes in world space.
    return [
        [cp * cy, sr * sp * cy - cr * sy, cr * sp * cy + sr * sy],
        [cp * sy, sr * sp * sy + cr * cy, cr * sp * sy - sr * cy],
        [-sp, sr * cp, cr * cp],
    ]


def transform(pos: Vector, ang: Angle, scale: float = 1.0) -> list[list[float]]:
    r = angle_matrix(ang.p, ang.y, ang.r)
    return [
        [r[0][0] * scale, r[0][1] * scale, r[0][2] * scale, pos.x],
        [r[1][0] * scale, r[1][1] * scale, r[1][2] * scale, pos.y],
        [r[2][0] * scale, r[2][1] * scale, r[2][2] * scale, pos.z],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _num(v, default=0.0):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _text(v, limit: int = 120) -> str:
    """A short string, or "". Values copied into the scene must not be tables: a table reference costs 3 bytes in the
    file, so formatting or serialising one table many times turns a small upload into gigabytes."""
    return v[:limit] if isinstance(v, str) else ""


def _index(v):
    """An entity index (an integral number), or None."""
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v == int(v) else None


def _entity(key, e: dict, warnings: list[str]) -> dict | None:
    model = e.get("Model")
    if not isinstance(model, str) or not model:
        warnings.append(f"entity {key} ({_text(e.get('Class'))}) has no model; skipped")
        return None
    mods = e.get("EntityMods") if isinstance(e.get("EntityMods"), dict) else {}
    colour = mods.get("colour") if isinstance(mods.get("colour"), dict) else {}
    col = colour.get("Color") if isinstance(colour.get("Color"), dict) else {}
    color = [int(_num(col.get(c), 255)) for c in ("r", "g", "b", "a")]
    material = mods.get("material") if isinstance(mods.get("material"), dict) else {}
    override = material.get("MaterialOverride") or None
    bodyg = e.get("BodyG") if isinstance(e.get("BodyG"), dict) else {}
    scale = _num(e.get("ModelScale"), 1.0) or 1.0

    parts = []
    phys = e["PhysicsObjects"]
    for idx in sorted(k for k in phys if isinstance(k, int)):
        po = phys[idx]
        if not isinstance(po, dict):
            continue
        pos, ang = po.get("Pos"), po.get("Angle")
        if not isinstance(pos, Vector) or not isinstance(ang, Angle):
            warnings.append(f"entity {key} physobj {idx} has no Pos/Angle; skipped")
            continue
        parts.append({
            "index": idx,
            "pos": [pos.x, pos.y, pos.z],
            "ang": [ang.p, ang.y, ang.r],
            "matrix": transform(pos, ang, scale),
            "frozen": bool(po.get("Frozen", False)),
        })
    if not parts:
        return None
    return {
        "id": key,
        "class": _text(e.get("Class")),
        "model": model.replace("\\", "/").lower(),
        "skin": int(_num(e.get("Skin"), 0)),
        "bodygroups": {str(int(k)): int(_num(v)) for k, v in bodyg.items() if isinstance(k, int)},
        "scale": scale,
        "color": color,
        "rendermode": int(_num(colour.get("RenderMode"), 0)),
        "renderfx": int(_num(colour.get("RenderFX"), 0)),
        "material_override": override.replace("\\", "/") if isinstance(override, str) else None,
        # the fading door tool stores an entity modifier; newer versions also flag the entity itself
        "fading_door": bool(mods.get("Fading Door")) or e.get("isFadingDoor") is True,
        # see-through as saved (the game ignores alpha in RenderMode 0); handled like fading doors, see-through mode
        "transparent": int(_num(colour.get("RenderMode"), 0)) != 0 and color[3] < 255,
        "numpad": _numpad(e, mods),
        "parts": parts,
    }


def _key(v):
    """A numpad key (GMod KEY_ enum), or None for "no key"."""
    k = _index(v)
    return k if k and 0 < k < 256 else None


def _numpad(e: dict, mods: dict) -> dict | None:
    """What the entity does with numpad keys: a fading door opens on a key, a keypad presses one. Only the key numbers
    are kept: the keypad's modifier also holds its password, which must never reach a viewer."""
    out = {}
    door = mods.get("Fading Door")
    if isinstance(door, dict) or e.get("isFadingDoor") is True:
        door = door if isinstance(door, dict) else {}
        # the modifier is what the game applies on paste; the entity fields are a newer copy
        key = _key(door.get("key")) or _key(e.get("fadeButton"))
        if key:
            out["door"] = {"key": key, "toggle": door.get("toggle", e.get("fadeToggle")) is True,
                           "reversed": door.get("reversed") is True}
    pad = mods.get("keypad_password_passthrough")
    if isinstance(pad, dict):
        granted, denied = _key(pad.get("KeyGranted")), _key(pad.get("KeyDenied"))
        if granted or denied:
            out["trigger"] = {"granted": granted, "denied": denied}
    if e.get("Class") == "gmod_button":
        dt = e.get("DT") if isinstance(e.get("DT"), dict) else {}
        key = _key(e.get("key")) or _key(dt.get("Key"))  # the spawn argument, else the networked copy
        if key:
            out["button"] = {"key": key, "toggle": dt.get("IsToggle") is True, "label": _text(dt.get("Label"), 60)}
    return out or None


def _links(ents: list[dict]) -> list[dict]:
    """Keypad / button -> fading door pairs that share a numpad key. Keys belong to the player, not to an entity, so this
    is what the dupe does once pasted by one player (who has no other doors, keypads or buttons on the same key)."""
    doors: dict[int, list] = {}
    for e in ents:
        if e["numpad"] and "door" in e["numpad"]:
            doors.setdefault(e["numpad"]["door"]["key"], []).append(e["id"])
    out = []
    for e in ents:
        n = e["numpad"] or {}
        keys = [(n["trigger"][on], on) for on in ("granted", "denied") if "trigger" in n and n["trigger"][on]]
        keys += [(n["button"]["key"], "press")] if "button" in n else []
        for key, on in keys:
            out += [{"from": e["id"], "to": d, "key": key, "on": on} for d in doors.get(key, ()) if d != e["id"]]
    return out


def _constraints(dupe: dict) -> list[dict]:
    out = []
    cons = dupe.get("Constraints")
    if not isinstance(cons, dict):
        return out
    for c in cons.values():
        if not isinstance(c, dict):
            continue
        ents = c.get("Entity") if isinstance(c.get("Entity"), dict) else {}
        ends = []
        for slot in sorted(k for k in ents if isinstance(k, int)):
            en = ents[slot]
            if not isinstance(en, dict):
                continue
            lp = en.get("LPos")
            ends.append({
                "entity": _index(en.get("Index")),
                "lpos": [lp.x, lp.y, lp.z] if isinstance(lp, Vector) else None,
            })
        out.append({"type": _text(c.get("Type")), "ends": ends})
    return out


def normalize(dupe: Dupe, name: str = "") -> dict:
    d = dupe.data
    warnings: list[str] = []
    ents = []
    for key in sorted(d["Entities"], key=lambda k: (not isinstance(k, (int, float)), str(k))):
        e = d["Entities"][key]
        if isinstance(e, dict):
            ent = _entity(key, e, warnings)
            if ent:
                ents.append(ent)
    head = d["HeadEnt"]
    hp = head["Pos"]
    return {
        "version": SCENE_VERSION,
        "name": name,
        "info": dict(dupe.info, revision=dupe.revision),
        "units": "source_inches",
        "up": "z",
        "head": {"index": _index(head.get("Index")), "pos": [hp.x, hp.y, hp.z], "z": _num(head.get("Z"))},
        "entities": ents,
        "constraints": _constraints(d),
        "links": _links(ents),
        "warnings": warnings,
    }


FADING_DOOR_MODES = ("solid", "ghost", "hidden")
GHOST_ALPHA = 90  # out of 255


def affected(e: dict) -> bool:
    """Props the Fading doors setting applies to: fading doors, and props with a transparent colour."""
    return bool(e.get("fading_door") or e.get("transparent"))


def with_fading_doors(scene: dict, mode: str) -> dict:
    """Scene for rendering with fading doors (and props saved with transparency) drawn solid (fading doors closed, others
    with their saved alpha), as see-through ghosts, or not at all."""
    if mode not in FADING_DOOR_MODES:
        raise ValueError(f"fading door mode must be one of {', '.join(FADING_DOOR_MODES)}")
    if mode == "solid" or not any(affected(e) for e in scene["entities"]):
        return scene
    ents = []
    for e in scene["entities"]:
        if not affected(e):
            ents.append(e)
        elif mode == "ghost":
            ents.append(dict(e, color=list(e["color"][:3]) + [min(e["color"][3], GHOST_ALPHA)], rendermode=1))
    return dict(scene, entities=ents)
