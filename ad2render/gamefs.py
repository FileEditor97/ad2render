"""Read-only virtual filesystem over a Garry's Mod install.

Sources, in lookup priority order:
  loose garrysmod/ folders, garrysmod/addons/*.gma, workshop content (.gma / legacy .bin),
  garrysmod/cache/workshop/*.gma, and every *_dir.vpk under garrysmod/ and sourceengine/.
"""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

from .codec import lzma_decompress

VPK_SIG = 0x55AA1234


def _cstr(f) -> bytes:
    out = bytearray()
    while True:
        c = f.read(1)
        if not c or c == b"\0":
            return bytes(out)
        out += c


def _norm(p: str) -> str:
    return p.replace("\\", "/").lower().strip("/")


class VPK:
    def __init__(self, dir_path: Path):
        self.dir_path = dir_path
        self.prefix = str(dir_path)[: -len("_dir.vpk")]
        self.entries: dict[str, tuple] = {}
        with open(dir_path, "rb") as f:
            sig, ver, tree_size = struct.unpack("<III", f.read(12))
            if sig != VPK_SIG:
                raise ValueError(f"{dir_path}: not a VPK")
            if ver == 2:
                f.read(16)
            header = f.tell()
            self.data_start = header + tree_size
            while ext := _cstr(f).decode("utf-8", "replace"):
                while path := _cstr(f).decode("utf-8", "replace"):
                    while name := _cstr(f).decode("utf-8", "replace"):
                        _crc, preload, arch, off, length, _term = struct.unpack("<IHHIIH", f.read(18))
                        pre = f.read(preload) if preload else b""
                        d = "" if path == " " else path + "/"
                        e = "" if ext == " " else "." + ext
                        self.entries[_norm(d + name + e)] = (arch, off, length, pre)

    def read(self, path: str) -> bytes | None:
        ent = self.entries.get(path)
        if ent is None:
            return None
        arch, off, length, pre = ent
        if length == 0:
            return pre
        if arch == 0x7FFF:
            fn, off = self.dir_path, off + self.data_start
        else:
            fn = f"{self.prefix}_{arch:03d}.vpk"
        with open(fn, "rb") as f:
            f.seek(off)
            return pre + f.read(length)


class GMA:
    def __init__(self, path: Path, blob: bytes | None = None):
        self.path = path
        self.blob = blob  # in-memory data for decompressed legacy .bin addons
        self.entries: dict[str, tuple[int, int]] = {}
        f = open(path, "rb") if blob is None else _BytesReader(blob)
        try:
            if f.read(4) != b"GMAD":
                raise ValueError(f"{path}: not a GMA")
            ver = f.read(1)[0]
            f.read(16)  # steamid, timestamp
            if ver > 1:
                while _cstr(f):
                    pass
            _cstr(f), _cstr(f), _cstr(f)
            f.read(4)
            files = []
            while True:
                (num,) = struct.unpack("<I", f.read(4))
                if num == 0:
                    break
                name = _cstr(f).decode("utf-8", "replace")
                size, _crc = struct.unpack("<qI", f.read(12))
                files.append((name, size))
            off = f.tell()
            for name, size in files:
                self.entries[_norm(name)] = (off, size)
                off += size
        finally:
            if blob is None:
                f.close()

    def read(self, path: str) -> bytes | None:
        ent = self.entries.get(path)
        if ent is None:
            return None
        off, size = ent
        if self.blob is not None:
            return self.blob[off:off + size]
        with open(self.path, "rb") as f:
            f.seek(off)
            return f.read(size)


class _BytesReader:
    def __init__(self, b: bytes):
        self.b, self.p = b, 0

    def read(self, n: int) -> bytes:
        r = self.b[self.p:self.p + n]
        self.p += len(r)
        return r

    def tell(self) -> int:
        return self.p

    def close(self):
        pass


class LooseDir:
    """A plain folder. Lookups are case-insensitive even on case-sensitive filesystems,
    because dupes and materials name files in whatever case the author typed."""

    def __init__(self, root: Path):
        self.root = root
        self._listings: dict[Path, dict[str, str] | None] = {}

    def read(self, path: str) -> bytes | None:
        try:
            return (self.root / path).read_bytes()
        except OSError:
            pass
        if sys.platform == "win32":
            return None
        p = self._resolve_ci(path)
        if p is None:
            return None
        try:
            return p.read_bytes()
        except OSError:
            return None

    def _listing(self, d: Path) -> dict[str, str] | None:
        if d not in self._listings:
            try:
                self._listings[d] = {n.lower(): n for n in os.listdir(d)}
            except OSError:
                self._listings[d] = None
        return self._listings[d]

    def _resolve_ci(self, path: str) -> Path | None:
        cur = self.root
        for seg in path.split("/"):
            names = self._listing(cur)
            if not names or seg.lower() not in names:
                return None
            cur = cur / names[seg.lower()]
        return cur


def find_gmod() -> Path | None:
    cands = []
    if env := os.environ.get("GMOD_PATH"):
        cands.append(Path(env))
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                steam = Path(winreg.QueryValueEx(k, "SteamPath")[0])
            cands.append(steam / "steamapps/common/GarrysMod")
            lib = steam / "steamapps/libraryfolders.vdf"
            if lib.exists():
                import re
                for m in re.finditer(r'"path"\s+"([^"]+)"', lib.read_text(errors="replace")):
                    cands.append(Path(m.group(1).replace("\\\\", "\\")) / "steamapps/common/GarrysMod")
        except OSError:
            pass
    cands += [Path.home() / ".steam/steam/steamapps/common/GarrysMod",
              Path.home() / ".local/share/Steam/steamapps/common/GarrysMod"]
    for c in cands:
        if (c / "garrysmod").is_dir():
            return c
    return None


def _mount_cfg(path: Path) -> list[Path]:
    """Extra game folders from cfg/mount.cfg ("cstrike" "C:\\...\\cstrike")."""
    import re
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        if line.strip().startswith("//"):
            continue
        m = re.match(r'\s*"([^"]+)"\s+"([^"]+)"', line)
        if m and Path(m.group(2)).is_dir():
            out.append(Path(m.group(2)))
    return out


def _safe(p: str) -> bool:
    """Reject asset paths that could escape the content roots (names come from untrusted dupes)."""
    return bool(p) and ":" not in p and chr(0) not in p and ".." not in p.split("/")


class GameFS:
    def __init__(self, gmod_root: Path | str | None = None, verbose: bool = False,
                 workshop_dir: Path | str | None = None):
        root = Path(gmod_root) if gmod_root else find_gmod()
        if root is None or not (root / "garrysmod").is_dir():
            raise FileNotFoundError("Garry's Mod install not found; pass --gmod or set GMOD_PATH")
        self.root = root
        self.sources: list = []
        self.warnings: list[str] = []
        gm = root / "garrysmod"
        self.sources.append(LooseDir(gm))
        gmas = sorted((gm / "addons").glob("*.gma"))
        # Steam client layout by default; SteamCMD downloads elsewhere (AD2RENDER_WORKSHOP)
        workshop = Path(workshop_dir or os.environ.get("AD2RENDER_WORKSHOP")
                        or root.parent.parent / "workshop/content/4000")
        if workshop.is_dir():
            for d in sorted(workshop.iterdir()):
                gmas += sorted(d.glob("*.gma")) if d.is_dir() else []
        gmas += [g for g in sorted((gm / "cache/workshop").glob("*.gma"))
                 if not (gm / "addons" / g.name).exists()]
        # addons/<id>.gma and cache/workshop/<id>.gma duplicate workshop/content/4000/<id>/
        have_ids = {g.stem for g in gmas if g.parent.parent != workshop}
        for g in gmas:
            self._add(lambda g=g: GMA(g), g)
        if workshop.is_dir():
            for b in sorted(workshop.glob("*/*.bin")):
                if b.parent.name in have_ids:
                    continue
                self._add(lambda b=b: GMA(b, lzma_decompress(b.read_bytes(), 1 << 31)), b)
        # legacy extracted addons (garrysmod/addons/<name>/models/...)
        for d in sorted(p for p in (gm / "addons").glob("*/") if p.is_dir()):
            self.sources.append(LooseDir(d))
        for d in (gm, root / "sourceengine"):
            self._add_vpks(d)
        for d in _mount_cfg(gm / "cfg/mount.cfg"):
            self.sources.append(LooseDir(d))
            self._add_vpks(d)
        self.sources.append(LooseDir(gm / "download"))  # server / FastDL content
        # Extra addons (AD2RENDER_ADDONS) go last: they can only fill in what is missing, never replace a base-game
        # file, so a dupe renders the same whichever addons happen to be installed.
        self._mounted: set[str] = set()
        if env := os.environ.get("AD2RENDER_ADDONS"):
            self.mount_workshop(Path(env))
        if verbose:
            print(f"[gamefs] {len(self.sources)} sources from {root}", file=sys.stderr)

    def mount_workshop(self, root: Path | str) -> int:
        """Add addons found under <root>/steamapps/workshop/content/4000/<id>/ that are not mounted yet, at the
        LOWEST priority. The source list is rebuilt and assigned in one step because request threads read it
        while this runs. Returns how many new addons were mounted."""
        content = Path(root) / "steamapps/workshop/content/4000"
        fresh = []
        if content.is_dir():
            for d in sorted(p for p in content.iterdir() if p.is_dir()):
                for g in sorted(d.glob("*.gma")):
                    if str(g) not in self._mounted:
                        fresh.append((str(g), lambda g=g: GMA(g)))
                for b in sorted(d.glob("*.bin")):
                    if str(b) not in self._mounted:
                        fresh.append((str(b), lambda b=b: GMA(b, lzma_decompress(b.read_bytes(), 1 << 31))))
        added = []
        for key, make in fresh:
            try:
                added.append(make())
                self._mounted.add(key)
            except Exception as e:  # corrupt download: skip it, but say so
                self.warnings.append(f"skipped {key}: {e}")
        if added:
            self.sources = [*self.sources, *added]
        return len(added)

    def _add_vpks(self, d: Path):
        for v in sorted(d.glob("*_dir.vpk")):
            if "sound" not in v.name:
                self._add(lambda v=v: VPK(v), v)

    def _add(self, make, path):
        try:
            self.sources.append(make())
        except Exception as e:  # corrupt addon: skip, but report
            self.warnings.append(f"skipped {path}: {e}")

    def read(self, path: str) -> bytes | None:
        p = _norm(path)
        if not _safe(p):
            return None
        for s in self.sources:
            data = s.read(p)
            if data is not None:
                return data
        return None

    def exists(self, path: str) -> bool:
        return self.read(path) is not None
