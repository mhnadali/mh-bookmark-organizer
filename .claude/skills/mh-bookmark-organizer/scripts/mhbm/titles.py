"""Title cleanup proposals: notification counters, repeated site-name suffixes, empty or URL-only titles."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from urllib.parse import unquote, urlsplit

from .report import is_error_title
from .urlnorm import host_of

_SEPS = re.compile(r"\s+(?:\||-|–|—|·|•|::|»|│)\s+")
_COUNTER = re.compile(r"^\s*[\(\[]\d{1,4}\+?[\)\]]\s*")
_INVISIBLE = re.compile("[​‎‏‪-‮⁦-⁩﻿]")
_LABEL = re.compile(r"[^\w]", re.UNICODE)


def _site_label(host: str) -> str:
    labels = host.split(".")
    return _LABEL.sub("", (labels[-2] if len(labels) >= 2 else host).lower())


def _is_brand(segment: str, host: str, common: set) -> bool:
    seg = _LABEL.sub("", segment.lower())
    if not seg:
        return False
    label = _site_label(host)
    return seg in common or (len(label) >= 3 and (seg == label or seg == _LABEL.sub("", host.lower())))


def brand_segments(bms) -> dict:
    """Per host: the leading/trailing title segments that repeat across that site's bookmarks (its brand)."""
    per_host: dict = defaultdict(list)
    for b in bms:
        per_host[host_of(b.url)].append(b.title)
    out: dict = {}
    for host, titles in per_host.items():
        if len(titles) < 3:
            out[host] = set()
            continue
        counts: Counter = Counter()
        for t in titles:
            parts = _SEPS.split(_COUNTER.sub("", t))
            if len(parts) >= 2:
                counts[_LABEL.sub("", parts[-1].lower())] += 1
                counts[_LABEL.sub("", parts[0].lower())] += 1
        need = max(3, int(0.4 * len(titles)))
        out[host] = {seg for seg, c in counts.items() if seg and c >= need}
    return out


def clean_title(title: str, host: str, common: set, *, keep_counters=False, strip_brand=True) -> str:
    t = _INVISIBLE.sub("", title or "")
    t = " ".join(t.split())
    if not keep_counters:
        t = _COUNTER.sub("", t)
    if strip_brand:
        for _ in range(2):  # at most one prefix and one suffix
            parts = _SEPS.split(t)
            if len(parts) < 2:
                break
            if _is_brand(parts[-1], host, common) and len(" | ".join(parts[:-1])) >= 3:
                t = t[: t.rfind(parts[-1])].rstrip()
                t = re.sub(r"\s*(?:\||-|–|—|·|•|::|»|│)\s*$", "", t)
            elif _is_brand(parts[0], host, common) and len(" | ".join(parts[1:])) >= 3:
                t = t[len(parts[0]):].lstrip()
                t = re.sub(r"^(?:\||-|–|—|·|•|::|»|│)\s*", "", t)
            else:
                break
    return t.strip()


def readable_from_url(url: str) -> str:
    p = urlsplit(url)
    host = host_of(url) or p.netloc
    seg = unquote(p.path.rstrip("/").rsplit("/", 1)[-1]) if p.path.strip("/") else ""
    seg = re.sub(r"\.(html?|php|aspx?)$", "", seg)
    seg = re.sub(r"[-_+]+", " ", seg).strip()
    return f"{host} › {seg}" if seg and not seg.isdigit() else host


def propose(bms, *, keep_counters=False, strip_brand=True) -> dict:
    brands = brand_segments(bms) if strip_brand else {}
    plan = {}
    for b in bms:
        host = host_of(b.url)
        title = b.title or ""
        new, why = title, ""
        page_title = (b.meta or {}).get("page_title", "")
        if not title.strip() or title.strip() == b.url:
            new, why = (page_title if page_title and not is_error_title(page_title) else readable_from_url(b.url)), "no usable title"
        elif is_error_title(title):
            if page_title and not is_error_title(page_title):
                new, why = page_title, "saved while the site showed an error; real title from the archived page"
            else:
                continue  # nothing better is known; `archive` first
        else:
            new = clean_title(title, host, brands.get(host, set()), keep_counters=keep_counters, strip_brand=strip_brand)
            if new != " ".join(_INVISIBLE.sub("", title).split()) or new != title:
                why = "cleaned"
                if _COUNTER.match(title) and not keep_counters:
                    why = "notification counter removed"
                if strip_brand and len(new) < len(_COUNTER.sub("", title).strip()) - 2:
                    why = (why + "; " if why != "cleaned" else "") + "site name suffix/prefix removed"
        if new and new != title:
            plan[b.id] = {"title": new, "reason": why}
    return plan
