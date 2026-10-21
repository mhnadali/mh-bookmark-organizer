"""Library health report."""
from __future__ import annotations

import re
from collections import Counter

from .dedupe import find_groups
from .model import is_unfiled
from .urlnorm import host_of

_ERROR_TITLE = re.compile(
    r"^\s*(?:error\s*)?(?:4\d\d|5\d\d)\b|\b(?:not found|access denied|forbidden|just a moment|attention required|"
    r"bad gateway|service unavailable|gateway time-?out|connection timed out|a timeout occurred|site can.t be reached|"
    r"this page isn.t working|page not found|err_[a-z_]+)\b", re.I)


def is_error_title(title: str) -> bool:
    """A bookmark saved while the site showed an error page keeps that error as its title."""
    return bool(_ERROR_TITLE.search(title or "")) and len(title) < 90


def build(bms, folders=()) -> dict:
    groups = find_groups(bms)
    used: set = set()
    for b in bms:
        parts = b.folder.split("/") if b.folder else []
        used.update("/".join(parts[: i + 1]) for i in range(len(parts)))
    counts = Counter(b.folder or "(unfiled)" for b in bms)
    return {
        "total": len(bms),
        "unfiled": sum(1 for b in bms if is_unfiled(b)),
        "untagged": sum(1 for b in bms if not b.tags),
        "untitled": sum(1 for b in bms if not b.title or b.title == b.url),
        "error_titles": [{"id": b.id, "title": b.title, "url": b.url} for b in bms if is_error_title(b.title)][:50],
        "duplicate_groups": len(groups),
        "duplicate_extras": sum(len(g) - 1 for g in groups),
        "status": dict(Counter(b.status or "unchecked" for b in bms)),
        "archived": sum(1 for b in bms if b.archive_path or b.content),
        "empty_folders": sorted(set(folders) - used),
        "max_folder_depth": max((b.folder.count("/") + 1 for b in bms if b.folder), default=0),
        "top_folders": counts.most_common(10),
        "top_domains": Counter(host_of(b.url) for b in bms).most_common(10),
        "top_tags": Counter(t for b in bms for t in b.tags).most_common(10),
    }


def render(r: dict) -> str:
    from .i18n import t

    def pairs(rows):
        return "\n".join(f"    {n:>5}  {k}" for k, n in rows) or "    " + t("none")

    return "\n".join([
        t("report_total", total=r["total"], archived=r["archived"]),
        t("report_unfiled", unfiled=r["unfiled"], untagged=r["untagged"], untitled=r["untitled"]),
        t("report_dups", extra=r["duplicate_extras"], groups=r["duplicate_groups"]),
        t("link_status", status=r["status"]),
        t("report_errtitles", n=len(r["error_titles"])),
        t("report_folders", depth=r["max_folder_depth"], empty=len(r["empty_folders"])),
        t("report_top_folders"), pairs(r["top_folders"]),
        t("report_top_domains"), pairs(r["top_domains"]),
        t("report_top_tags"), pairs(r["top_tags"]),
    ])
