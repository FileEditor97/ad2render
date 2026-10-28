"""Web viewer API tests against a real server on a random port. Needs a GMod install for model
resolution."""
import http.client
import json
import re
import threading
from pathlib import Path

import pytest

from ad2render.gamefs import find_gmod

from dupes import PASSWORD, sample_dupe

gmod = find_gmod()
pytestmark = pytest.mark.skipif(gmod is None, reason="Garry's Mod install not found")


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    from ad2render.convert import Converter
    from ad2render.gamefs import GameFS
    from ad2render.server import App, make_server
    dupes = tmp_path_factory.mktemp("dupes")
    for name in ("b.txt", "a.txt", "sub/c.txt"):
        (dupes / name).parent.mkdir(exist_ok=True)
        (dupes / name).write_bytes(sample_dupe())
    fs = GameFS(gmod)
    app = App(fs, Converter(fs, tmp_path_factory.mktemp("cache")), dupe_dir=dupes, log=lambda *_: None)
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    app.close()


def call(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    ctype = r.getheader("Content-Type", "")
    return r.status, json.loads(data) if "json" in ctype else data


def test_static_and_config(server):
    s, html = call(server, "GET", "/")
    assert s == 200 and b"<title>ad2render</title>" in html
    s, js = call(server, "GET", "/static/viewer.js")
    assert s == 200 and b"OrbitControls" in js
    s, cfg = call(server, "GET", "/api/config")
    assert s == 200 and cfg["files"] and "engines" not in cfg
    s, files = call(server, "GET", "/api/files")
    assert files["files"] == ["a.txt", "b.txt", "sub/c.txt"]


def test_upload_scene_and_assets(server):
    raw = sample_dupe()
    s, d = call(server, "POST", "/api/scene?name=sample.txt", raw, {"Content-Length": str(len(raw))})
    assert s == 200 and len(d["scene"]["entities"]) == 10
    assert d["scene"]["report"]["missing_models"] == []
    text = json.dumps(d)
    assert ":\\\\" not in text and "/cache/" not in text  # no server paths leak to the browser
    ent = d["scene"]["entities"][0]
    s, glb = call(server, "GET", ent["glb"])
    assert s == 200 and glb[:4] == b"glTF"
    tex = next(e["override"]["texture"] for e in d["scene"]["entities"]
               if e["override"] and e["override"]["texture"])
    s, png = call(server, "GET", tex)
    assert s == 200 and png[:4] == b"\x89PNG"


@pytest.mark.parametrize("method, path, body, status", [
    ("GET", "/static/..%2Fserver.py", None, 404),
    ("GET", "/static/vendor/..%2F..%2Fserver.py", None, 404),
    ("GET", "/static/vendor/three/../../../server.py", None, 404),
    ("GET", "/static/vendor/three/addons/missing.js", None, 404),
    ("GET", "/static/vendor", None, 404),
    ("POST", "/api/render", b"{}", 404),                       # server-side rendering no longer exists
    ("GET", "/api/jobs/0123456789abcdef", None, 404),
    ("GET", "/api/glb/../../x.glb", None, 404),
    ("GET", "/api/glb/0123456789abcdef0123.glb", None, 404),   # well-formed but never issued
    ("POST", "/api/scene?file=../pyproject.toml", None, 400),
    ("POST", "/api/scene", b"garbage", 400),
])
def test_rejects_bad_requests(server, method, path, body, status):
    headers = {"Content-Length": str(len(body))} if body is not None else {}
    s, _ = call(server, method, path, body, headers)
    assert s == status


def test_the_three_js_libraries_are_served_from_this_server(server):
    for path in ("three.module.js", "addons/controls/OrbitControls.js", "addons/loaders/GLTFLoader.js",
                 "addons/environments/RoomEnvironment.js", "addons/utils/BufferGeometryUtils.js", "LICENSE"):
        s, body = call(server, "GET", f"/static/vendor/three/{path}")
        assert s == 200 and len(body) > 500, path
    html = call(server, "GET", "/")[1]
    assert b"/static/vendor/three/three.module.js" in html and b"cdn." not in html     # no CDN any more


def test_every_answer_refuses_to_be_framed_by_other_sites_and_pages_get_their_policy(server):
    from ad2render.server import OTHER_CSP, page_csp
    csp = {}
    for path in ("/", "/api/config", "/static/viewer.js", "/static/favicon.svg", "/nope"):
        conn = http.client.HTTPConnection("127.0.0.1", server, timeout=10)
        conn.request("GET", path)
        res = conn.getresponse()
        body = res.read()
        assert res.getheader("X-Frame-Options") == "SAMEORIGIN", path
        csp[path] = res.getheader("Content-Security-Policy")
        assert csp[path] == (page_csp(body) if path == "/" else OTHER_CSP), path
    assert csp["/"].count("sha256-") == 1                                # the viewer's import map


def test_the_server_header_does_not_name_the_python_version(server):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=10)
    conn.request("GET", "/api/config")
    res = conn.getresponse()
    res.read()
    assert res.getheader("Server") == "ad2render"


def test_scene_sends_keypad_links_but_never_the_password(server):
    raw = sample_dupe()
    s, d = call(server, "POST", "/api/scene?name=sample.txt", raw, {"Content-Length": str(len(raw))})
    assert s == 200 and len(d["scene"]["links"]) == 3
    assert sum(1 for e in d["scene"]["entities"] if e["numpad"]) == 6
    assert not re.search(rf"(?<![\d.]){PASSWORD}(?![\d.])", json.dumps(d)) and "Password" not in json.dumps(d)
