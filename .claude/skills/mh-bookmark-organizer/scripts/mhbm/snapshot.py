"""Single-file, script-free page snapshots.

The result is one self-contained HTML file: stylesheets and (small) images are
inlined, scripts/iframes/objects/event handlers are removed and a strict
Content-Security-Policy blocks anything remote, so opening an archived page can
neither run code nor phone home.
"""
from __future__ import annotations

import base64
import html
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

from .netsafe import fetch

CSP = ("default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; "
       "base-uri 'none'; form-action 'none'")
_VOID = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}
_SKIP = {"script", "iframe", "object", "embed", "applet", "noscript", "template", "frame", "frameset"}
_URL_CSS = re.compile(r"url\(\s*(?!['\"]?data:)[^)]*\)", re.I)
_IMPORT_CSS = re.compile(r"@import[^;]*;", re.I)


class Limits:
    def __init__(self, max_images=40, max_image_bytes=400_000, max_total=4_000_000, max_css=300_000):
        self.max_images, self.max_image_bytes = max_images, max_image_bytes
        self.max_total, self.max_css = max_total, max_css
        self.images, self.total = 0, 0


def _safe_css(css: str) -> str:
    return _IMPORT_CSS.sub("", _URL_CSS.sub("none", css)).replace("</", "<\\/")


class _Rewriter(HTMLParser):
    def __init__(self, base_url, limits, assets, timeout, throttle):
        super().__init__(convert_charrefs=True)
        self.out = ["<!DOCTYPE html>"]
        self.base, self.limits, self.assets = base_url, limits, assets
        self.timeout, self.throttle = timeout, throttle
        self.skip = 0
        self.in_style = False
        self.csp_done = False

    def _csp(self):
        if not self.csp_done:
            self.out.append(f'<meta http-equiv="Content-Security-Policy" content="{html.escape(CSP)}">')
            self.csp_done = True

    def _fetch(self, url, max_bytes):
        r = fetch(urljoin(self.base, url), timeout=self.timeout, max_bytes=max_bytes, throttle=self.throttle)
        return r if r.status == 200 else None

    def _image(self, src):
        lim = self.limits
        if not self.assets or src.startswith("data:") or lim.images >= lim.max_images or lim.total >= lim.max_total:
            return src if src.startswith("data:image/") else ""
        r = self._fetch(src, lim.max_image_bytes + 1)
        ctype = r.content_type.split(";")[0].strip().lower() if r else ""
        if not r or not ctype.startswith("image/") or ctype == "image/svg+xml" or len(r.body) > lim.max_image_bytes:
            return ""
        lim.images += 1
        lim.total += len(r.body)
        return f"data:{ctype};base64,{base64.b64encode(r.body).decode()}"

    def handle_starttag(self, tag, attrs):
        if self.skip or tag in _SKIP:
            if tag not in _VOID:
                self.skip += 1
            return
        a = [(k.lower(), v or "") for k, v in attrs if not k.lower().startswith("on")]
        if self._rewritten(tag, dict(a)):
            return
        if tag == "style":
            self.in_style = True
        attrs_s = "".join(f' {k}="{html.escape(v, quote=True)}"' for k, v in self._safe_attributes(tag, a))
        self.out.append(f"<{tag}{attrs_s}>")

    def _rewritten(self, tag, d) -> bool:
        """Tags that are replaced or dropped instead of copied; True when this one has been dealt with."""
        if tag == "head":
            self.out.append("<head>")
            self._csp()
            return True
        if tag == "base":
            return True
        if tag == "meta" and d.get("http-equiv", "").lower() in ("refresh", "content-security-policy"):
            return True
        if tag == "link":
            self._inline_stylesheet(d)
            return True
        return False

    def _inline_stylesheet(self, d) -> None:
        if "stylesheet" in d.get("rel", "").lower() and d.get("href") and self.assets:
            r = self._fetch(d["href"], self.limits.max_css)
            if r and "css" in r.content_type.lower():
                self.out.append(f"<style>{_safe_css(r.body.decode('utf-8', 'replace'))}</style>")

    def _safe_attributes(self, tag, a) -> list:
        """The attributes that survive: no event handlers (already gone), no remote-loading ones, links made absolute and
        ``javascript:`` neutralised, images inlined or dropped."""
        new = []
        for k, v in a:
            if k in ("srcset", "ping", "formaction", "poster", "data-src", "data-srcset") or (k == "style" and "url(" in v.lower()):
                continue
            if k == "href":
                v = urljoin(self.base, v) if not v.lower().startswith("javascript:") else "#"
            elif k == "src":
                if tag != "img":
                    continue
                v = self._image(v)
                if not v:
                    continue
            new.append((k, v))
        return new

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self.skip:
            if tag in _SKIP:
                self.skip -= 1
            return
        if tag == "style":
            self.in_style = False
        if tag in _VOID:
            return
        if tag == "head":
            self._csp()
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self.skip:
            return
        self.out.append(_safe_css(data) if self.in_style else html.escape(data))

    def result(self) -> str:
        if not self.csp_done:  # documents without <head>
            self.out.insert(1, f'<meta http-equiv="Content-Security-Policy" content="{html.escape(CSP)}">')
        return "".join(self.out)


def build_snapshot(page: str, base_url: str, *, assets: bool = True, timeout: float = 10, throttle=None,
                   limits: Limits | None = None) -> str:
    r = _Rewriter(base_url, limits or Limits(), assets, timeout, throttle)
    try:
        r.feed(page)
        r.close()
    except Exception:
        pass
    return r.result()
