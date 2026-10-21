"""Tag and folder synonyms: ``ml`` and ``machine-learning`` are one tag; ``Docs`` and ``Documentation`` one folder.

``<home>/synonyms.json``::

    {"tags": {"machine-learning": ["ml", "machinelearning"]}, "folders": {"Work/Documentation": ["Work/Docs"]}}
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from . import model
from .model import clean_folder


class SynonymError(ValueError):
    pass


def path_for(home) -> Path:
    return Path(home) / "synonyms.json"


def load(home) -> dict:
    p = path_for(home)
    if not p.exists():
        return {"tags": {}, "folders": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SynonymError(f"{p}: {e}") from e
    if not isinstance(data, dict):
        raise SynonymError(f"{p}: expected an object")
    return {"tags": dict(data.get("tags", {})), "folders": dict(data.get("folders", {}))}


def save(home, data) -> None:
    Path(home).mkdir(parents=True, exist_ok=True)
    path_for(home).write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def reverse(mapping: dict, norm) -> dict:
    """alias -> canonical (both normalised); a canonical never aliases itself."""
    out = {}
    for canon, aliases in mapping.items():
        c = norm(canon)
        for a in aliases:
            na = norm(a)
            if na and na != c:
                out[na] = c
    return out


def tag_norm(t: str) -> str:
    return str(t).strip().lower().replace(" ", "-")


def configure(home) -> None:
    """Activate this library's tag synonyms for every ``clean_tags`` call (same pattern as the URL tracking list)."""
    data = load(home)
    model.set_tag_synonyms(reverse(data["tags"], tag_norm))
    model.set_folder_synonyms({k.lower(): v for k, v in reverse(data["folders"], lambda p: clean_folder(p)).items()})


def add(data: dict, kind: str, canonical: str, aliases) -> None:
    norm = tag_norm if kind == "tags" else clean_folder
    canonical = norm(canonical)
    if not canonical:
        raise SynonymError("canonical name is empty")
    bucket = data[kind]
    current = bucket.get(canonical, [])
    for a in aliases:
        a = norm(a)
        if not a or a == canonical:
            continue
        for other, others in list(bucket.items()):  # an alias belongs to one canonical only
            if other != canonical and a in others:
                others.remove(a)
        if a in bucket and a != canonical:  # alias was itself a canonical: fold its aliases in
            current += bucket.pop(a)
            current.append(a)
        elif a not in current:
            current.append(a)
    bucket[canonical] = sorted(set(current))


def remove(data: dict, kind: str, name: str) -> bool:
    norm = tag_norm if kind == "tags" else clean_folder
    name = norm(name)
    bucket = data[kind]
    if name in bucket:
        del bucket[name]
        return True
    hit = False
    for canon, aliases in list(bucket.items()):
        if name in aliases:
            aliases.remove(name)
            hit = True
            if not aliases:
                del bucket[canon]
    return hit


def _squash(t: str) -> str:
    return re.sub(r"[^\w]", "", t.lower())


def _singular(t: str) -> str:
    return t[:-1] if t.endswith("s") and len(t) > 3 and not t.endswith("ss") else t


def _edit1(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1 or a == b:
        return False
    if len(a) == len(b):
        diff = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        return len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]])
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    i = j = skipped = 0
    while i < len(short) and j < len(long_):
        if short[i] == long_[j]:
            i += 1
        else:
            skipped += 1
        j += 1
        if skipped > 1:
            return False
    return True


def suggest_tag_merges(tag_counts: Counter) -> list:
    """Pairs that are probably the same tag: separators, plural forms, one typo, or initials (``ml`` / ``machine-learning``)."""
    tags = sorted(tag_counts, key=lambda t: (-tag_counts[t], t))
    out, seen = [], set()
    for i, a in enumerate(tags):
        for b in tags[i + 1:]:
            reason = None
            if _squash(a) == _squash(b):
                reason = "same letters, different separators"
            elif _singular(_squash(a)) == _singular(_squash(b)):
                reason = "singular/plural"
            elif len(a) >= 5 and len(b) >= 5 and _edit1(_squash(a), _squash(b)):
                reason = "one letter apart (typo?)"
            elif ("-" in b and len(a) >= 2 and a == "".join(w[0] for w in b.split("-") if w)) or \
                 ("-" in a and len(b) >= 2 and b == "".join(w[0] for w in a.split("-") if w)):
                reason = "initials of the longer tag"
            if reason and (a, b) not in seen:
                seen.add((a, b))
                out.append({"keep": a, "merge": b, "reason": reason, "uses": [tag_counts[a], tag_counts[b]]})
    return out
