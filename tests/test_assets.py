"""Asset pipeline tests. They need a local Garry's Mod install and are skipped without one."""
from pathlib import Path

import pytest

from ad2render.gamefs import GameFS, find_gmod

from dupes import sample_dupe

gmod = find_gmod()
pytestmark = pytest.mark.skipif(gmod is None, reason="Garry's Mod install not found")


@pytest.fixture(scope="module")
def fs():
    return GameFS(gmod)


def test_vpk_and_mdl(fs):
    from ad2render.mdl import load_meshes, read_header
    base = "models/hunter/plates/plate1x1"
    mdl = fs.read(base + ".mdl")
    h = read_header(mdl)
    assert h.textures == ["myplastic"]
    assert h.hull[0][0] == pytest.approx(-24, abs=0.5) and h.hull[1][1] == pytest.approx(24, abs=0.5)
    meshes = load_meshes(h, mdl, fs.read(base + ".vvd"), fs.read(base + ".dx90.vtx"))
    assert sum(len(m.indices) for m in meshes) // 3 == 12


def test_source_winding_is_clockwise(fs):
    from ad2render.mdl import load_meshes, read_header
    base = "models/props_junk/trafficcone001a"
    mdl = fs.read(base + ".mdl")
    m = load_meshes(read_header(mdl), mdl, fs.read(base + ".vvd"), fs.read(base + ".dx90.vtx"))[0]
    agree = 0
    for t in range(0, len(m.indices), 3):
        a, b, c = (m.positions[i] for i in m.indices[t:t + 3])
        e1 = [b[k] - a[k] for k in range(3)]
        e2 = [c[k] - a[k] for k in range(3)]
        n = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])
        vn = [sum(m.normals[i][k] for i in m.indices[t:t + 3]) for k in range(3)]
        agree += sum(n[k] * vn[k] for k in range(3)) < 0
    assert agree == len(m.indices) // 3


def test_texture_decode(fs):
    from ad2render.material import find_material, load_texture
    mat = find_material(fs, "TrafficCone001a", ["models/props_junk/"])
    img = load_texture(fs, mat.basetexture)
    assert img.size == (128, 128)


@pytest.fixture
def sample_file(tmp_path):
    f = tmp_path / "sample.txt"
    f.write_bytes(sample_dupe())
    return f


def test_resolve_sample(fs, tmp_path, sample_file):
    from ad2render.cli import load_scene
    from ad2render.convert import Converter
    conv = Converter(fs, tmp_path / "cache")
    scene = load_scene(sample_file, fs, conv)
    assert all(Path(e["glb"]).exists() for e in scene["entities"])
    assert scene["report"]["missing_models"] == []


def test_placeholder_for_missing_model(fs, tmp_path):
    from ad2render.convert import Converter
    meta = Converter(fs, tmp_path).model("models/does/not/exist.mdl")
    assert meta["placeholder"] and Path(meta["glb"]).exists()


def test_pil_render(fs, tmp_path, sample_file):
    from PIL import Image
    from ad2render.cli import load_scene
    from ad2render.convert import Converter
    from ad2render.render_pil import render
    scene = load_scene(sample_file, fs, Converter(fs, tmp_path / "cache"))
    out = tmp_path / "s.png"
    render(scene, out, 200, 150)
    img = Image.open(out)
    assert img.size == (200, 150)
    assert img.getbbox() is not None  # something was drawn


def test_ragdoll_bind_pose_roundtrip(fs):
    """Posing every physics bone at its bind pose must reproduce the reference mesh."""
    from ad2render.convert import _inv_rigid, _phy_bones, _skin_meshes
    from ad2render.mdl import load_meshes, read_header
    base = "models/kleiner"
    mdl = fs.read(base + ".mdl")
    h = read_header(mdl)
    vvd, vtx = fs.read(base + ".vvd"), fs.read(base + ".dx90.vtx")
    ref = load_meshes(h, mdl, vvd, vtx)
    posed = load_meshes(h, mdl, vvd, vtx)
    phy = _phy_bones(fs.read(base + ".phy"))
    names = [n.lower() for n in h.bone_names]
    pose = [_inv_rigid(h.pose_to_bone[names.index(phy[i])] + [[0, 0, 0, 1]]) for i in sorted(phy)]
    _skin_meshes(h, posed, pose, phy)
    worst = max(abs(a - b) for m0, m1 in zip(ref, posed)
                for p0, p1 in zip(m0.positions, m1.positions) for a, b in zip(p0, p1))
    assert worst < 1e-3


class _StubFS:
    """Serves one model's files, optionally corrupted."""

    def __init__(self, real, base, mutate):
        self.files = {}
        for ext in (".mdl", ".vvd", ".dx90.vtx"):
            data = real.read(base + ext)
            self.files[base + ext] = mutate(ext, data)

    def read(self, path):
        return self.files.get(path.lower())

    def exists(self, path):
        return self.read(path) is not None


@pytest.mark.parametrize("mutate", [
    lambda ext, d: d[:300] if ext == ".mdl" else d,
    lambda ext, d: d[:64] if ext == ".vvd" else d,
    lambda ext, d: d[:40] if ext == ".dx90.vtx" else d,
    lambda ext, d: bytes(reversed(d)) if ext == ".dx90.vtx" else d,
    lambda ext, d: b"IDST" + bytes(range(256)) * 4 if ext == ".mdl" else d,
])
def test_corrupt_model_becomes_placeholder(fs, tmp_path, mutate):
    from ad2render.convert import Converter
    base = "models/props_junk/trafficcone001a"
    meta = Converter(_StubFS(fs, base, mutate), tmp_path).model(base + ".mdl")
    assert meta["placeholder"]
    assert Path(meta["glb"]).exists()


def test_stale_placeholder_is_rebuilt(fs, tmp_path):
    from ad2render.convert import Converter
    base = "models/props_junk/trafficcone001a"
    missing = _StubFS(fs, base, lambda ext, d: None)
    assert Converter(missing, tmp_path).model(base + ".mdl")["placeholder"]
    # the addon got installed: the cached placeholder must not stick
    assert not Converter(_StubFS(fs, base, lambda ext, d: d), tmp_path).model(base + ".mdl")["placeholder"]


def _bounds(meshes):
    pts = [p for m in meshes for p in m.positions]
    return [min(p[i] for p in pts) for i in range(3)], [max(p[i] for p in pts) for i in range(3)]


@pytest.mark.parametrize("model", ["models/props_lab/keypad.mdl", "models/props_combine/breenchair.mdl",
                                   "models/nova/chair_office01.mdl"])
def test_rest_pose_matches_collision_hull(fs, model):
    """These models keep their root rotation in the default sequence: raw VVD vertices are turned 90 degrees
    relative to the physics hull, the posed mesh lines up with it (as in game)."""
    from ad2render.mdl import load_meshes, read_header
    from ad2render.pose import apply_rest_pose
    base = model[:-4]
    mdl = fs.read(model)
    h = read_header(mdl)
    meshes = load_meshes(h, mdl, fs.read(base + ".vvd"), fs.read(base + ".dx90.vtx"))
    lo, hi = _bounds(meshes)
    raw_err = max(abs(a - b) for a, b in zip(lo[:2] + hi[:2], list(h.hull[0][:2]) + list(h.hull[1][:2])))
    assert apply_rest_pose(h, mdl, meshes)
    lo, hi = _bounds(meshes)
    posed_err = max(abs(a - b) for a, b in zip(lo[:2] + hi[:2], list(h.hull[0][:2]) + list(h.hull[1][:2])))
    assert posed_err < 2.0 and posed_err < raw_err


def test_static_props_keep_bind_pose(fs):
    from ad2render.mdl import load_meshes, read_header
    from ad2render.pose import apply_rest_pose
    base = "models/hunter/plates/plate1x1"
    mdl = fs.read(base + ".mdl")
    h = read_header(mdl)
    meshes = load_meshes(h, mdl, fs.read(base + ".vvd"), fs.read(base + ".dx90.vtx"))
    assert not apply_rest_pose(h, mdl, meshes)



@pytest.mark.parametrize("kind", ["grass", "concrete", "dirt"])
def test_ground_textures(fs, tmp_path, kind):
    from ad2render.convert import Converter
    from ad2render.ground import ground_texture
    tex = ground_texture(Converter(fs, tmp_path), kind)
    assert Path(tex["png"]).exists() and tex["tile"] > 0 and len(tex["avg"]) == 3



def test_wireframe_material_is_a_flag_not_a_missing_texture(fs, tmp_path):
    """models/wireframe has no texture: the override must resolve and carry the wireframe flag."""
    from ad2render.convert import Converter
    from ad2render.material import parse_vmt
    assert parse_vmt('"Wireframe"\n{\n\t"$model" 1\n}\n').wireframe
    assert parse_vmt('"UnlitGeneric"\n{\n\t"$wireframe" 1\n}\n').wireframe
    assert not parse_vmt('"VertexLitGeneric"\n{\n\t"$basetexture" "x"\n}\n').wireframe
    conv = Converter(fs, tmp_path)
    ov = conv.override_texture("models/wireframe")
    assert ov is not None and ov["wireframe"] and ov["png"] is None
    plain = conv.override_texture("phoenix_storms/metalfloor_2-3")
    assert plain is None or not plain["wireframe"]
