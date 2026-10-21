"""Careful dead-link detection.

A single failed request never condemns a link.  Failures are retried, counted
across runs and only become ``dead`` once they persist over several checks that
are spread over time; until then the link is ``suspect``.  Sites that refuse
bots (401/403/429) are ``restricted`` and are never treated as dead.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from .model import Bookmark
from .netsafe import HostThrottle, fetch

_HEAD_UNSUPPORTED = {400, 403, 405, 406, 501}
HARD = "hard"      # 404/410/DNS: the page really seems gone
SOFT = "soft"      # timeouts, 5xx, connection errors: may be temporary


def classify(code: int, error: str, blocked: bool, moved: bool):
    """Return ``(status, failure_kind)``; failure_kind is "" unless the request failed."""
    if blocked:
        return "skipped", ""
    if code == 0:
        low = error.lower()
        if "dns" in low or "name or service" in low or "nodename" in low or "not known" in low:
            return "fail", HARD
        return "fail", SOFT
    if code in (404, 410):
        return "fail", HARD
    if code in (401, 403, 429, 999):
        return "restricted", ""
    if code >= 500:
        return "fail", SOFT
    if 200 <= code < 400:
        return ("redirect" if moved else "ok"), ""
    return "fail", SOFT


def verdict(b: Bookmark, kind: str, now: int, confirmations: int, min_gap: int) -> str:
    """suspect until the failure has been seen often enough, over a long enough period."""
    needed = confirmations if kind == HARD else confirmations + 1
    persisted = b.fail_count >= needed and (now - b.first_failed) >= min_gap
    return "dead" if persisted else "suspect"


def _conn_dropped(error: str) -> bool:
    """Servers that mishandle HEAD often just drop the connection; a timeout or DNS failure is not worth a second try."""
    low = error.lower()
    return any(w in low for w in ("reset", "closed", "eof", "remote end", "broken pipe", "bad status"))


def _probe(b: Bookmark, timeout: float, throttle, retries: int):
    r = None
    for attempt in range(retries + 1):
        r = fetch(b.url, method="HEAD", timeout=timeout, throttle=throttle)
        if r.status in _HEAD_UNSUPPORTED or (r.status == 0 and _conn_dropped(r.error)):
            r = fetch(b.url, method="GET", timeout=timeout, max_bytes=1, throttle=throttle)
        moved = r.final_url.rstrip("/") != b.url.rstrip("/")
        status, kind = classify(r.status, r.error, r.blocked, moved)
        if status != "fail" or (kind == HARD and r.status in (404, 410)) or attempt == retries:
            break
        time.sleep(min(r.retry_after or 1.0, 5.0))
    return r, status, kind


def wayback_lookup(url: str, timeout: float = 10, throttle=None) -> str:
    r = fetch("https://archive.org/wayback/available?url=" + quote(url, safe=""), timeout=timeout,
              max_bytes=50_000, throttle=throttle)
    try:
        snap = json.loads(r.body or b"{}").get("archived_snapshots", {}).get("closest", {})
        return snap.get("url", "") if snap.get("available") else ""
    except (ValueError, AttributeError):
        return ""


def check_all(bms, *, workers: int = 16, timeout: float = 10, per_host: int = 2, interval: float = 0.4,
              retries: int = 1, confirmations: int = 2, min_gap: int = 6 * 3600, wayback: bool = False,
              progress=None, now=None):
    """Yield ``(bookmark, status, http_code, permanent_new_url, error)`` and update each bookmark in place."""
    web = [b for b in bms if b.url.lower().startswith(("http://", "https://"))]
    throttle = HostThrottle(per_host, interval)
    wb_throttle = HostThrottle(1, 1.0)

    def work(b):
        r, status, kind = _probe(b, timeout, throttle, retries)
        return b, r, status, kind

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for i, (b, r, status, kind) in enumerate(pool.map(work, web), 1):
            stamp = int(now if now is not None else time.time())
            new_url = ""
            if status == "fail":
                if b.fail_count == 0:
                    b.first_failed = stamp
                b.fail_count += 1
                status = verdict(b, kind, stamp, confirmations, min_gap)
                if wayback:
                    b.wayback = wayback_lookup(b.url, timeout, wb_throttle) or b.wayback
            elif status in ("ok", "redirect"):
                b.fail_count, b.first_failed = 0, 0
                if status == "redirect" and r.permanent_redirect:
                    new_url = r.final_url
            b.status, b.http_code, b.last_checked = status, r.status, stamp
            if progress:
                progress(i, len(web))
            yield b, status, r.status, new_url, r.error


def looks_offline(results, min_sample: int = 10) -> bool:
    """True when (almost) nothing in a sizeable batch got any HTTP answer: the network is down or blocked,
    so the failures say nothing about the links and must not be recorded against them."""
    results = list(results)
    if len(results) < min_sample:
        return False
    answered = sum(1 for r in results if r[2] != 0)
    return answered <= len(results) * 0.03
