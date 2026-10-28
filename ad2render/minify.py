"""Small, conservative JavaScript and CSS minifiers (standard library only).

They only remove what cannot change behaviour: comments, and whitespace that no token needs. Names are never
renamed and code is never rewritten, so a page behaves exactly as its source. The source files stay as they are;
the server minifies them when it sends them (see server.py) and remembers the result until the file changes.

JavaScript: a newline is kept wherever automatic semicolon insertion could depend on it. Regular expression
literals are told from divisions by the previous token (as every non-parsing minifier does); `AD2_MINIFY=0` turns
minifying off if that ever guesses wrong. License comments (`/*! ... */`, `@license`, `@preserve`) are kept.
"""
from __future__ import annotations

import os
import re

ENABLED = os.environ.get("AD2_MINIFY", "1").strip().lower() not in ("0", "false", "no", "off")

# Words after which a "/" starts a regular expression instead of dividing.
_REGEX_AFTER = {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case", "do", "else",
                "yield", "await"}
_WORD = re.compile(r"[A-Za-z0-9_$\\]|[^\x00-\x7f]")
_IDENT = re.compile(r"[A-Za-z_$\\]|[^\x00-\x7f]")
_NUM_END = re.compile(r"[0-9A-Za-z_.$]")


class MinifyError(ValueError):
    pass


# ------------------------------------------------------------------------------------------ JavaScript
def _skip_string(s: str, i: int) -> int:
    q, n = s[i], len(s)
    i += 1
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
        elif c == q:
            return i + 1
        elif c == "\n":
            raise MinifyError("unterminated string")
        else:
            i += 1
    raise MinifyError("unterminated string")


def _skip_template(s: str, i: int) -> int:
    """i is at the opening backtick; returns the index after the closing one. Embedded ${...} may nest anything."""
    n = len(s)
    i += 1
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
        elif c == "`":
            return i + 1
        elif c == "$" and s.startswith("${", i):
            i = _skip_braces(s, i + 2)
        else:
            i += 1
    raise MinifyError("unterminated template")


def _skip_braces(s: str, i: int) -> int:
    """i is just after a `${`; returns the index after the matching `}`."""
    depth, n = 1, len(s)
    while i < n:
        c = s[i]
        if c in "'\"":
            i = _skip_string(s, i)
            continue
        if c == "`":
            i = _skip_template(s, i)
            continue
        if c == "/" and s.startswith("//", i):
            i = s.find("\n", i)
            i = n if i < 0 else i
            continue
        if c == "/" and s.startswith("/*", i):
            j = s.find("*/", i + 2)
            if j < 0:
                raise MinifyError("unterminated comment")
            i = j + 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise MinifyError("unterminated template expression")


def _skip_regex(s: str, i: int) -> int:
    n, in_class = len(s), False
    i += 1
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "\n":
            raise MinifyError("unterminated regular expression")
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            i += 1
            while i < n and _WORD.match(s[i]):  # flags
                i += 1
            return i
        i += 1
    raise MinifyError("unterminated regular expression")


def _tokens(s: str):
    """Yield (kind, text, ws_before, newline_before). Kinds: word, str, tpl, re, punct."""
    i, n = 0, len(s)
    ws = nl = False
    prev_kind, prev_text = None, ""
    while i < n:
        c = s[i]
        if c in " \t\r\n\f\v ﻿  ":
            ws = True
            nl = nl or c in "\r\n  "
            i += 1
            continue
        if c == "/" and s.startswith("//", i):
            j = s.find("\n", i)
            j = n if j < 0 else j
            if "@license" in s[i:j] or "@preserve" in s[i:j]:
                yield "keep", s[i:j], True, True
                ws = nl = True
                prev_kind, prev_text = "keep", ""
            else:
                ws = True
            i = j
            continue
        if c == "/" and s.startswith("/*", i):
            j = s.find("*/", i + 2)
            if j < 0:
                raise MinifyError("unterminated comment")
            body = s[i:j + 2]
            if body.startswith("/*!") or "@license" in body or "@preserve" in body:
                yield "keep", body, True, True
                nl = True
            else:
                nl = nl or "\n" in body
            ws = True
            i = j + 2
            continue
        start = i
        if c in "'\"":
            i, kind = _skip_string(s, i), "str"
        elif c == "`":
            i, kind = _skip_template(s, i), "tpl"
        elif c.isdigit() or (c == "." and i + 1 < n and s[i + 1].isdigit()):
            i += 1
            while i < n:
                d = s[i]
                if d in "eE" and i + 1 < n and s[i + 1] in "+-" and not s[start:i].lower().startswith(("0x", "0b", "0o")):
                    i += 2
                elif _NUM_END.match(d):
                    i += 1
                else:
                    break
            kind = "word"
        elif _IDENT.match(c):
            i += 1
            while i < n and _WORD.match(s[i]):
                i += 1
            kind = "word"
        elif c == "/" and _regex_allowed(prev_kind, prev_text):
            i, kind = _skip_regex(s, i), "re"
        else:
            i, kind = i + 1, "punct"
        text = s[start:i]
        yield kind, text, ws, nl
        ws = nl = False
        prev_kind, prev_text = kind, text


def _regex_allowed(kind, text) -> bool:
    if kind is None or kind == "keep":
        return True
    if kind == "word":
        return text in _REGEX_AFTER
    if kind in ("str", "tpl", "re"):
        return False
    return text not in (")", "]")      # after "}" a statement may start, which is far likelier than a division


# After these a newline cannot end a statement (the expression is still open), so it can go.
_OPEN = set("{([,;:=*%&|^<>?!~./")
# Before these the previous statement cannot have ended (they continue it, or close a block), so it can go.
_CONTINUE = set(".,;:?)]}=*%&|^<>([/")
# `return` followed by a newline returns nothing (same for throw, break, ...): there the newline is part of the meaning.
_RESTRICTED = {"return", "throw", "break", "continue", "yield", "async"}


def _needs_newline(a_kind, a, b_kind, b) -> bool:
    """A newline sat between tokens a and b: may it be dropped without changing how the code is parsed?"""
    if a_kind == "punct" and a in _OPEN:
        return False
    if a_kind == "word" and a in _RESTRICTED:
        return True
    return not (b_kind == "punct" and b in _CONTINUE)       # b starts a new statement (or `++x`): keep the newline


def minify_js(src: str) -> str:
    out: list[str] = []
    prev = None  # (kind, text)
    for kind, text, ws, nl in _tokens(src):
        if kind == "keep":
            out.append(("\n" if out and not out[-1].endswith("\n") else "") + text + "\n")
            prev = None
            continue
        if prev is not None:
            pk, pt = prev
            sep = ""
            if nl and _needs_newline(pk, pt, kind, text):
                sep = "\n"
            elif ws:
                if _WORD.match(pt[-1]) and _WORD.match(text[0]):
                    sep = " "
                elif pk == "word" and pt[0].isdigit() and text == "." and not re.search(r"[.eE]", pt):
                    sep = " "                                   # 1 .toString()
                elif pt in ("+", "-") and pk == "punct" and text[0] == pt:
                    sep = " "                                   # a + +b, a - -b
                elif pt == "/" and pk == "punct" and text[0] in "/*":
                    sep = " "                                   # never form a comment
                elif pk == "punct" and pt == "<" and text.startswith("!"):
                    sep = " "                                   # never form <!--
            out.append(sep)
        out.append(text)
        prev = (kind, text)
    return "".join(out).strip() + "\n"


# ----------------------------------------------------------------------------------------------- CSS
def minify_css(src: str) -> str:
    out: list[str] = []
    i, n = 0, len(src)
    depth_paren = 0
    pending_space = False

    def last() -> str:
        return out[-1][-1] if out and out[-1] else ""

    def emit(text: str):
        nonlocal pending_space
        if pending_space and out:
            prev = last()
            # no space needed next to punctuation that ends or starts a token on its own
            drop_before = text[0] in "{};,>~)!" or (text[0] == "+" and depth_paren == 0) or (text[0] == ":" and False)
            drop_after = prev in "{};,>~(" or (prev == "+" and depth_paren == 0) or prev == ":"
            if not drop_before and not drop_after:
                out.append(" ")
        pending_space = False
        out.append(text)

    while i < n:
        c = src[i]
        if c.isspace():
            pending_space = True
            i += 1
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                raise MinifyError("unterminated comment")
            if src[i + 2:i + 3] == "!":
                emit(src[i:j + 2])
            else:
                pending_space = True
            i = j + 2
        elif c in "'\"":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            if j >= n:
                raise MinifyError("unterminated string")
            emit(src[i:j + 1])
            i = j + 1
        elif src.startswith("url(", i) and not re.match(r"url\(\s*['\"]", src[i:i + 16]):
            j = src.find(")", i)
            if j < 0:
                raise MinifyError("unterminated url()")
            emit("url(" + src[i + 4:j].strip() + ")")
            i = j + 1
        else:
            if c == "(":
                depth_paren += 1
            elif c == ")":
                depth_paren = max(0, depth_paren - 1)
            if c == ";":
                # a ";" right before "}" is redundant
                j = i + 1
                while j < n and src[j].isspace():
                    j += 1
                if j < n and src[j] == "}":
                    pending_space = False
                    i += 1
                    continue
            # collect a run of ordinary characters so words stay whole
            j = i + 1
            if c not in "{};,>~()+:!":
                while j < n and not src[j].isspace() and src[j] not in "{};,>~()+:!'\"/":
                    j += 1
            emit(src[i:j])
            i = j
    return "".join(out).strip() + "\n"


# --------------------------------------------------------------------------------------- the server's cache
_cache: dict[str, tuple[tuple[int, int], bytes | None]] = {}


def minified(path, data: bytes | None = None) -> bytes | None:
    """The minified bytes of a .js or .css file, or None when it should be sent as is (other type, minifying off, or
    the file could not be understood). Results are kept until the file changes."""
    if not ENABLED:
        return None
    p = os.fspath(path)
    ext = os.path.splitext(p)[1].lower()
    if ext not in (".js", ".css"):
        return None
    try:
        st = os.stat(p)
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    hit = _cache.get(p)
    if hit and hit[0] == key:
        return hit[1]
    try:
        text = (data if data is not None else open(p, "rb").read()).decode("utf-8")
        out = (minify_js if ext == ".js" else minify_css)(text).encode("utf-8")
    except (MinifyError, UnicodeDecodeError, RecursionError):
        out = None
    if out is not None and len(out) >= len(text.encode("utf-8")):
        out = None
    _cache[p] = (key, out)          # a file that cannot be minified is not retried until it changes
    return out
