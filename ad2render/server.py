"""Web server for the local three.js viewer.

Standard library only. Scenes live server-side; the browser gets unguessable asset URLs, never file paths.
All rendering happens in the visitor's browser (GPU when there is one, software otherwise).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sys
import threading
from collections import OrderedDict
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .codec import MAX_DUPE_SIZE, DecodeError, decode
from .convert import resolve_scene
from .ground import GROUND_MATERIALS, footprint, ground_texture
from .minify import minified
from .scene import normalize

VIEWER_DIR = Path(__file__).with_name("viewer")
VENDOR_DIR = VIEWER_DIR / "vendor"      # third-party browser libraries shipped with the project (three.js)
MAX_SCENES = 32
_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".png": "image/png", ".webp": "image/webp", ".glb": "model/gltf-binary",
          ".json": "application/json", ".svg": "image/svg+xml", ".txt": "text/plain; charset=utf-8"}


class BadRequest(Exception):
    pass


# Content-Security-Policy. Pages run only this site's own scripts, plus the inline <script> / <style> blocks of the
# page files in viewer/, allowed by hash (so an edited page needs no list kept by hand). Only those trusted files are
# hashed: HTML built at runtime gets no inline script, whatever its text says. Renders and textures are data: / blob:
# URLs. Only same-origin frames.
# Everything that is not a page gets a policy that allows nothing, in case a browser is made to open it as one.
PAGE_CSP = ("default-src 'self'; script-src 'self'{script}; style-src 'self'{style}; "
            "img-src 'self' data: blob:; connect-src 'self' data: blob:; "
            "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'")
OTHER_CSP = "default-src 'none'; frame-ancestors 'self'"
_INLINE = re.compile(rb"<(script|style)\b(?![^>]*\bsrc=)[^>]*>(.*?)</\1\s*>", re.S | re.I)


@lru_cache(maxsize=32)
def page_csp(body: bytes) -> str:
    """The policy for one trusted HTML page (a file of ours, never text built from requests), with a hash for each of
    its inline scripts and styles."""
    hashes = {"script": "", "style": ""}
    for tag, text in _INLINE.findall(body):
        text = text.replace(b"\r\n", b"\n").replace(b"\r", b"\n")   # what the browser hashes, after the HTML parser
        hashes[tag.lower().decode()] += f" 'sha256-{base64.b64encode(hashlib.sha256(text).digest()).decode()}'"
    return PAGE_CSP.format(**hashes)


class App:
    def __init__(self, fs, conv, *, dupe_dir: Path | None = None, log=print):
        self.fs, self.conv = fs, conv
        self.dupe_dir = dupe_dir.resolve() if dupe_dir else None
        self.log = log
        self.scenes: OrderedDict[str, dict] = OrderedDict()
        self.assets: dict[str, Path] = {}     # url name -> file (glb / texture png)
        self._salt = secrets.token_bytes(16)  # asset URLs are unguessable: hash(salt, path)
        self.lock = threading.Lock()          # scene store + converter (writes cache files)

    def close(self):
        pass

    # Scenes ---------------------------------------------------------------
    def load_dupe(self, raw: bytes, name: str) -> dict:
        sid = hashlib.sha1(raw).hexdigest()[:20]
        with self.lock:
            if sid not in self.scenes:
                scene = normalize(decode(raw), name=name)
                resolve_scene(scene, self.conv)
                self.scenes[sid] = scene
                while len(self.scenes) > MAX_SCENES:
                    self.scenes.popitem(last=False)
            self.scenes.move_to_end(sid)
            return {"id": sid, "scene": self._client_scene(self.scenes[sid])}

    def _asset_url(self, path: str, kind: str) -> str:
        p = Path(path)
        name = hmac.new(self._salt, str(p).encode(), hashlib.sha1).hexdigest()[:20] + p.suffix.lower()
        self.assets[name] = p
        return f"/api/{kind}/{name}"

    def _client_scene(self, s: dict) -> dict:
        ents = []
        for e in s["entities"]:
            ov = e.get("override")
            ents.append({
                "id": e["id"], "class": e["class"], "model": e["model"], "skin": e["skin"],
                "color": e["color"], "rendermode": e["rendermode"],
                "matrix": e["parts"][0]["matrix"], "posed": e.get("posed", False),
                "placeholder": e.get("placeholder", False), "bounds": e.get("bounds"),
                "fading_door": e.get("fading_door", False), "transparent": e.get("transparent", False),
                "numpad": e.get("numpad"),
                "glb": self._asset_url(e["glb"], "glb"),
                "override": None if not ov else {
                    "texture": self._asset_url(ov["png"], "tex") if ov.get("png") else None,
                    "avg": ov["avg"], "wireframe": bool(ov.get("wireframe")), "name": e.get("material_override")},
            })
        cx, cy, side = footprint(s)
        return {"name": s["name"], "info": s["info"], "entities": ents,
                "ground": {"z": s.get("ground_z", 0.0), "center": [cx, cy], "size": side},
                "constraints": s["constraints"], "links": s.get("links", []), "report": s.get("report", {})}

    def ground_info(self, kind: str) -> dict:
        """Texture URL, tile size (inches) and average colour of a textured ground kind."""
        if kind not in GROUND_MATERIALS:
            raise BadRequest("unknown ground")
        with self.lock:
            tex = ground_texture(self.conv, kind)
        if tex is None:
            raise BadRequest("this ground texture is not in the installed game content")
        return {"texture": self._asset_url(tex["png"], "tex"), "tile": tex["tile"], "color": tex["avg"]}

    def list_files(self) -> list[str]:
        if not self.dupe_dir:
            return []
        return sorted(str(p.relative_to(self.dupe_dir)).replace("\\", "/")
                      for p in self.dupe_dir.rglob("*.txt"))[:5000]

    def dir_file(self, rel: str) -> Path:
        if not self.dupe_dir:
            raise BadRequest("server was started without --dir")
        p = (self.dupe_dir / rel).resolve()
        if self.dupe_dir not in p.parents or p.suffix.lower() != ".txt" or not p.is_file():
            raise BadRequest("no such dupe")
        return p


def static_file(path: str) -> Path | None:
    """Resolve /static/<path> to a file inside the viewer folder, or None. Handles the flat page assets and the
    vendored libraries (/static/vendor/...); nothing outside those folders is ever reachable."""
    if not re.fullmatch(r"[\w./-]+", path) or ".." in path.split("/"):
        return None
    f = (VIEWER_DIR / path).resolve()
    inside_viewer = f.parent == VIEWER_DIR.resolve()
    inside_vendor = VENDOR_DIR.resolve() in f.parents
    return f if (inside_viewer or inside_vendor) and f.is_file() else None


# HTTP -------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    app: App = None  # set by make_server
    server_version = "ad2render"

    def version_string(self):
        return self.server_version        # the default appends "Python/x.y.z", which tells visitors the interpreter version

    def log_message(self, fmt, *args):
        self.app.log(f"{self.address_string()} {fmt % args}")

    def _send(self, status, body: bytes, ctype: str, cache: bool = False, headers=(), csp: str | None = None):
        """csp: the policy for an HTML page; by default HTML may run no inline script or style at all."""
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if "cache-control" not in {k.lower() for k, _ in headers}:
            self.send_header("Cache-Control", "public, max-age=86400" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")    # (older browsers; frame-ancestors says the same)
        html = ctype.startswith("text/html")
        self.send_header("Content-Security-Policy", (csp or page_csp(b"")) if html else OTHER_CSP)
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=HTTPStatus.OK, headers=()):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json", headers=headers)

    _body_read = False
    MAX_DRAIN = 40 * 1024 * 1024

    def _drain(self):
        """Read and discard an unread request body before answering with an error. Closing a socket that still
        has unread data makes the peer see a connection reset instead of our response."""
        if self._body_read or self.command not in ("POST", "PUT", "PATCH", "DELETE"):
            return
        self._body_read = True
        try:
            left = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            left = 0
        if left > self.MAX_DRAIN:
            self.close_connection = True  # not worth reading; the client will have to retry smaller
            return
        while left > 0:
            chunk = self.rfile.read(min(left, 65536))
            if not chunk:
                break
            left -= len(chunk)

    def _error(self, status, msg):
        self._drain()
        self._json({"error": msg}, status)

    def _file(self, path: Path, cache=False):
        try:
            data = path.read_bytes()
        except OSError:
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        data = minified(path, data) or data      # .js / .css are sent minified; the files on disk stay as written
        trusted = path.suffix.lower() == ".html" and path.parent == VIEWER_DIR   # our own pages: their inline blocks run
        self._send(HTTPStatus.OK, data, _TYPES.get(path.suffix.lower(), "application/octet-stream"), cache,
                   csp=page_csp(data) if trusted else None)

    def _body(self, limit: int) -> bytes:
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise BadRequest("Content-Length required") from None
        if n <= 0 or n > limit:
            raise BadRequest(f"body must be 1..{limit} bytes")
        self._body_read = True
        return self.rfile.read(n)

    def do_GET(self):
        url = urlparse(self.path)
        p = unquote(url.path)
        try:
            if p in ("/", "/index.html"):
                return self._file(VIEWER_DIR / "index.html")
            if m := re.fullmatch(r"/static/(.+)", p):
                f = static_file(m.group(1))
                return self._file(f) if f else self._error(404, "not found")
            if p == "/api/config":
                return self._json({"files": bool(self.app.dupe_dir)})
            if m := re.fullmatch(r"/api/ground/([a-z]+)", p):
                return self._json(self.app.ground_info(m.group(1)))
            if p == "/api/files":
                return self._json({"files": self.app.list_files()})
            if m := re.fullmatch(r"/api/(glb|tex)/([0-9a-f]+)\.(glb|png)", p):
                f = self.app.assets.get(f"{m.group(2)}.{m.group(3)}")
                return self._file(f, cache=True) if f else self._error(404, "not found")
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        except BadRequest as e:
            return self._error(HTTPStatus.BAD_REQUEST, str(e))

    def do_POST(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path == "/api/scene":  # opening a file or a folder entry
                if "file" in q:
                    f = self.app.dir_file(q["file"][0])
                    raw, name = f.read_bytes(), f.stem
                else:
                    raw = self._body(MAX_DUPE_SIZE)
                    name = Path(q.get("name", ["upload"])[0]).stem[:120] or "upload"
                return self._json(self.app.load_dupe(raw, name))
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        except (BadRequest, DecodeError) as e:
            return self._error(HTTPStatus.BAD_REQUEST, str(e))
        except Exception as e:  # keep the server up; tell the page what went wrong
            self.app.log(f"error handling {url.path}: {e!r}")
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"server error: {e}")


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def serve(app: App, host="127.0.0.1", port=8000, open_browser=False):
    srv = make_server(app, host, port)
    url = f"http://{'localhost' if host in ('0.0.0.0', '::') else host}:{srv.server_address[1]}/"
    print(f"ad2render viewer on {url}", file=sys.stderr)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        app.close()
