"""PROTOTYPE of ``urlnorm.browser_key`` (to be merged into mhbm/urlnorm.py).

Goal: ``browser_key(raw) == browser_key(chromium_canonical(raw))`` for every http(s) URL, so that a bookmark does not look
"deleted and re-added" to ``sync`` after Chromium rewrites its Bookmarks file with GURL-canonicalised URLs.
It is a *key* (like ``normalize_url``), not a canonicaliser: it only has to map the raw form and the browser's form to the same string.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

DEFAULT_PORTS = {"http": 80, "https": 443}
_ESC = re.compile(r"%([0-9A-Fa-f]{2})")
_SAFE = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;=:@")
_KEEP_PATH = frozenset(b"/?#%")        # escapes that would change the meaning of the URL if decoded
_KEEP_FRAG = frozenset(b"%")


def _pct_normal_form(text: str, keep: frozenset, extra_safe: bytes = b"") -> str:
    """Decode every ``%XX`` that is not structural, then percent-encode (UTF-8, upper-case hex) whatever is not URL-safe.

    A raw ``ü``, ``%C3%BC`` and ``%c3%bc`` therefore give the same result, as do ``a b`` and ``a%20b``."""
    data = text.encode("utf-8", "surrogatepass")
    out, i, n = [], 0, len(data)
    while i < n:
        b = data[i]
        if b == 0x25 and i + 2 < n + 0 and re.fullmatch(rb"[0-9A-Fa-f]{2}", data[i + 1:i + 3] or b"x"):
            val = int(data[i + 1:i + 3], 16)
            if val in keep or val >= 0x80:
                out.append(f"%{val:02X}")
            elif val in _SAFE or val in extra_safe:
                out.append(chr(val))
            else:
                out.append(f"%{val:02X}")
            i += 3
            continue
        if b in _SAFE or b in extra_safe:
            out.append(chr(b))
        else:
            out.append(f"%{b:02X}")
        i += 1
    return "".join(out)


def _dot_segments(path: str) -> str:
    out: list = []
    segs = path.split("/")
    for i, seg in enumerate(segs):
        if seg == "..":
            if len(out) > 1:
                out.pop()
            if i == len(segs) - 1:
                out.append("")
        elif seg == ".":
            if i == len(segs) - 1:
                out.append("")
        else:
            out.append(seg)
    return "/".join(out) or "/"


def _host(host: str) -> str:
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if host.isascii():
        return host
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def browser_key(url: str, is_tracking=lambda k: False, route_fragment=lambda f: f.startswith(("/", "!"))) -> str:
    url = (url or "").strip()
    url = re.sub(r"[\t\r\n]", "", url)                       # GURL drops tab, CR and LF anywhere in the string
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return url
    try:
        port = parts.port
    except ValueError:
        port = None
    host = _host(parts.hostname)
    netloc = host if port in (None, DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    path = _dot_segments(_pct_normal_form(parts.path.replace("\\", "/") or "/", _KEEP_PATH, b"/"))
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    query = sorted((k, v) for k, v in parse_qsl(parts.query.replace("\\", "%5C"), keep_blank_values=True) if not is_tracking(k))
    frag = parts.fragment
    fragment = _pct_normal_form(frag, _KEEP_FRAG, b"/?#") if route_fragment(frag) else ""
    return urlunsplit(("https", netloc, path, urlencode(query), fragment))
