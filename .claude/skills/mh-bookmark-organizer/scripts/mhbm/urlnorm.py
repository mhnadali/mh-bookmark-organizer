"""URL normalisation used to detect duplicates, and conservative URL cleaning."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Deliberately conservative: only parameters that are tracking noise on every site. Names that mean something on some
# sites (ocid on Google Ads, ref on GitHub, spm, trk...) are NOT here; add your own via config "extra_tracking_params".
TRACKING_PARAMS = {
    "fbclid", "gclid", "gclsrc", "dclid", "msclkid", "yclid", "mc_cid", "mc_eid", "igshid", "ref_src",
    "_hsenc", "_hsmi", "mkt_tok", "_ga", "_gl", "vero_id", "wickedid", "oly_enc_id",
}
TRACKING_PREFIXES = ("utm_", "pk_", "hsa_", "mtm_", "matomo_")
DEFAULT_PORTS = {"http": 80, "https": 443}
AMP_PARAMS = {"amp", "amp_js_v", "amp_gsa", "usqp", "outputtype", "output"}
_extra_tracking: set = set()


def configure(extra_tracking_params=()) -> None:
    """Add user-defined tracking parameters (from config.json)."""
    _extra_tracking.clear()
    _extra_tracking.update(p.lower() for p in extra_tracking_params)


def is_tracking(key: str) -> bool:
    k = key.lower()
    return k in TRACKING_PARAMS or k in _extra_tracking or k.startswith(TRACKING_PREFIXES)


def _route_fragment(fragment: str) -> bool:
    """``#/inbox`` and ``#!/x`` are single-page-app routes; plain ``#section`` anchors are not."""
    return fragment.startswith(("/", "!"))


def normalize_url(url: str, *, strip_www: bool = True, keep_fragment: bool = False) -> str:
    """Return a canonical key for ``url``; non-web URLs are returned trimmed."""
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return url
    host = parts.hostname.lower().rstrip(".")
    if strip_www and host.startswith("www."):
        host = host[4:]
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host if port in (None, DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not is_tracking(k))
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    fragment = parts.fragment if (keep_fragment or _route_fragment(parts.fragment)) else ""
    # http and https variants of a page are treated as the same bookmark
    return urlunsplit(("https", netloc, path, urlencode(query), fragment))


_HEX2 = re.compile(rb"[0-9A-Fa-f]{2}")
_URL_SAFE = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;=:@")
_KEEP_ENCODED_IN_PATH = frozenset(b"/?#%")   # decoding these escapes would change what the URL means
_KEEP_ENCODED_IN_FRAGMENT = frozenset(b"%")


def _percent_normal_form(text: str, keep: frozenset, extra_safe: bytes = b"") -> str:
    """Decode every ``%XX`` that is not structural, then percent-encode (UTF-8, upper-case hex) whatever is not URL-safe.

    A raw ``ü``, ``%C3%BC`` and ``%c3%bc`` therefore give the same string, as do ``a b`` and ``a%20b``."""
    data = text.encode("utf-8", "surrogatepass")
    out, i = [], 0
    while i < len(data):
        byte = data[i]
        if byte == 0x25 and _HEX2.fullmatch(data, i + 1, i + 3):
            byte, i = int(data[i + 1:i + 3], 16), i + 2
            if byte in keep or byte >= 0x80:
                out.append(f"%{byte:02X}")
                i += 1
                continue
        out.append(chr(byte) if byte in _URL_SAFE or byte in extra_safe else f"%{byte:02X}")
        i += 1
    return "".join(out)


def _dot_segments(path: str) -> str:
    out: list = []
    segments = path.split("/")
    for i, seg in enumerate(segments):
        last = i == len(segments) - 1
        if seg == "..":
            if len(out) > 1:
                out.pop()
            if last:
                out.append("")
        elif seg == ".":
            if last:
                out.append("")
        else:
            out.append(seg)
    return "/".join(out) or "/"


def _ascii_host(host: str) -> str:
    host = host.lower().rstrip(".").removeprefix("www.")
    if host.isascii():
        return host
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def browser_key(url: str) -> str:
    """Like ``normalize_url``, but also equal for a URL and the form a Chromium browser stores it in.

    Chromium rewrites every bookmark URL through its URL parser (``a b`` -> ``a%20b``, ``ü`` -> ``%C3%BC``, IDN hosts to
    punycode, ``..`` segments resolved, tabs and newlines dropped). ``sync`` compares the library with the browser, so it needs
    a key that does not change when the browser does that, or every such bookmark looks deleted and re-added.
    Verified against Chromium 141 and Brave 155 on 5,500 generated URLs (tests/realbrowser)."""
    url = re.sub(r"[\t\r\n]", "", (url or "").strip())
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
    host = _ascii_host(parts.hostname)
    netloc = host if port in (None, DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    path = _dot_segments(_percent_normal_form(parts.path.replace("\\", "/") or "/", _KEEP_ENCODED_IN_PATH, b"/"))
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    query = sorted((k, v) for k, v in parse_qsl(parts.query.replace("\\", "%5C"), keep_blank_values=True) if not is_tracking(k))
    fragment = _percent_normal_form(parts.fragment, _KEEP_ENCODED_IN_FRAGMENT, b"/?#") if _route_fragment(parts.fragment) else ""
    return urlunsplit(("https", netloc, path, urlencode(query), fragment))


def strip_tracking(url: str) -> str:
    """Remove only tracking parameters; everything else (order, case, fragment, scheme) is preserved."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    kept = [pair for pair in parts.query.split("&") if pair and not is_tracking(pair.split("=", 1)[0])]
    if len(kept) == len([p for p in parts.query.split("&") if p]):
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(kept), parts.fragment))


_MOBILE_HOST = re.compile(r"^(m|mobile|amp|touch)\.|\.(m|mobile|amp)\.")
_AMP_PATH = re.compile(r"(/amp/?$|\.amp(\.html)?$|/amp/)")


def variant_key(url: str, canonical: str = "") -> str:
    """Looser key that also merges mobile/AMP variants; prefers the page's declared canonical URL."""
    base = normalize_url(canonical or url)
    try:
        parts = urlsplit(base)
    except ValueError:
        return base
    if parts.scheme != "https" or not parts.hostname:
        return base
    host = _MOBILE_HOST.sub(lambda m: "" if m.start() == 0 else ".", parts.hostname)
    path = _AMP_PATH.sub(lambda m: "/" if m.group(0).endswith("/") and m.start() else "", parts.path) or "/"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in AMP_PARAMS]
    netloc = host if not parts.port else f"{host}:{parts.port}"
    return urlunsplit(("https", netloc, path.rstrip("/") or "/", urlencode(query), parts.fragment))


def host_of(url: str) -> str:
    try:
        h = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return h.lower().removeprefix("www.")
