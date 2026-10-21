"""Learning from what you do with the suggestions.

Every suggestion that reaches you (a plan file, the review page, the web UI, the browser extension, ``triage``) is
remembered. When you act on it the outcome is recorded:

* ``accepted``  - you kept it (or filed the bookmark somewhere inside the suggested folder);
* ``corrected`` - you filed the bookmark in a different folder;
* ``rejected``  - you threw the suggestion away without choosing another folder.

Two things use the record. The model's stated confidence is checked against what really happened (``Outcomes`` in
``learn.py``), and a folder you turned down for a bookmark is never suggested for it again.

Only *explicit* decisions (the review page, ``triage``, the extension's save button, the web UI) count towards the
confidence check. Applying a plan file that nobody looked at proves nothing, so it is recorded but not counted.
"""
from __future__ import annotations

from collections import defaultdict

from .model import Bookmark
from .urlnorm import host_of, normalize_url

EXPLICIT_CHANNELS = frozenset({"review", "triage", "extension", "ui"})
ACTIONS = ("accepted", "corrected", "rejected")
BINS = (0.5, 0.6, 0.7, 0.8, 0.9, 1.0001)


def _is_under(node: str, folder: str) -> bool:
    return folder == node or folder.startswith(node + "/")


def issue(lib, plan: dict, bms) -> int:
    """Remember the filing suggestions in ``plan`` (``{id: entry}``); entries without a folder are not about filing."""
    by_id = {b.id: b for b in bms}
    rows = []
    for bid, entry in plan.items():
        b = by_id.get(bid)
        if b is None or not isinstance(entry, dict) or not isinstance(entry.get("folder"), str) or entry.get("delete"):
            continue
        if entry.get("source") not in ("learned", "rules", "triage"):
            continue
        rows.append({"bm_id": bid, "norm": b.norm or normalize_url(b.url), "folder": entry["folder"], "level": entry.get("level", "exact"),
                     "confidence": entry.get("confidence"), "source": entry.get("source", "")})
    return lib.record_suggestions(rows) if rows else 0


def settle(lib, b: Bookmark, final: str, channel: str, *, suggested=None, confidence=None, level=None, source="", action=None) -> str:
    """Record the outcome for ``b``, now filed in ``final``. Returns the action, or '' when there was nothing to judge.

    The suggestion is ``suggested`` when the caller carries it (the review page and the extension do); otherwise it is
    the latest open suggestion stored for the bookmark.
    """
    sug = lib.open_suggestion(b.id)
    if suggested is None:
        if sug is None:
            return ""
        suggested, confidence, level, source = sug["folder"], sug["confidence"], sug["level"], sug["source"]
    elif sug is not None and sug["folder"] != suggested:
        sug = None  # the caller's suggestion is not the one we stored; do not close an unrelated record
    if not action:
        action = "accepted" if final and _is_under(suggested, final) else "corrected"
    lib.add_feedback(bm_id=b.id, norm=b.norm or normalize_url(b.url), host=host_of(b.url), suggested=suggested, final=final, action=action,
                     confidence=confidence, level=level or "exact", source=source, channel=channel,
                     explicit=channel in EXPLICIT_CHANNELS, suggestion=sug["id"] if sug else None)
    return action


def settle_plan(lib, res, plan: dict, bms_by_id: dict, default_channel: str = "plan-file", reviewed_channel: str = "review") -> dict:
    """Record outcomes for an applied plan: every changed bookmark, and every entry the reviewer threw away.

    A plan entry carries ``reviewed: true`` when somebody looked at it item by item (the review page, the web UI);
    those decisions are explicit and count as evidence. ``reviewed_channel`` names where the review happened.
    """
    counts = dict.fromkeys(ACTIONS, 0)
    for b in res.changed:
        entry = plan.get(b.id) or {}
        if "folder" not in entry:
            continue
        channel = reviewed_channel if entry.get("reviewed") else default_channel
        got = settle(lib, b, b.folder, channel, suggested=entry.get("suggested"), confidence=entry.get("confidence"),
                     level=entry.get("level"), source=entry.get("source", ""))
        if got:
            counts[got] += 1
    for bid, entry in getattr(res, "declined", []):
        b = bms_by_id.get(bid)
        if b is None:
            continue
        channel = reviewed_channel if entry.get("reviewed") else default_channel
        got = settle(lib, b, b.folder, channel, suggested=entry.get("suggested"), confidence=entry.get("confidence"),
                     level=entry.get("level"), source=entry.get("source", ""), action="rejected")
        if got:
            counts[got] += 1
    return counts


def outcomes(lib) -> list:
    """``[(confidence quoted, accepted?)]`` for explicit decisions: the evidence the confidence is checked against."""
    return [(r["confidence"], r["action"] == "accepted") for r in lib.feedback_rows(explicit_only=True) if r["confidence"] is not None]


def rejections(lib) -> dict:
    """``{normalised URL: {folders turned down for it}}``: never suggest these again."""
    out: dict = defaultdict(set)
    for r in lib.feedback_rows():
        if r["action"] in ("corrected", "rejected") and r["suggested"]:
            out[r["norm"]].add(r["suggested"])
    return dict(out)


def exclusions_for(lib, bms) -> dict:
    """``{bookmark id: {folders}}`` for the bookmarks in ``bms`` that have rejected suggestions."""
    rej = rejections(lib)
    if not rej:
        return {}
    return {b.id: rej[b.norm or normalize_url(b.url)] for b in bms if (b.norm or normalize_url(b.url)) in rej}


def summary(lib) -> dict:
    """Counts by action and channel, and the acceptance rate per quoted-confidence band (explicit decisions only)."""
    rows = lib.feedback_rows()
    by_action: dict = defaultdict(int)
    by_channel: dict = defaultdict(int)
    for r in rows:
        by_action[r["action"]] += 1
        by_channel[r["channel"]] += 1
    bands = []
    explicit = [r for r in rows if r["explicit"] and r["confidence"] is not None]
    for lo, hi in zip(BINS, BINS[1:]):
        inside = [r for r in explicit if lo <= r["confidence"] < hi]
        if inside:
            ok = sum(1 for r in inside if r["action"] == "accepted")
            bands.append({"from": lo, "to": min(hi, 1.0), "decisions": len(inside), "accepted": ok,
                          "quoted": round(sum(r["confidence"] for r in inside) / len(inside), 3), "observed": round(ok / len(inside), 3)})
    return {"total": len(rows), "explicit": len(explicit), "by_action": dict(by_action), "by_channel": dict(by_channel), "bands": bands}
