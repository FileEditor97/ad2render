"""Decoder for Advanced Duplicator 2 files (AD2F revisions 3-5).

Ported from wiremod/advdupe2 lua/advdupe2/sh_codec.lua.
"""
from __future__ import annotations

import lzma
import struct
from dataclasses import dataclass, field

MAX_DUPE_SIZE = 32_000_000
CHECK = b"\r\n\t\n"


class DecodeError(Exception):
    pass


@dataclass
class Vector:
    x: float
    y: float
    z: float

    def to_json(self):
        return {"__type": "Vector", "x": self.x, "y": self.y, "z": self.z}


@dataclass
class Angle:
    p: float
    y: float
    r: float

    def to_json(self):
        return {"__type": "Angle", "p": self.p, "y": self.y, "r": self.r}


@dataclass
class Dupe:
    revision: int
    info: dict[str, str]
    data: dict = field(default_factory=dict)


def lzma_decompress(blob: bytes, max_size: int = MAX_DUPE_SIZE) -> bytes:
    """Decompress GMod util.Compress output (5 props bytes, 8-byte LE size, raw LZMA)."""
    if len(blob) < 13:
        raise DecodeError("Compressed payload too short (truncated file?)")
    props = blob[:5]
    size = struct.unpack_from("<Q", blob, 5)[0]
    if size > max_size:
        raise DecodeError(f"Uncompressed size {size} exceeds limit {max_size}")
    d = props[0]
    if d >= 9 * 5 * 5:
        raise DecodeError("Invalid LZMA properties byte")
    lc, rest = d % 9, d // 9
    lp, pb = rest % 5, rest // 5
    dict_size = struct.unpack_from("<I", props, 1)[0]
    filt = {"id": lzma.FILTER_LZMA1, "lc": lc, "lp": lp, "pb": pb,
            "dict_size": max(dict_size, 4096)}
    dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[filt])
    try:
        out = dec.decompress(blob[13:], max_length=size)
    except lzma.LZMAError as e:
        raise DecodeError(f"LZMA decompression failed: {e} (file corrupted or text-mangled?)") from e
    if len(out) != size:
        raise DecodeError(f"Decompressed {len(out)} bytes, header promised {size} (truncated file?)")
    return out


def _key(k):
    """Integral Lua numbers act as integer keys."""
    if isinstance(k, float) and k.is_integer():
        return int(k)
    if isinstance(k, (dict, Vector, Angle)):
        return id(k)
    return k


class _Reader:
    def __init__(self, buf: bytes, revision: int, max_values: int | None = None):
        self.buf = buf
        self.pos = 0
        self.tables: dict[int, object] = {}
        self.reference = 0
        self.rev = revision
        self.values_left = max_values  # None = no cap; one-byte values make 32 MB of payload gigabytes of objects

    def byte(self) -> int | None:
        if self.pos >= len(self.buf):
            return None
        b = self.buf[self.pos]
        self.pos += 1
        return b

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.buf):
            raise DecodeError(f"Unexpected EOF reading {n} bytes at offset {self.pos}")
        b = self.buf[self.pos:self.pos + n]
        self.pos += n
        return b

    def double(self) -> float:
        return struct.unpack("<d", self.take(8))[0]

    def short(self) -> int:
        return struct.unpack("<h", self.take(2))[0]

    def ulong(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    @staticmethod
    def _str(b: bytes) -> str:
        return b.decode("utf-8", errors="surrogateescape")

    def read(self):
        tt = self.byte()
        if tt is None:
            raise DecodeError("Expected value, got EOF!")
        if self.values_left is not None:
            self.values_left -= 1
            if self.values_left < 0:
                raise DecodeError("Dupe holds too many values")
        return self._read5(tt) if self.rev >= 5 else self._read4(tt)

    def _common(self, tt: int):
        if tt == 253:
            return True
        if tt == 252:
            return False
        if tt == 251:
            return self.double()
        if tt == 250:
            return Vector(self.double(), self.double(), self.double())
        if tt == 249:
            return Angle(self.double(), self.double(), self.double())
        raise DecodeError(f"Couldn't find deserializer for type {tt} at offset {self.pos - 1}")

    # Revision 5 -----------------------------------------------------------
    def _read5(self, tt: int):
        if tt == 255:  # table
            t: dict = {}
            self.reference += 1
            self.tables[self.reference] = t
            while (k := self.read()) is not None:
                t[_key(k)] = self.read()
            return t
        if tt == 254:  # array
            t = {}
            self.reference += 1
            self.tables[self.reference] = t
            i = 1
            while (v := self.read()) is not None:
                t[i] = v
                i += 1
            return t
        if tt == 248:  # long string
            return self._str(self.take(self.ulong()))
        if tt == 247:  # table reference
            return self.tables.get(self.short())
        if tt == 246:  # nil / terminator
            return None
        if tt == 0:
            return ""
        if tt <= 245:
            return self._str(self.take(tt))
        return self._common(tt)

    # Revision 4 (and 3) ---------------------------------------------------
    def _read4(self, tt: int):
        if tt == 0:
            return None
        if tt == 255:
            t = {}
            self.reference += 1
            ref = self.reference
            while (k := self.read()) is not None:
                t[_key(k)] = self.read()
            self.tables[ref] = t
            return t
        if tt == 254:
            t = {}
            self.reference += 1
            ref = self.reference
            i = 1
            while (v := self.read()) is not None:
                t[i] = v
                i += 1
            self.tables[ref] = t
            return t
        if tt == 248:  # null-terminated string
            end = self.buf.find(b"\0", self.pos)
            if end < 0:
                end = len(self.buf)
            s = self.buf[self.pos:end]
            self.pos = min(end + 1, len(self.buf))
            return self._str(s)
        if tt == 247:
            self.reference += 1  # quirk of the original rev-4 decoder
            return self.tables.get(self.short())
        if tt <= 246:
            return self._str(self.take(tt))
        return self._common(tt)


def parse_header(raw: bytes) -> tuple[int, dict[str, str], bytes]:
    if len(raw) < 6:
        raise DecodeError("Malformed dupe (too short)")
    sig = raw[:4]
    if sig == b"[Inf":
        raise DecodeError("Advanced Duplicator 1 files are not supported")
    if sig != b"AD2F":
        raise DecodeError(f"Unknown duplication format (signature {sig!r})")
    rev = raw[4]
    if rev > 5:
        raise DecodeError(f"Newer codec needed (have rev 5, file is rev {rev})")
    if rev < 3:
        raise DecodeError(f"Legacy revision {rev} is not supported (only 3-5)")
    if rev == 3:
        raw = (raw.replace(b"\r\r\n\t\r\n", b"\t\t\t\t").replace(CHECK, b"\t\t\t\t")
               .replace(b"\r\n", b"\n").replace(b"\t\t\t\t", CHECK))
    body = raw[6:]
    last = body.find(b"\2")
    if last < 0:
        raise DecodeError("Malformed info block (no terminator)")
    parts = body[:last].split(b"\1")
    info: dict[str, str] = {}
    raw_info: dict[str, bytes] = {}
    for i in range(0, len(parts) - 1, 2):
        k = parts[i].decode("utf-8", "replace")
        raw_info[k] = parts[i + 1]
        info[k] = parts[i + 1].decode("utf-8", "replace")
    check = raw_info.get("check")
    if check != CHECK:
        if check == b"\n\t\n":
            raise DecodeError("Check field mismatch: newlines homogenized in transfer "
                              "(use binary mode for FTP)")
        if check is not None:
            raise DecodeError("Check field mismatch: file was text-mangled "
                              "(newline replacements from copy/paste or an editor)")
        raise DecodeError("Malformed info block (missing check field)")
    info.pop("check", None)
    return rev, info, body[last + 2:]


def decode(raw: bytes, max_values: int | None = None) -> Dupe:
    """max_values caps how many values (table entries, strings, numbers...) the file may hold."""
    rev, info, payload = parse_header(raw)
    buf = lzma_decompress(payload)
    r = _Reader(buf, rev, max_values)
    try:
        data = r.read()
    except (struct.error, RecursionError) as e:
        raise DecodeError(f"Deserialization failed: {e}") from e
    if not isinstance(data, dict):
        raise DecodeError("Top-level value is not a table")
    _validate(data)
    return Dupe(rev, info, data)


def decode_file(path) -> Dupe:
    with open(path, "rb") as f:
        return decode(f.read())


def _validate(d: dict):
    head = d.get("HeadEnt")
    if not isinstance(head, dict):
        raise DecodeError("Missing HeadEnt table")
    if not isinstance(d.get("Entities"), dict):
        raise DecodeError("Missing Entities table")
    for k in ("Z", "Pos", "Index"):
        if head.get(k) is None:
            raise DecodeError(f"Missing HeadEnt.{k}")
    if not isinstance(head["Pos"], Vector):
        raise DecodeError("HeadEnt.Pos is not a vector")
    for key, ent in d["Entities"].items():
        po = ent.get("PhysicsObjects") if isinstance(ent, dict) else None
        if not isinstance(po, dict) or not isinstance(po.get(0), dict):
            raise DecodeError(f"Missing PhysicsObjects[0] for entity {key}")
        if "Pos" not in po[0] or "Angle" not in po[0]:
            raise DecodeError(f"Missing PhysicsObjects[0].Pos/Angle for entity {key}")


def to_jsonable(obj, _stack=()):
    if isinstance(obj, (Vector, Angle)):
        return obj.to_json()
    if isinstance(obj, dict):
        if id(obj) in _stack:
            return {"__cycle": True}
        s = _stack + (id(obj),)
        return {str(k): to_jsonable(v, s) for k, v in obj.items()}
    if isinstance(obj, float) and obj.is_integer() and abs(obj) < 2**53:
        return int(obj)
    if isinstance(obj, str):
        return obj.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    return obj
