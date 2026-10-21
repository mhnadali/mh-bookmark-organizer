"""Readable-text and metadata extraction from HTML."""
from __future__ import annotations

import re
from html.parser import HTMLParser

MAX_TEXT = 200_000
_DROP = {"script", "style", "noscript", "svg", "template", "iframe", "canvas"}
_CHROME = {"nav", "header", "footer", "aside", "form", "menu", "dialog"}
_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search"}
_MAIN = {"article", "main"}
_VOID = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict = {}
        self.main: list = []
        self.body: list = []
        self._stack: list = []  # (tag, kind) of open elements, kind in drop|chrome|main|plain
        self._in_title = False
        self._time_attr = ""

    def _count(self, kind):
        return sum(1 for _, k in self._stack if k == kind)

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html" and a.get("lang"):
            self.meta["lang"] = a["lang"][:12]
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or a.get("itemprop") or "").lower()
            val = a.get("content", "").strip()
            if val:
                if key in ("description", "og:description"):
                    self.meta.setdefault("description", val)
                elif key == "og:title":
                    self.meta.setdefault("og_title", val)
                elif key in ("author", "article:author"):
                    self.meta.setdefault("author", val[:120])
                elif key in ("article:published_time", "date", "pubdate", "datepublished", "dc.date", "dc.date.issued"):
                    self.meta.setdefault("published", val[:40])
        elif tag == "link" and "canonical" in a.get("rel", "").lower() and a.get("href"):
            self.meta.setdefault("canonical", a["href"].strip())
        elif tag == "time" and a.get("datetime") and "published" not in self.meta:
            self.meta["published"] = a["datetime"][:40]
        if tag in _VOID:
            return
        kind = ("drop" if tag in _DROP else "chrome" if tag in _CHROME or a.get("role", "").lower() in _ROLES
                else "main" if tag in _MAIN else "plain")
        self._stack.append((tag, kind))

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        for i in range(len(self._stack) - 1, -1, -1):  # tolerate unclosed tags
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._count("drop") or self._count("chrome"):
            return
        self.body.append(data)
        if self._count("main"):
            self.main.append(data)


def extract(html_text: str) -> dict:
    """Return ``{title, text, description, canonical, published, author, lang}``."""
    p = _Page()
    try:
        p.feed(html_text)
        p.close()
    except Exception:  # malformed markup must never abort a batch
        pass
    squash = lambda parts: re.sub(r"\s+", " ", " ".join(parts)).strip()  # noqa: E731
    main, body = squash(p.main), squash(p.body)
    text = main if len(main) >= 200 else body
    meta = dict(p.meta)
    return {
        "title": " ".join(p.title.split()) or meta.get("og_title", ""),
        "text": text[:MAX_TEXT],
        "description": meta.pop("description", ""),
        **{k: v for k, v in meta.items() if k in ("canonical", "published", "author", "lang")},
    }
