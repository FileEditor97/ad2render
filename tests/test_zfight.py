import copy

from ad2render import zfight

BOUNDS = [[-24.0, -24.0, -6.0], [24.0, 24.0, 6.0]]  # a cube1x1x025-like block, inches


def ent(eid, x=0.0, z=0.0, color=(255, 255, 255, 255), override=None):
    return {"id": eid, "model": "models/hunter/blocks/cube1x1x025.mdl", "bounds": copy.deepcopy(BOUNDS),
            "color": list(color), "rendermode": 0, "material_override": override,
            "parts": [{"matrix": [[1.0, 0, 0, x], [0, 1.0, 0, 0], [0, 0, 1.0, z], [0, 0, 0, 1.0]]}]}


def scene(*ents):
    return {"entities": list(ents)}


def top(e):  # world z of the block's top face
    m = e["parts"][0]["matrix"]
    return m[2][2] * BOUNDS[1][2] + m[2][3]


def test_styled_prop_wins_coincident_face():
    plain, orange = ent(5), ent(3, color=(255, 93, 0, 255))   # plain was placed later but isn't styled
    s = scene(plain, orange)
    assert zfight.resolve(s) == 1
    assert "zbias" in orange and "zbias" not in plain
    assert top(orange) > top(plain)
    assert abs(top(orange) - top(plain) - zfight.Z_BIAS) < 1e-6  # every face moves out by the bias


def test_later_prop_wins_when_both_styled():
    a = ent(10, override="models/debug/debugwhite")
    b = ent(20, color=(0, 0, 255, 255))
    zfight.resolve(scene(a, b))
    assert "zbias" in b and "zbias" not in a


def test_same_look_and_side_by_side_are_left_alone():
    same = scene(ent(1), ent(2))                                  # identical look: no visible fight
    assert zfight.resolve(same) == 0
    side = scene(ent(1), ent(2, x=48.0, color=(255, 0, 0, 255)))  # touching edges, coplanar top faces
    assert zfight.resolve(side) == 0


def test_stacked_overlaps_get_increasing_levels_and_resolve_is_idempotent():
    base, mid, topmost = ent(1), ent(2, color=(255, 0, 0, 255)), ent(3, color=(0, 255, 0, 255))
    s = scene(base, mid, topmost)
    zfight.resolve(s)
    assert mid["zbias"] == 1 and topmost["zbias"] == 2
    before = copy.deepcopy(s)
    assert zfight.resolve(s) == 0 and s == before
