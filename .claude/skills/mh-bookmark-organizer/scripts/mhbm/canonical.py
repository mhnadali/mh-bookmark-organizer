"""Refresh bookmark URLs from the canonical URL pages declare - only when the change is cosmetic."""
from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit

from .urlnorm import is_tracking, variant_key


def _plain_host(url: str) -> str:
    h = (urlsplit(url).hostname or "").lower()
    for prefix in ("www.", "m.", "mobile.", "amp."):
        if h.startswith(prefix):
            h = h[len(prefix):]
    return h


def _query_keys(url: str) -> set:
    return {k.lower() for k, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True) if not is_tracking(k)}


def judge(url: str, canonical: str):
    """``(verdict, reason)``: ``update`` | ``same`` | ``review``."""
    try:
        u, c = urlsplit(url), urlsplit(canonical)
    except ValueError:
        return "review", "unparsable"
    if c.scheme not in ("http", "https") or not c.hostname:
        return "review", "canonical is not a web URL"
    if variant_key(url) == variant_key(canonical) and (u.scheme, u.netloc.lower(), u.path, u.query) == (c.scheme, c.netloc.lower(), c.path, c.query):
        return "same", ""
    if _plain_host(url) != _plain_host(canonical):
        return "review", "canonical is on a different site"
    if u.path not in ("", "/") and c.path in ("", "/"):
        return "review", "canonical is the home page (typical of apps and search pages)"
    if _query_keys(url) - _query_keys(canonical):
        return "review", "the page's own query parameters would be dropped"
    if variant_key(url) == variant_key(canonical):
        return "update", "same page: scheme, mobile/AMP host, tracking parameters or trailing slash differ"
    up, cp = u.path.rstrip("/"), c.path.rstrip("/")
    if up and cp.startswith(up) and cp[len(up):len(up) + 1] in ("-", "/", ""):
        return "update", "canonical adds the readable slug to the same address"
    return "review", "canonical points to a different path"


def propose(bms):
    plan, review = {}, []
    for b in bms:
        canon = (b.meta or {}).get("canonical", "")
        if not canon or canon.strip() == b.url:
            continue
        verdict, reason = judge(b.url, canon.strip())
        if verdict == "update":
            plan[b.id] = {"url": canon.strip(), "reason": reason}
        elif verdict == "review":
            review.append({"id": b.id, "url": b.url, "canonical": canon.strip(), "reason": reason})
    return plan, review
