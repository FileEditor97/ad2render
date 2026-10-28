"""Synthetic AdvDupe2 files for the tests: an encoder (mirror of sh_codec.lua) and a small sample build.

The sample only names models and materials that ship with Garry's Mod; no game content is stored here.
"""
import lzma
import struct

from ad2render.codec import Angle, Vector


class Ref(int):
    """A table reference: the n-th table written so far (1-based)."""


def _encode_value(v, rev, out):
    if isinstance(v, Ref):
        out.append(247)
        out += struct.pack("<h", v)
    elif isinstance(v, bool):
        out.append(253 if v else 252)
    elif isinstance(v, (int, float)):
        out.append(251)
        out += struct.pack("<d", v)
    elif isinstance(v, Vector):
        out.append(250)
        out += struct.pack("<3d", v.x, v.y, v.z)
    elif isinstance(v, Angle):
        out.append(249)
        out += struct.pack("<3d", v.p, v.y, v.r)
    elif isinstance(v, str):
        b = v.encode()
        if rev >= 5 and len(b) == 0:
            out.append(0)
        elif len(b) < (246 if rev >= 5 else 247) and (rev >= 5 or len(b) > 0):
            out.append(len(b))
            out += b
        elif rev >= 5:
            out.append(248)
            out += struct.pack("<I", len(b)) + b
        else:
            out.append(248)
            out += b + b"\0"
    elif isinstance(v, dict):
        out.append(255)
        for k, x in v.items():
            _encode_value(k, rev, out)
            _encode_value(x, rev, out)
        out.append(246 if rev >= 5 else 0)
    else:
        raise TypeError(v)


def encode(tree, rev=5, info=None):
    body = bytearray()
    _encode_value(tree, rev, body)
    filt = [{"id": lzma.FILTER_LZMA1, "lc": 3, "lp": 0, "pb": 2, "dict_size": 1 << 16}]
    comp = lzma.compress(bytes(body), format=lzma.FORMAT_RAW, filters=filt)
    payload = bytes([93]) + struct.pack("<I", 1 << 16) + struct.pack("<Q", len(body)) + comp
    hdr = b""
    for k, v in (info or {"name": "test"}).items():
        hdr += k.encode() + b"\1" + v.encode() + b"\1"
    hdr += b"check\1\r\n\t\n\1\2"
    return b"AD2F" + bytes([rev]) + b"\n" + hdr + b"\n" + payload


# The sample build ------------------------------------------------------------------------------------------------

PLATE = "models/hunter/plates/plate1x1.mdl"
DOOR = "models/props_c17/door01_left.mdl"
KEYPAD = "models/props_lab/keypad.mdl"
BUTTON = "models/maxofs2d/button_05.mdl"
OVERRIDE = "phoenix_storms/metalset_1-2"
PASSWORD = 2517              # stored in the keypad's modifier; must never reach a viewer
INFO = {"name": "Sample Author", "date": "01 January 2026", "time": "12:00:00 PM", "timezone": "+0000"}
HEAD_Z = 48.0


def _prop(model, pos, cls="prop_physics", ang=(0, 0, 0), color=(255, 255, 255, 255), rendermode=0, **extra):
    mods = {"colour": {"Color": dict(zip("rgba", color)), "RenderMode": rendermode, "RenderFX": 0}}
    mods.update(extra.pop("mods", {}))
    return {"Class": cls, "Model": model, "EntityMods": mods,
            "PhysicsObjects": {0: {"Pos": Vector(*pos), "Angle": Angle(*ang), "Frozen": True}}, **extra}


def _fading(pos, key, reversed_=False):
    return _prop(PLATE, pos, mods={"Fading Door": {"key": key, "toggle": False, "reversed": reversed_}})


def sample_tree():
    """A small build with one of everything the scene cares about:
    1  head plate, black, RenderMode 1
    2  door with skin 1 and bodygroup 1=1
    3, 4  fading doors on KEY_PAD_MINUS (49), opened by keypad 5
    5  keypad: access granted presses 49
    6  gmod_button on KEY_PAD_ENTER (61), opening fading door 7
    8  fading door on G (17) with nothing in the dupe pressing it
    9  see-through prop (RenderMode 1, alpha 120)
    10 prop with a material override
    One weld joins 1 and 2."""
    ents = {
        1: _prop(PLATE, (0, 0, 0), color=(0, 0, 0, 255), rendermode=1),
        2: _prop(DOOR, (0, 60, 50), Skin=1, BodyG={1: 1}),
        3: _fading((60, 0, 0), 49, reversed_=True),
        4: _fading((60, 60, 0), 49, reversed_=True),
        5: _prop(KEYPAD, (0, -40, 40), cls="keypad",
                 mods={"keypad_password_passthrough": {"Password": PASSWORD, "KeyGranted": 49, "KeyDenied": 0}}),
        6: _prop(BUTTON, (40, -40, 40), cls="gmod_button", key=61, DT={"IsToggle": False, "Label": ""}),
        7: _fading((-60, 0, 0), 61),
        8: _fading((-60, 60, 0), 17),
        9: _prop(PLATE, (0, 0, 60), color=(255, 255, 255, 120), rendermode=1),
        10: _prop(PLATE, (0, 0, 120), ang=(0, 45, 0), mods={"material": {"MaterialOverride": OVERRIDE}}),
    }
    return {
        "Entities": ents,
        "Constraints": {1: {"Type": "Weld", "Entity": {1: {"Index": 1, "LPos": Vector(0, 0, 0)},
                                                       2: {"Index": 2, "LPos": Vector(0, 0, 0)}}}},
        "HeadEnt": {"Index": 1, "Pos": Vector(100, 200, 50), "Z": HEAD_Z},
    }


def sample_dupe(rev=5) -> bytes:
    return encode(sample_tree(), rev, INFO)
