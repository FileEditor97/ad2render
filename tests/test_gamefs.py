"""GameFS behaviour that matters on Linux / in containers. No GMod install needed."""
from pathlib import Path

import pytest

from ad2render.gamefs import GameFS, LooseDir, _safe


def _fake_install(tmp_path: Path) -> Path:
    root = tmp_path / "GarrysMod"
    (root / "garrysmod/download/Models/Props_Foo").mkdir(parents=True)
    (root / "garrysmod/download/Models/Props_Foo/Thing.MDL").write_bytes(b"mdl")
    (tmp_path / "secret.txt").write_bytes(b"secret")
    return root


def test_case_insensitive_resolution(tmp_path):
    root = _fake_install(tmp_path)
    d = LooseDir(root / "garrysmod/download")
    # exercise the fallback directly: on Windows read() never needs it
    p = d._resolve_ci("models/props_foo/thing.mdl")
    assert p is not None and p.read_bytes() == b"mdl"
    assert d._resolve_ci("models/props_foo/missing.mdl") is None


def test_gamefs_reads_mixed_case(tmp_path):
    fs = GameFS(_fake_install(tmp_path), workshop_dir=tmp_path / "none")
    assert fs.read("models/props_foo/thing.mdl") == b"mdl"
    assert fs.read("MODELS\\Props_Foo\\THING.mdl") == b"mdl"


@pytest.mark.parametrize("path", [
    "../../secret.txt", "models/../../../secret.txt", "c:/windows/win.ini", "a\0b", "",
])
def test_unsafe_paths_rejected(tmp_path, path):
    fs = GameFS(_fake_install(tmp_path), workshop_dir=tmp_path / "none")
    assert not _safe(path.replace("\\", "/").lower().strip("/"))
    assert fs.read(path) is None


def test_workshop_dir_override(tmp_path, monkeypatch):
    root = _fake_install(tmp_path)
    ws = tmp_path / "steamcmd/steamapps/workshop/content/4000/123"
    ws.mkdir(parents=True)
    # minimal GMA v3 holding one file
    import struct
    data = b"hello"
    gma = (b"GMAD" + bytes([3]) + b"\0" * 16 + b"\0" + b"name\0desc\0author\0" + struct.pack("<i", 1)
           + struct.pack("<I", 1) + b"models/ws.mdl\0" + struct.pack("<qI", len(data), 0)
           + struct.pack("<I", 0) + data)
    (ws / "addon.gma").write_bytes(gma)
    monkeypatch.setenv("AD2RENDER_WORKSHOP", str(ws.parent))
    assert GameFS(root).read("models/ws.mdl") == b"hello"
