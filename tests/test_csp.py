"""The Content-Security-Policy: inline blocks are allowed by hash, and the pages use nothing the policy blocks."""
import base64
import hashlib
import re

from ad2render.server import OTHER_CSP, VIEWER_DIR, page_csp


def sha(text: str) -> str:
    return f"'sha256-{base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()}'"


def directive(csp: str, name: str) -> str:
    return next(d.strip() for d in csp.split(";") if d.strip().startswith(name + " "))


def test_inline_blocks_are_allowed_by_hash_and_nothing_else_inline():
    page = b"<script type=importmap>{}</script><script type=module src=/x.js></script><style>a{}</style><script>\r\nx()\r\n</script>"
    csp = page_csp(page)
    assert directive(csp, "script-src") == f"script-src 'self' {sha('{}')} {sha(chr(10) + 'x()' + chr(10))}"   # CRLF hashed as LF
    assert directive(csp, "style-src") == f"style-src 'self' {sha('a{}')}"
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "frame-ancestors 'self'" in csp and "object-src 'none'" in csp and "base-uri 'none'" in csp
    assert directive(page_csp(b"<p>plain</p>"), "script-src") == "script-src 'self'"


def test_everything_but_pages_allows_nothing():
    assert OTHER_CSP == "default-src 'none'; frame-ancestors 'self'"


class FakeHandler:
    """Just enough of server.Handler to see the headers _send writes."""
    from ad2render.server import Handler
    _send = Handler._send

    def __init__(self):
        import io
        self.headers_out, self.wfile = {}, io.BytesIO()

    def send_response(self, status): pass
    def end_headers(self): pass
    def send_header(self, k, v): self.headers_out[k] = v


def test_html_built_at_runtime_never_gets_its_inline_scripts_allowed():
    injected = "<script>alert(1)</script><style>*{}</style>"
    h = FakeHandler()
    h._send(200, injected.encode(), "text/html; charset=utf-8")    # any other HTML: no inline blocks at all
    csp = h.headers_out["Content-Security-Policy"]
    assert directive(csp, "script-src") == "script-src 'self'" and directive(csp, "style-src") == "style-src 'self'"


def test_the_pages_use_nothing_the_policy_blocks():
    for f in VIEWER_DIR.glob("*.html"):
        html = f.read_text(encoding="utf-8")
        assert not re.search(r"\sstyle\s*=", html), f"{f.name}: style attribute"              # style-src has no 'unsafe-inline'
        assert not re.search(r"\son[a-z]+\s*=", html), f"{f.name}: inline event handler"
        assert "javascript:" not in html, f.name
    for f in VIEWER_DIR.glob("*.js"):
        js = f.read_text(encoding="utf-8")
        assert not re.search(r"\bstyle:\s*['`\"]|setAttribute\(\s*['\"]style", js), f"{f.name}: style attribute"
        assert not re.search(r"\beval\(|new Function\(", js), f.name
