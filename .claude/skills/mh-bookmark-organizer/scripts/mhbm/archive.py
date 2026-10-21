"""Fetch pages politely, store sanitised snapshots and build the full-text index."""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from .extract import MAX_TEXT, extract
from .model import Bookmark
from .netsafe import USER_AGENT, HostThrottle, fetch
from .snapshot import build_snapshot

PDF_MAX = 15_000_000


class Robots:
    """Per-host robots.txt cache; an unreachable robots.txt means 'allowed'."""

    def __init__(self, timeout: float = 5):
        self.timeout, self._lock = timeout, threading.Lock()
        self._cache: dict = {}

    def allowed(self, url: str, throttle=None) -> bool:
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        with self._lock:
            rp = self._cache.get(origin)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            r = fetch(origin + "/robots.txt", timeout=self.timeout, max_bytes=200_000, throttle=throttle)
            if r.status == 200:
                rp.parse(r.body.decode("utf-8", "replace").splitlines())
            else:
                rp.parse([])
            with self._lock:
                self._cache[origin] = rp
        return rp.can_fetch(USER_AGENT, url) and rp.can_fetch("*", url)


def _decode(body: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type, re.I)
    enc = m.group(1) if m else None
    if not enc:
        m2 = re.search(rb"charset=[\"']?([\w-]+)", body[:2048], re.I)
        enc = m2.group(1).decode("ascii", "ignore") if m2 else "utf-8"
    try:
        return body.decode(enc, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def pdf_text(data: bytes, limit_pages: int = 30) -> str:
    """Text of a PDF via poppler's ``pdftotext`` when installed; empty string otherwise."""
    exe = shutil.which("pdftotext")
    if not exe:
        return ""
    try:
        proc = subprocess.run([exe, "-q", "-l", str(limit_pages), "-", "-"], input=data, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return re.sub(r"\s+", " ", proc.stdout.decode("utf-8", "replace")).strip()[:MAX_TEXT]


def archive_one(b: Bookmark, root: Path, *, timeout, assets, throttle, robots, keep_snapshot=True):
    if robots is not None and not robots.allowed(b.url, throttle):
        return b, False, "blocked by robots.txt"
    is_pdf_url = urlsplit(b.url).path.lower().endswith(".pdf")
    r = fetch(b.url, timeout=timeout, max_bytes=PDF_MAX if is_pdf_url else 3_000_000, throttle=throttle)
    if r.status != 200:
        return b, False, r.error or f"HTTP {r.status}"
    ctype = r.content_type.lower()
    d = root / "archive"
    if "pdf" in ctype or is_pdf_url:
        text = pdf_text(r.body)
        b.content = text
        if not b.title or b.title == b.url:
            b.title = Path(urlsplit(b.url).path).name or b.title
        if keep_snapshot:
            d.mkdir(exist_ok=True)
            (d / f"{b.id}.pdf").write_bytes(r.body)
            b.archive_path = f"archive/{b.id}.pdf"
        b.meta = {**b.meta, "type": "pdf"}
        return b, bool(text), "" if text else "PDF text unavailable (install poppler's pdftotext)"
    if "html" not in ctype:
        return b, False, f"unsupported content type {ctype or 'unknown'}"
    page = _decode(r.body, r.content_type)
    info = extract(page)
    b.content = (info["description"] + " " + info["text"])[:MAX_TEXT].strip()
    if not b.title or b.title == b.url:
        b.title = info["title"] or b.title
    b.meta = {**b.meta, **{k: info[k] for k in ("canonical", "published", "author", "lang") if info.get(k)}}
    if info["title"]:
        b.meta["page_title"] = info["title"][:300]
    if keep_snapshot:
        d.mkdir(exist_ok=True)
        (d / f"{b.id}.html").write_text(
            build_snapshot(page, r.final_url, assets=assets, timeout=timeout, throttle=throttle), encoding="utf-8")
        b.archive_path = f"archive/{b.id}.html"
    return b, True, ""


def archive_all(bms, root, *, workers=8, timeout=15, assets=True, keep_snapshot=True, respect_robots=True,
                per_host=2, interval=0.4, progress=None):
    root = Path(root)
    web = [b for b in bms if b.url.lower().startswith(("http://", "https://"))]
    throttle = HostThrottle(per_host, interval)
    robots = Robots() if respect_robots else None

    def work(b):
        return archive_one(b, root, timeout=timeout, assets=assets, throttle=throttle, robots=robots,
                           keep_snapshot=keep_snapshot)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for i, res in enumerate(pool.map(work, web), 1):
            if progress:
                progress(i, len(web))
            yield res
