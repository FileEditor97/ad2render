"""The JS/CSS minifier: tricky syntax stays intact, shipped files still parse, and the server sends the small version."""
import http.client
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from ad2render import minify
from ad2render.minify import MinifyError, minify_css, minify_js, _tokens

VIEWER = Path(__file__).resolve().parent.parent / "ad2render" / "viewer"
NODE = shutil.which("node")


def js(src):
    return minify_js(src).strip()


def toks(src):
    return [(k, t) for k, t, _, _ in _tokens(src) if k != "keep"]


@pytest.mark.parametrize("src, want", [
    ("const a = 1;\nlet b = 2;\n", "const a=1;let b=2;"),
    ("  function f ( x , y ) {\n  return x + y ;\n}\n", "function f(x,y){return x+y;}"),
    ("// gone\nx = 1; /* gone */ y = 2;", "x=1;y=2;"),
    ("a = b / c / d;", "a=b/c/d;"),
    ("a = b\n/ c;", "a=b/c;"),
    ("x = /ab+c/gi.test(s);", "x=/ab+c/gi.test(s);"),
    ("x = a ? /re/ : 2;", "x=a?/re/:2;"),
    ("return /x/.test(y)", "return/x/.test(y)"),
    ("x = y / 2 / z", "x=y/2/z"),
    ("const r = /[/]\\/(x)/;", "const r=/[/]\\/(x)/;"),
])
def test_basic_forms(src, want):
    assert js(src) == want


def test_strings_and_templates_are_untouched():
    src = 'a = "  // not a comment  ";\nb = \'/* nor this */\';\nc = `x  ${ a + `${ b }` }  // y\n  z`;\n'
    out = js(src)
    assert '"  // not a comment  "' in out and "'/* nor this */'" in out
    assert "`x  ${ a + `${ b }` }  // y\n  z`" in out
    assert toks(out) == toks(src)


def test_a_comment_marker_inside_a_template_expression():
    src = "x = `${ f(/* c */ 1) } and ${ '}' }`;"
    assert js(src) == "x=`${ f(/* c */ 1) } and ${ '}' }`;"       # the template is copied exactly


def test_words_are_never_glued_together():
    assert js("typeof  x") == "typeof x" and js("a in b") == "a in b" and js("x instanceof Y") == "x instanceof Y"
    assert js("return  1") == "return 1" and js("let  a") == "let a" and js("1 in x") == "1 in x"
    assert js('return "a"') == 'return"a"'


def test_operators_that_would_merge_keep_a_space():
    assert js("a + +b") == "a+ +b" and js("a - -b") == "a- -b" and js("a + ++b") == "a+ ++b"
    assert js("a++ + b") == "a++ +b" and js("a-- - b") == "a-- -b"
    assert js("1 .toString()") == "1 .toString()" and js("1.5.toFixed()") == "1.5.toFixed()"
    assert js("a / /re/.exec(b)") == "a/ /re/.exec(b)"


@pytest.mark.parametrize("src", [
    "x = 1\ny = 2\n",                                  # ASI: the newline ends the statement
    "a++\nb++\n",
    "let a = f\n++b\n",
    "function f() {\n  return\n  1\n}\n",             # return + newline returns undefined
    "var q = 1\nif (q) {}\n",
    "const o = {}\nlet z = 1\n",
    "x\n!y\n",
    "var a = b\nfoo()\n",
])
def test_automatic_semicolon_insertion_still_works(src):
    out = js(src)
    assert "\n" in out or ";" in out
    if NODE:
        run = subprocess.run([NODE, "--check", "-"], input=out, capture_output=True, text=True)
        assert run.returncode == 0, (src, out, run.stderr)


@pytest.mark.skipif(not NODE, reason="node not installed")
@pytest.mark.parametrize("src", [
    "var x = 1\nvar y = 2\nconsole.log(x + y)\n",
    "var a = 1\nvar b = a\n++a\nconsole.log(a, b)\n",
    "function f() {\n  return\n  5\n}\nconsole.log(f())\n",
    "var n = 5\nvar s = n\n/ 2\nconsole.log(s)\n",
    "var t = `a${1 + 1}b`\nconsole.log(t, /x\\/y/.test('x/y'), 4 / 2 / 1)\n",
    "var o = { a: 1 }\nvar k = o.a\nconsole.log(k, -(-k), +(+k), k++ + ++k)\n",
    "label: for (var i = 0; i < 3; i++) {\n  if (i == 1) continue label\n  console.log(i)\n}\n",
    "var f = function () { return 1 }\n;(function () { console.log(f()) })()\n",
])
def test_minified_code_behaves_the_same(src):
    def run(code):
        r = subprocess.run([NODE, "-"], input=code, capture_output=True, text=True)
        assert r.returncode == 0, (code, r.stderr)
        return r.stdout
    assert run(minify_js(src)) == run(src)


def test_license_comments_are_kept_other_comments_go():
    out = minify_js("/*! keep me */\n// drop\n/** drop too */\n/* @license MIT */\nx = 1;\n")
    assert "keep me" in out and "@license MIT" in out and "drop" not in out and out.rstrip().endswith("x=1;")


def test_broken_input_is_reported():
    for bad in ['x = "open', "x = `open", "/* open", "x = /open"]:
        with pytest.raises(MinifyError):
            minify_js(bad)


def test_non_ascii_identifiers_and_text():
    assert js("let π = 1;\nlet ü = π;") == "let π=1;let ü=π;"
    assert js("x = 'héllo';") == "x='héllo';"


# CSS ---------------------------------------------------------------------------------------------------
def test_css_basics():
    src = "/* c */\n.a, .b > .c {\n  color : red;\n  margin: 0 auto ;\n}\n\n@media (min-width: 600px) {\n  .a { top: 1px; }\n}\n"
    assert minify_css(src).strip() == ".a,.b>.c{color :red;margin:0 auto}@media (min-width:600px){.a{top:1px}}"


def test_css_keeps_what_has_meaning():
    assert "calc(1px + 2px)" in minify_css(".a { width: calc(1px + 2px); }")                 # spaces around + are required
    assert "calc(100% - 4px)" in minify_css(".a { width: calc(100% - 4px); }")
    assert ".a :hover" in minify_css(".a :hover { x: y }") and ".a:hover" in minify_css(".a:hover { x: y }")
    assert 'content:"  a;b  "' in minify_css('.a::after { content: "  a;b  "; }')
    assert "url(data:image/png;base64,AA==)" in minify_css(".a { background: url( data:image/png;base64,AA== ); }")
    assert 'url("a b.png")' in minify_css('.a { background: url("a b.png"); }')
    assert ".a+.b" in minify_css(".a + .b { x: y }")
    assert "margin:0 1px" in minify_css(".a { margin: 0   1px; }")
    assert "color:red!important" in minify_css(".a { color: red !important; }")
    assert "/*! license */" in minify_css("/*! license */ .a { x: y }")


# The shipped files and the server ---------------------------------------------------------------------
SHIPPED_JS = sorted([*VIEWER.glob("*.js"), *VIEWER.glob("vendor/three/**/*.js")])


@pytest.mark.parametrize("path", SHIPPED_JS, ids=lambda p: p.name)
def test_shipped_scripts_minify_to_the_same_tokens(path):
    src = path.read_text(encoding="utf-8")
    out = minify_js(src)
    assert len(out) < len(src) * 0.85 or path.stat().st_size < 5000
    assert toks(out) == toks(src)


@pytest.mark.skipif(not NODE, reason="node not installed")
@pytest.mark.parametrize("path", SHIPPED_JS, ids=lambda p: p.name)
def test_shipped_scripts_still_parse(path, tmp_path):
    f = tmp_path / (path.stem + ".mjs")
    f.write_text(minify_js(path.read_text(encoding="utf-8")), encoding="utf-8")
    run = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


def test_cache_follows_the_file(tmp_path):
    f = tmp_path / "a.js"
    f.write_text("let  a  =  1;\n\n// c\n", encoding="utf-8")
    assert minify.minified(f) == b"let a=1;\n"
    f.write_text("let  b  =  2;\n\n// c\n", encoding="utf-8")
    assert minify.minified(f) == b"let b=2;\n"
    broken = tmp_path / "b.js"
    broken.write_text('x = "open', encoding="utf-8")
    assert minify.minified(broken) is None                                    # served as written
    assert minify.minified(tmp_path / "c.txt") is None and minify.minified(tmp_path / "missing.js") is None


def test_the_viewer_server_sends_minified_files_and_the_sources_stay_as_they_are():
    from ad2render.server import App, make_server
    app = App(None, None, log=lambda *_: None)
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        for url, path in [("/static/viewer.js", VIEWER / "viewer.js"), ("/static/style.css", VIEWER / "style.css"),
                          ("/static/vendor/three/addons/controls/OrbitControls.js", VIEWER / "vendor/three/addons/controls/OrbitControls.js")]:
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=30)
            conn.request("GET", url)
            r = conn.getresponse()
            body = r.read()
            assert r.status == 200 and len(body) < path.stat().st_size * 0.9, url
            assert "javascript" in r.getheader("Content-Type") or url.endswith(".css")
            conn.close()
        assert b"// " not in (VIEWER / "viewer.js").read_bytes()[:0] and b"\n  " in (VIEWER / "viewer.js").read_bytes()   # the source is untouched
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=30)
        conn.request("GET", "/")
        assert b"/static/" in conn.getresponse().read()                                  # HTML is not touched
    finally:
        srv.shutdown()
        app.close()
