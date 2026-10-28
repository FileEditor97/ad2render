import json
import re
import math
import random

import pytest

from ad2render import codec
from ad2render.codec import Angle, DecodeError, Vector, decode
from ad2render.scene import angle_matrix, normalize

from dupes import HEAD_Z, INFO, PASSWORD, Ref, encode, sample_dupe


def sample():
    return decode(sample_dupe())


# The sample build ------------------------------------------------------------------------------------------------

def test_sample_decodes():
    d = sample()
    assert d.revision == 5
    assert d.info["name"] == INFO["name"] and d.info["date"] == INFO["date"] and d.info["timezone"] == "+0000"
    assert len(d.data["Entities"]) == 10
    head = d.data["HeadEnt"]
    assert head["Index"] in d.data["Entities"] and head["Z"] == pytest.approx(HEAD_Z)
    assert head["Pos"] == Vector(100, 200, 50)
    e = d.data["Entities"][1]
    assert e["Model"] == "models/hunter/plates/plate1x1.mdl"
    assert e["PhysicsObjects"][0]["Pos"] == Vector(0.0, 0.0, 0.0)
    assert e["EntityMods"]["colour"]["Color"] == {"r": 0, "g": 0, "b": 0, "a": 255}
    assert e["EntityMods"]["colour"]["RenderMode"] == 1


def test_skin_and_bodygroups():
    ents = sample().data["Entities"]
    doors = [e for e in ents.values() if "Skin" in e]
    assert len(doors) == 1
    assert all(e["Skin"] == 1 and e["BodyG"] == {1: 1} for e in doors)


def _tree():
    return {
        "Entities": {
            5: {"Class": "prop_physics", "Model": "models/a.mdl", "Skin": 2, "Empty": "",
                "Long": "x" * 300,
                "PhysicsObjects": {0: {"Pos": Vector(1, 2, 3), "Angle": Angle(10, 20, 30), "Frozen": True}}},
        },
        "Constraints": {},
        "HeadEnt": {"Index": 5, "Pos": Vector(0, 0, 0), "Z": 1.5},
    }


@pytest.mark.parametrize("rev", [4, 5])
def test_roundtrip(rev):
    d = decode(encode(_tree(), rev))
    assert d.revision == rev
    e = d.data["Entities"][5]
    assert e["PhysicsObjects"][0]["Angle"] == Angle(10, 20, 30)
    assert e["Long"] == "x" * 300
    assert e["Skin"] == 2
    if rev >= 5:
        assert e["Empty"] == ""


def test_text_mangled_check():
    raw = encode(_tree()).replace(b"\r\n\t\n", b"\n\t\n")
    with pytest.raises(DecodeError, match="newlines homogenized"):
        decode(raw)
    raw = encode(_tree()).replace(b"\r\n\t\n", b"\r\r\n\t\r\n")
    with pytest.raises(DecodeError, match="text-mangled"):
        decode(raw)


@pytest.mark.parametrize("raw, msg", [
    (b"", "too short"),
    (b"hello world", "Unknown duplication format"),
    (b"AD2F\x09\n", "Newer codec"),
    (b"[Info]\n", "Advanced Duplicator 1"),
])
def test_bad_headers(raw, msg):
    with pytest.raises(DecodeError, match=msg):
        decode(raw)


def test_truncated():
    raw = sample_dupe()
    with pytest.raises(DecodeError):
        decode(raw[: len(raw) // 2])


def test_fuzz_never_crashes():
    raw = sample_dupe()
    _rev, _info, payload = codec.parse_header(raw)
    buf = codec.lzma_decompress(payload)
    rnd = random.Random(1234)
    for _ in range(1500):
        b = bytearray(buf)
        for _ in range(rnd.randint(1, 4)):
            b[rnd.randrange(len(b))] = rnd.randrange(256)
        if rnd.random() < 0.3:
            b = b[: rnd.randrange(len(b))]
        try:
            data = codec._Reader(bytes(b), 5).read()
            if isinstance(data, dict):
                codec._validate(data)
                normalize(codec.Dupe(5, {}, data))
        except DecodeError:
            pass


# Coordinate conventions -----------------------------------------------------------

def _apply(m, v):
    return [sum(m[i][k] * v[k] for k in range(3)) for i in range(3)]


def test_angle_conventions():
    assert _apply(angle_matrix(0, 90, 0), [1, 0, 0]) == pytest.approx([0, 1, 0], abs=1e-9)
    # positive pitch points the nose down
    assert _apply(angle_matrix(90, 0, 0), [1, 0, 0]) == pytest.approx([0, 0, -1], abs=1e-9)
    # positive roll tips the left side (+Y) up
    assert _apply(angle_matrix(0, 0, 90), [0, 1, 0]) == pytest.approx([0, 0, 1], abs=1e-9)
    # yaw is applied last: forward of (p=45, y=90) is (0, cos45, -sin45)
    s = math.sqrt(0.5)
    assert _apply(angle_matrix(45, 90, 0), [1, 0, 0]) == pytest.approx([0, s, -s], abs=1e-9)


def test_normalize_scene():
    scene = normalize(sample(), "sample")
    assert len(scene["entities"]) == 10
    doors = [e for e in scene["entities"] if e["skin"] == 1]
    assert doors and doors[0]["bodygroups"] == {"1": 1}
    for e in scene["entities"]:
        m = e["parts"][0]["matrix"]
        assert m[3] == [0.0, 0.0, 0.0, 1.0]
        assert len(e["color"]) == 4


def test_fading_door_modes():
    from ad2render.scene import with_fading_doors
    scene = normalize(sample(), "sample")
    doors = [e for e in scene["entities"] if e["fading_door"]]
    assert len(doors) == 4
    assert with_fading_doors(scene, "solid") is scene
    ghost = with_fading_doors(scene, "ghost")
    assert all(e["color"][3] <= 90 and e["rendermode"] == 1 for e in ghost["entities"] if e["fading_door"])
    assert all(e["color"][3] == 255 for e in scene["entities"] if e["fading_door"])  # original untouched
    assert len(with_fading_doors(scene, "hidden")["entities"]) == len(scene["entities"]) - 5   # 4 doors + 1 see-through
    with pytest.raises(ValueError):
        with_fading_doors(scene, "open")


def _with_bounds(scene, lo=(-10.0, -10.0, -10.0), hi=(10.0, 10.0, 10.0)):
    for e in scene["entities"]:
        e["bounds"] = [list(lo), list(hi)]
    return scene


def test_ground_level_from_head_z():
    from ad2render.ground import footprint, ground_level
    scene = _with_bounds(normalize(sample(), "sample"))
    # HeadEnt.Z is the head prop's height above the ground: props sticking below must not lift the ground
    assert ground_level(scene) == pytest.approx(-scene["head"]["z"])
    assert scene["head"]["z"] == pytest.approx(HEAD_Z)
    cx, cy, side = footprint(scene)
    assert side >= 256


def test_ground_level_falls_back_to_lowest_point():
    from ad2render.ground import ground_level

    def ent(z):
        return {"bounds": [[-5.0, -5.0, -2.0], [5.0, 5.0, 2.0]],
                "parts": [{"matrix": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, z], [0, 0, 0, 1]]}]}
    scene = {"head": {"z": 0}, "entities": [ent(0.0), ent(-30.0), ent(12.0)]}  # the copy trace hit nothing
    assert ground_level(scene) == pytest.approx(-32.0)


def test_transparent_props_follow_the_fading_door_setting():
    from ad2render.scene import with_fading_doors
    ent = lambda i, a, rm=1, door=False: {"id": i, "color": [255, 255, 255, a], "rendermode": rm, "fading_door": door,
                                          "transparent": rm != 0 and a < 255}
    scene = {"entities": [ent(1, 255, 0), ent(2, 40), ent(3, 200), ent(4, 255, 0, door=True), ent(5, 255, 1)]}
    assert [e["transparent"] for e in scene["entities"]] == [False, True, True, False, False]   # alpha 255 / RenderMode 0: opaque
    assert with_fading_doors(scene, "solid") is scene                                            # solid keeps the saved alpha
    ghost = {e["id"]: e for e in with_fading_doors(scene, "ghost")["entities"]}
    assert ghost[2]["color"][3] == 40 and ghost[3]["color"][3] == 90 and ghost[4]["color"][3] == 90   # never more visible than saved
    assert ghost[1]["color"][3] == 255 and ghost[5]["color"][3] == 255
    assert [e["id"] for e in with_fading_doors(scene, "hidden")["entities"]] == [1, 5]


def test_normalize_flags_transparent_props():
    ents = normalize(sample())["entities"]
    assert [e for e in ents if e["transparent"]] and all(e["rendermode"] != 0 and e["color"][3] < 255 for e in ents if e["transparent"])


# Uploads: tables repeated through references -----------------------------------------------------------------
def _repeated(n_props=1, n_physobjs=1, n_constraints=0, n_ends=2):
    """A tiny file that repeats one prop / physics object / constraint end by reference.
    Tables are numbered as written: top 1, HeadEnt 2, Entities 3, the prop 4, its PhysicsObjects 5, object 0 6."""
    prop = {"Model": "models/props_c17/oildrum001.mdl",
            "PhysicsObjects": {0: {"Pos": Vector(0, 0, 0), "Angle": Angle(0, 0, 0)}, **{i: Ref(6) for i in range(1, n_physobjs)}}}
    tree = {"HeadEnt": {"Index": 1, "Pos": Vector(0, 0, 0), "Z": 0},
            "Entities": {1: prop, **{i: Ref(4) for i in range(2, n_props + 1)}}}
    if n_constraints:   # Constraints is table 7, constraint 1 is table 8
        tree["Constraints"] = {1: {"Type": "Weld", "Entity": {i: {"Index": 1} for i in range(1, n_ends + 1)}},
                               **{i: Ref(8) for i in range(2, n_constraints + 1)}}
    return encode(tree)


def test_a_value_cap_stops_decoding():
    raw = _repeated(n_props=50)
    assert len(decode(raw).data["Entities"]) == 50
    with pytest.raises(DecodeError, match="too many values"):
        decode(raw, max_values=20)


def test_tables_in_copied_fields_are_dropped_not_repeated():
    """Class, constraint types and indexes are copied into the scene (and its JSON): one big table referenced from
    each of 400 props used to be written out 400 times."""
    big = {i: "x" for i in range(1, 300_001)}                    # table 2 (top 1); HeadEnt 3, Entities 4, the prop 5
    tree = {"Big": big, "HeadEnt": {"Index": Ref(2), "Pos": Vector(0, 0, 0), "Z": Ref(2)},
            "Entities": {1: {"Class": Ref(2), "PhysicsObjects": {0: {"Pos": Vector(0, 0, 0), "Angle": Angle(0, 0, 0)}}},
                         **{i: Ref(5) for i in range(2, 401)}},
            "Constraints": {1: {"Type": Ref(2), "Entity": {1: {"Index": Ref(2)}}}}}
    tree["Entities"][1]["Model"] = "models/a.mdl"
    sc = normalize(decode(encode(tree)))
    assert len(sc["entities"]) == 400 and {e["class"] for e in sc["entities"]} == {""}
    assert sc["constraints"] == [{"type": "", "ends": [{"entity": None, "lpos": None}]}]
    assert sc["head"]["index"] is None and sc["head"]["z"] == 0.0
    assert len(json.dumps(sc, default=str)) < 2 * 2**20


def test_keypads_link_to_the_fading_doors_on_their_key():
    scene = normalize(sample(), "sample")
    doors = [e for e in scene["entities"] if (e["numpad"] or {}).get("door", {}).get("key") == 49]
    pads = [e for e in scene["entities"] if (e["numpad"] or {}).get("trigger")]
    assert len(doors) == 2                                                                  # KEY_PAD_MINUS
    assert all(e["numpad"]["door"] == {"key": 49, "toggle": False, "reversed": True} for e in doors)
    assert len(pads) == 1 and pads[0]["numpad"]["trigger"] == {"granted": 49, "denied": None}
    pad_links = [l for l in scene["links"] if l["on"] == "granted"]
    assert {(l["from"], l["to"]) for l in pad_links} == {(pads[0]["id"], d["id"]) for d in doors}
    assert all(l["key"] == 49 for l in pad_links)
    assert not re.search(rf"(?<![\d.]){PASSWORD}(?![\d.])", json.dumps(scene, default=str))   # the password stays out


def test_fading_doors_without_a_keypad_have_no_links():
    scene = normalize(sample(), "sample")
    keys = sorted(e["numpad"]["door"]["key"] for e in scene["entities"] if (e["numpad"] or {}).get("door"))
    assert keys == [17, 49, 49, 61]
    assert not [l for l in scene["links"] if l["key"] == 17]                               # G, pressed by the owner


def test_buttons_link_to_the_fading_doors_on_their_key():
    scene = normalize(sample(), "sample")
    buttons = {e["id"]: e["numpad"]["button"] for e in scene["entities"] if (e["numpad"] or {}).get("button")}
    assert buttons == {6: {"key": 61, "toggle": False, "label": ""}}
    assert [(l["from"], l["to"], l["on"]) for l in scene["links"] if l["on"] == "press"] == [(6, 7, "press")]
