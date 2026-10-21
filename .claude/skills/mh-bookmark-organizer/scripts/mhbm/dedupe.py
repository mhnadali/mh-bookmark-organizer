"""Duplicate detection at three confidence levels.

* ``exact``   - same normalised URL (safe to merge automatically)
* ``variant`` - also mobile/AMP variants and pages declaring the same canonical URL (safe)
* fuzzy       - similar titles on one site or near-identical page text; ALWAYS for human/agent review
"""
from __future__ import annotations

import difflib
import hashlib
import re
from collections import defaultdict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .model import Bookmark, clean_tags
from .urlnorm import host_of, normalize_url, variant_key

LEVELS = ("exact", "variant")


def _key(b: Bookmark, level: str) -> str:
    if level == "variant":
        return variant_key(b.url, (b.meta or {}).get("canonical", ""))
    return normalize_url(b.url)


def _score(b: Bookmark):
    """Higher is better: alive, a real title, notes, tags, filed, then oldest."""
    return (b.status != "dead", bool(b.title and b.title != b.url), len(b.notes), len(b.tags),
            bool(b.folder), -(b.added or 2**62))


def find_groups(bms, level: str = "exact", same_folder_only: bool = False) -> list:
    groups = defaultdict(list)
    for b in bms:
        b.norm = _key(b, level)
        groups[(b.norm, b.folder) if same_folder_only else b.norm].append(b)
    return [g for g in groups.values() if len(g) > 1]


def merge_group(group):
    """Return ``(keeper, removed)``; the keeper absorbs tags, notes and the earliest date."""
    ordered = sorted(group, key=_score, reverse=True)
    keeper, rest = ordered[0], ordered[1:]
    keeper.tags = clean_tags([t for b in ordered for t in b.tags])
    for b in rest:
        if b.notes and b.notes not in keeper.notes:
            keeper.notes = f"{keeper.notes} | {b.notes}" if keeper.notes else b.notes
        if not keeper.wayback and b.wayback:
            keeper.wayback = b.wayback
    elsewhere = sorted({b.folder for b in rest if b.folder and b.folder != keeper.folder})
    if elsewhere:  # the merged copies were deliberately filed elsewhere: leave a trace instead of silently dropping that
        trace = "also filed in: " + "; ".join(elsewhere[:3]) + (f" (+{len(elsewhere) - 3} more)" if len(elsewhere) > 3 else "")
        if trace not in keeper.notes:
            keeper.notes = f"{keeper.notes} | {trace}" if keeper.notes else trace
    dates = [b.added for b in ordered if b.added]
    keeper.added = min(dates) if dates else 0
    return keeper, rest


def dedupe(bms, level: str = "exact", same_folder_only: bool = False):
    """Return ``(kept, removed, groups)``; ``bms`` itself is not reordered."""
    groups = find_groups(bms, level, same_folder_only)
    removed = []
    for g in groups:
        removed += merge_group(g)[1]
    gone = {id(b) for b in removed}
    return [b for b in bms if id(b) not in gone], removed, groups


# ------------------------------------------------------------------ fuzzy
_SUFFIX = re.compile(r"\s+[|\u2013\u2014-]\s+[^|\u2013\u2014-]{2,40}$")
_ID = re.compile(r"\d{4,}|[0-9a-f]{8,}")


def _title_key(title: str) -> str:
    t = _SUFFIX.sub("", title or "").lower()
    return " ".join(re.findall(r"\w+", t))


_IGNORABLE = {"ref", "referrer", "referer", "via", "source", "src", "from", "share", "affiliate", "aff", "partner", "campaign"}


def _page_key(url: str) -> str:
    """Normalised URL minus referral-style parameters. A page that differs only by those is probably the same page;
    anything that carries state (``?q=``, ``?id=``, ``#/route``) keeps its own identity."""
    parts = urlsplit(normalize_url(url))
    kept = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _IGNORABLE and not k.lower().startswith("ref_"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def _ids(url: str) -> set:
    return set(_ID.findall(urlsplit(url).path.lower() + "?" + urlsplit(url).query.lower() + "#" + urlsplit(url).fragment.lower()))


def _slug(url: str) -> str:
    return urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1].lower()


def simhash(text: str, bits: int = 64) -> int:
    words = re.findall(r"\w{3,}", text.lower())
    if len(words) < 40:
        return 0
    v = [0] * bits
    for i in range(len(words) - 2):
        h = int.from_bytes(hashlib.md5(" ".join(words[i:i + 3]).encode()).digest()[:8], "big")
        for k in range(bits):
            v[k] += 1 if (h >> k) & 1 else -1
    return sum(1 << k for k in range(bits) if v[k] > 0)


def _same_page_variants(bms) -> list:
    """Bookmarks of one page that differ only in a query or fragment (referral parameters and the like)."""
    by_page = defaultdict(list)
    for b in bms:
        by_page[_page_key(b.url)].append(b)
    return [("same page, different query/fragment", g) for g in by_page.values() if len({normalize_url(b.url) for b in g}) > 1]


def _same_title_pairs(bms) -> list:
    """Same title on the same site and a similar address. Bucketed by (host, title) so the work stays linear on large libraries."""
    by_title = defaultdict(list)
    for b in bms:
        t = _title_key(b.title)
        if len(t) >= 20:
            by_title[(host_of(b.url), t)].append(b)
    found = []
    for items in by_title.values():
        items = items[:50]  # a site title shared by hundreds of pages carries no information
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                if _looks_like_the_same_page(a, b):
                    found.append(("same title and similar address", [a, b]))
    return found


def _looks_like_the_same_page(a, b) -> bool:
    if _page_key(a.url) == _page_key(b.url):
        return False  # already reported as the same page
    ia, ib = _ids(a.url), _ids(b.url)
    if ia and ib and ia != ib:
        return False  # different numeric/hex ids = different entities (products, workspaces, issues...)
    return difflib.SequenceMatcher(None, _slug(a.url), _slug(b.url)).ratio() >= 0.6


def _near_identical_text(bms, max_hamming: int) -> list:
    """Near-identical page text via SimHash; 4 bands of 16 bits find every pair within 3 differing bits without O(n^2)."""
    buckets = defaultdict(list)
    for b in bms:
        if len(b.content) > 400:
            h = simhash(b.content)
            if h:
                for band in range(4):
                    buckets[(band, (h >> (16 * band)) & 0xFFFF)].append((b, h))
    found, seen = [], set()
    for cands in buckets.values():
        cands = cands[:200]
        for i, (a, ha) in enumerate(cands):
            for b, hb in cands[i + 1:]:
                key = (a.id, b.id) if a.id < b.id else (b.id, a.id)
                if key in seen:
                    continue
                seen.add(key)
                if bin(ha ^ hb).count("1") <= max_hamming and normalize_url(a.url) != normalize_url(b.url):
                    found.append(("near-identical page text", [a, b]))
    return found


def fuzzy_groups(bms, *, max_hamming: int = 3) -> list:
    """Look-alikes that need a human decision, as ``(reason, members)``; never merged automatically.

    Deliberately strict: pages that only share a site title (different products, apps, workspaces or videos) are NOT
    reported, because on real libraries those are the overwhelming majority of title matches.
    """
    return _union(_same_page_variants(bms) + _same_title_pairs(bms) + _near_identical_text(bms, max_hamming))


def _union(found) -> list:
    parent: dict = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    members, reasons = {}, defaultdict(set)
    for _reason, g in found:
        for b in g:
            members[b.id] = b
        for b in g[1:]:
            parent[find(b.id)] = find(g[0].id)
    for reason, g in found:
        reasons[find(g[0].id)].add(reason)
    out = defaultdict(list)
    for i, b in members.items():
        out[find(i)].append(b)
    return [("; ".join(sorted(reasons[root])), g) for root, g in out.items() if len(g) > 1]
