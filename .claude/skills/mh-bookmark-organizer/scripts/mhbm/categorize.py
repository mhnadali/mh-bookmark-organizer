"""Folder/tag proposals (learned + rules), the AI hand-off protocol and plan application.

Everything funnels into one *plan* format so a reviewer can edit or trim it::

    {"<bookmark id>": {"folder": "A/B", "tags": ["x"], "title": "...", "notes": "...", "delete": false,
                       "reason": "...", "confidence": 0.8}}
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .model import Bookmark, clean_folder, clean_tags, folder_synonym, is_unfiled
from .urlnorm import host_of, normalize_url

DEFAULT_TAXONOMY = Path(__file__).resolve().parent / "data" / "taxonomy.json"
MIN_SCORE = 2


def load_taxonomy(path=None) -> list:
    data = json.loads(Path(path or DEFAULT_TAXONOMY).read_text(encoding="utf-8"))
    return data["categories"]


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def classify(b: Bookmark, taxonomy: list):
    """Rule-based: return ``(folder, tags, score)`` for the best category, or ``("", [], score)``."""
    host = host_of(b.url)
    haystack = " ".join([b.title, b.url, b.notes, b.content[:2000]]).lower()
    best: tuple = ("", [], 0)
    for cat in taxonomy:
        score = 3 * sum(_host_matches(host, d) for d in cat.get("domains", []))
        hits = [k for k in cat.get("keywords", [])
                if re.search(rf"(?<![a-z0-9]){re.escape(k.lower())}(?![a-z0-9])", haystack)]
        score += len(hits)
        if score > best[2]:
            best = (cat["folder"], clean_tags(cat.get("tags", []) + hits[:3]), score)
    return best if best[2] >= MIN_SCORE else ("", [], best[2])


def _existing(folder: str, known_lower: dict):
    """Map a taxonomy folder onto an existing one: the same full path (case-insensitive) or nothing.

    Matching on a trailing name alone ("Productivity") filed unrelated bookmarks into whatever folder happened to share it.
    """
    return known_lower.get(folder.lower())


def propose(bms, taxonomy, model=None, *, only_unfiled: bool = True, min_conf: float = 0.85,
            retag: bool = True, allow_new_folders=None, rules=None, max_up: int = 1, known_folders=None, exclude=None) -> dict:
    """Build a plan: your rules first, then the learned model, then the generic taxonomy.

    ``min_conf`` is the probability (calibrated on your own library) that a suggestion is right; the learned model
    answers one level up (``max_up``) when it is sure of the area but not the sub-folder.
    When the library already has an established tree the generic taxonomy may only file into folders that already
    exist, so it never sprouts a parallel structure; ``allow_new_folders=True`` lifts that.
    ``exclude`` maps a bookmark id to folders that were already turned down for it; they are not suggested again.
    """
    from . import rules as rules_mod

    established = model is not None and model.ready
    if allow_new_folders is None:
        allow_new_folders = not established
    known = sorted(known_folders) if known_folders is not None else sorted({b.folder for b in bms if b.folder and not is_unfiled(b)})
    known_lower = {f.lower(): f for f in known}
    plan = {}
    exclude = exclude or {}
    for b in bms:
        if only_unfiled and not is_unfiled(b):
            continue
        choice = rules_mod.apply(b, rules, known) if rules else None
        if choice is None and established:
            p = model.predict(b, min_conf, max_up=max_up, exclude=exclude.get(b.id, ()))
            if p:
                why = "similar to " + "; ".join(p["why"]) if p["why"] else "learned from your folders"
                if p["level"] != "exact":
                    why = f"sure of the area, not the sub-folder ({p['level']} of the best match); " + why
                choice = {"folder": p["folder"], "tags": p["tags"], "source": "learned", "confidence": p["confidence"],
                          "level": p["level"], "alternatives": p["alternatives"], "reason": why}
        if choice is None:
            folder, tags, score = classify(b, taxonomy)
            if folder and not allow_new_folders:
                folder = _existing(folder, known_lower) or ""
            if folder in exclude.get(b.id, ()):
                folder = ""
            if folder:
                choice = {"folder": folder, "tags": tags, "source": "rules", "confidence": min(1.0, score / 6),
                          "level": "exact", "reason": f"taxonomy rule match (score {score})"}
        if choice is None:
            continue
        if not retag:
            choice["tags"] = []
        if choice["folder"] != b.folder or any(t not in b.tags for t in choice["tags"]):
            plan[b.id] = choice
    return plan


def ai_batch(bms, taxonomy, model=None, *, limit: int = 200, only_unfiled: bool = True, rules=None,
             min_conf: float = 0.85, exclude=None) -> dict:
    """Items nothing could place with confidence, for an LLM to decide - each with the model's best guesses as hints."""
    exclude = exclude or {}
    placed = propose(bms, taxonomy, model, only_unfiled=only_unfiled, rules=rules, min_conf=min_conf, exclude=exclude)
    items = []
    for b in bms:
        if only_unfiled and not is_unfiled(b):
            continue
        if b.id in placed:
            continue
        item = {"id": b.id, "title": b.title[:120], "host": host_of(b.url), "url": b.url[:200],
                "snippet": (b.content or b.notes)[:240]}
        if model is not None and model.ready:
            item["candidates"] = [{"folder": f, "score": round(p, 2)} for f, p in model.rank(b, 3, exclude.get(b.id, ()))]
        items.append(item)
        if len(items) >= limit:
            break
    return {
        "instructions": ("Each item may list `candidates` (the model's best guesses; scores are not certainties). "
                         "Return JSON {id: {\"folder\": \"Parent/Child\", \"tags\": [\"a\",\"b\"], "
                         "\"title\": \"optional cleaner title\"}} for every id. Reuse existing folder names; "
                         "prefer an existing folder (any depth); a NEW folder may be at most 3 levels deep; 1-4 lowercase tags."),
        "existing_folders": sorted({b.folder for b in bms if b.folder}),
        "taxonomy_folders": sorted({c["folder"] for c in taxonomy}),
        "items": items,
    }


def resolve_folder(folder: str, known) -> str:
    """Map a folder path onto an existing one: verbatim if it exists, else the existing folder that matches ignoring case
    and stray spaces around names (so a typed ``Work/Docs`` lands in the user's ``Work /Docs`` instead of making a twin)."""
    known = list(known)
    if folder in known:
        return folder
    cleaned = folder_synonym(clean_folder(folder))
    if cleaned in known:
        return cleaned
    lookup: dict = {}
    for f in known:
        lookup.setdefault(clean_folder(f).lower(), f)
    return lookup.get(cleaned.lower(), cleaned)


@dataclass
class PlanResult:
    changed: list = field(default_factory=list)
    deleted: list = field(default_factory=list)
    rejected: list = field(default_factory=list)  # entries that are invalid, with the reason
    declined: list = field(default_factory=list)  # (id, entry) the reviewer threw away on purpose: valid, but "no"

    @property
    def applied(self) -> int:
        return len(self.changed) + len(self.deleted)


class _Folders:
    """Maps a folder named in a plan onto the user's existing spelling (exact, then ignoring case and stray spaces)."""

    def __init__(self, known_folders):
        known_list = sorted(known_folders)
        self.known = {f.lower() for f in known_list}
        self.exact = {f: f for f in known_list}
        self.lookup: dict = {}
        for f in known_list:
            self.lookup.setdefault(clean_folder(f).lower(), f)

    def resolve(self, wanted: str) -> str:
        return self.exact.get(wanted) or self.lookup.get(clean_folder(wanted).lower()) or clean_folder(wanted)

    def too_deep(self, folder: str, max_depth: int) -> bool:
        return bool(folder) and folder.lower() not in self.known and folder.count("/") + 1 > max_depth


def _new_url(change: dict) -> tuple:
    """``(url, problem)``: the replacement address in a plan entry, validated (an empty url means 'unchanged')."""
    url = str(change["url"]).strip() if change.get("url") else ""
    if url:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or len(url) > 2000:
            return url, "url must be an http(s) address"
    return url, ""


def _apply_entry(b, change: dict, folder: str, new_url: str) -> None:
    """Apply a plan entry that has passed every check, as a whole."""
    b.folder = folder
    extra = change.get("tags", [])
    b.tags = clean_tags(b.tags + (clean_tags(extra) if isinstance(extra, str) else extra[:6]))
    if change.get("title"):
        b.title = change["title"].strip()[:200]
    if new_url:
        b.url = new_url
        b.norm = normalize_url(b.url)
    if change.get("notes"):
        b.notes = change["notes"].strip()[:2000]
    if change.get("added") is not None:
        b.added = change["added"]


def apply_plan(bms, plan: dict, *, max_depth: int = 3, known_folders=None) -> PlanResult:
    """Validate and apply a plan in memory; the caller persists ``changed`` and ``deleted``.

    ``max_depth`` limits only *new* folders; a folder that already exists (``known_folders``, default: those in use)
    may be as deep as the user made it. A rejected entry never changes anything.
    """
    folders = _Folders(known_folders if known_folders is not None else {b.folder for b in bms if b.folder})
    by_id = {b.id: b for b in bms}
    res = PlanResult()
    for bid, change in plan.items():
        b = by_id.get(bid)
        if b is None:
            res.rejected.append(f"{bid}: unknown id")
        elif not isinstance(change, dict):
            res.rejected.append(f"{bid}: entry must be an object")
        elif change.get("rejected") is True:  # the review page marks a suggestion the user turned down
            res.declined.append((bid, change))
        elif change.get("delete") is True:
            res.deleted.append(b)
        else:
            _plan_entry(res, bid, b, change, folders, max_depth)
    return res


def _plan_entry(res, bid, b, change: dict, folders: _Folders, max_depth: int) -> None:
    """Check one entry in a fixed order (types, then folder depth, then address); apply it only if every check passes."""
    problem = _entry_problem(change)
    folder, new_url = "", ""
    if not problem:
        folder = folders.resolve(change.get("folder", b.folder))
        if folders.too_deep(folder, max_depth):
            problem = f"new folder deeper than {max_depth}"
    if not problem:
        new_url, problem = _new_url(change)
    if problem:
        res.rejected.append(f"{bid}: {problem}")
        return
    _apply_entry(b, change, folder, new_url)
    res.changed.append(b)


def _entry_problem(change: dict) -> str:
    """Why a plan entry is malformed ('' when it is fine). Wrong types are refused, not coerced into nonsense."""
    for key in ("folder", "title", "notes", "url", "suggested"):
        if key in change and change[key] is not None and not isinstance(change[key], str):
            return f"{key} must be a string"
    if "folder" in change and change["folder"] is None:
        return "folder must be a string"
    tags = change.get("tags", [])
    if not isinstance(tags, (str, list)) or (isinstance(tags, list) and not all(isinstance(t, str) for t in tags)):
        return "tags must be a list of strings"
    added = change.get("added")
    if added is not None and (isinstance(added, bool) or not isinstance(added, int) or added < 0):
        return "added must be a non-negative integer"
    return ""
