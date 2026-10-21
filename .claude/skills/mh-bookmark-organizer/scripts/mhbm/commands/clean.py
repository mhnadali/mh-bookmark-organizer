"""Command group: clean."""
from __future__ import annotations

import sys
import time
from collections import Counter

from .. import dedupe as dedupe_mod
from .. import linkcheck, report
from ..i18n import t as tr
from ..urlnorm import normalize_url, strip_tracking
from .common import (
    Out,
    _batches,
    _lib,
    _progress,
    _row,
    _select,
    _write_plan,
)


def cmd_stats(args, S):
    lib, o = _lib(args), Out(args)
    r = report.build(lib.all(snippet=1), lib.folders())
    o.set(**r)
    o.say(report.render(r))
    o.finish()


def _show_groups(o, groups, limit):
    for g in groups[:limit]:
        o.say(f"- {g[0].norm or g[0].url}  ({len(g)} copies)")
        for b in g:
            o.say(f"    {b.id}  [{b.folder or 'unfiled'}] {b.title[:60]}  {b.url[:70]}")


def _ask_keeper(group):
    print()
    for n, b in enumerate(group, 1):
        print(f"  {n}) [{b.folder or 'unfiled'}] {b.title[:60]}  {b.url}")
    while True:
        ans = input("  keep which? (number, s=skip, q=quit): ").strip().lower()
        if ans in ("s", "q"):
            return ans
        if ans.isdigit() and 1 <= int(ans) <= len(group):
            return int(ans) - 1


def _review_groups(o, kept, removed, show):
    """Look-alike groups that need a human decision (never merged automatically), minus anything already being merged."""
    gone_ids = {b.id for b in removed}
    review_r = [(r, g) for r, g in dedupe_mod.fuzzy_groups(kept) if not any(b.id in gone_ids for b in g)]
    o.say(f"\n{len(review_r)} look-alike groups need a human decision (never merged automatically):")
    for reason, g in review_r[:show]:
        o.say(f"- {reason}")
        for b in g:
            o.say(f"    {b.id}  [{(b.folder or 'unfiled')[-40:]}] {b.title[:50]}  {b.url[:70]}")
    o.set(review_groups=[{"reason": r, "members": [_row(b) for b in g]} for r, g in review_r])
    return [g for _, g in review_r]


def _interactive_review(lib, o, review):
    if not sys.stdin.isatty():
        sys.exit("--interactive needs a terminal; use --plan-out and apply-plan instead")
    chosen_keep, chosen_drop = [], []
    for g in review:
        ans = _ask_keeper(g)
        if ans == "q":
            break
        if ans == "s":
            continue
        k, rest = dedupe_mod.merge_group([g[ans]] + [b for i, b in enumerate(g) if i != ans])
        chosen_keep.append(k)
        chosen_drop += rest
    if chosen_drop:
        with lib.op("dedupe-review"):
            lib.upsert(chosen_keep)
            lib.delete(b.id for b in chosen_drop)
        o.say(f"merged {len(chosen_drop)} reviewed duplicates")


def _dedupe_plan(groups, gone) -> dict:
    plan = {}
    for g in groups:
        k = next(b for b in g if id(b) not in gone)
        plan[k.id] = {"tags": k.tags, "notes": k.notes, "added": k.added, "reason": "kept (best of group)"}
        for b in g:
            if id(b) in gone:
                plan[b.id] = {"delete": True, "reason": f"duplicate of {k.id}"}
    return plan


def cmd_dedupe(args, S):
    lib, o = _lib(args), Out(args)
    bms = lib.all()
    kept, removed, groups = dedupe_mod.dedupe(bms, args.level, args.same_folder_only)
    gone = {id(b) for b in removed}
    keepers = [b for g in groups for b in g if id(b) not in gone]
    o.set(level=args.level, groups=len(groups), duplicates=len(removed))
    _show_groups(o, groups, args.show)
    o.say(tr("dup_summary", n=len(removed), g=len(groups), level=args.level))
    review = _review_groups(o, kept, removed, args.show) if (args.fuzzy or args.interactive) else []
    if args.interactive:
        _interactive_review(lib, o, review)
    if args.plan_out:
        _write_plan(args.plan_out, _dedupe_plan(groups, gone), o)
        return o.finish()
    if not args.apply:
        o.say(tr("dry_run_apply"))
        return o.finish()
    o.say("snapshot:", lib.snapshot("pre-dedupe"))
    with lib.op("dedupe"):
        lib.delete(b.id for b in removed)
        lib.upsert(keepers)
    o.say(tr("removed_remain", n=len(removed), left=lib.count()))
    o.set(removed=len(removed), library=lib.count())
    o.finish()


def cmd_clean_urls(args, S):
    lib, o = _lib(args), Out(args)
    changed = []
    for b in lib.all(snippet=0):
        clean = strip_tracking(b.url)
        if clean != b.url:
            changed.append((b, b.url, clean))
    for _b, old, new in changed[: args.show]:
        o.say(f"{old}\n  -> {new}")
    o.say(f"{len(changed)} URLs carry tracking parameters (only those parameters are removed)")
    o.set(count=len(changed), changes=[{"id": b.id, "old": old, "new": new} for b, old, new in changed[:500]])
    if args.apply and changed:
        o.say("snapshot:", lib.snapshot("pre-clean"))
        for b, _, new in changed:
            b.url = new
            b.norm = normalize_url(new)
        with lib.op("clean-urls"):
            lib.upsert([c[0] for c in changed])
    elif changed:
        o.say("dry run; add --apply to rewrite")
    o.finish()


def cmd_check(args, S):
    lib, o = _lib(args), Out(args)
    bms = _select(lib, args)
    now = time.time()
    if args.recheck_suspect:
        bms = [b for b in bms if b.status == "suspect"]
    if args.due_days is not None:
        bms = [b for b in bms if not b.last_checked or now - b.last_checked > args.due_days * 86400 or b.status == "suspect"]
    if args.limit:
        bms = bms[: args.limit]
    moved = 0
    total = len(bms)
    cb = _progress(args, tr("progress_checking"))
    done = 0
    offline = False
    for chunk in _batches(bms, 500):
        results = []
        for b, status, code, new_url, err in linkcheck.check_all(
                chunk, workers=S("workers"), timeout=S("timeout"), per_host=S("per_host"), interval=S("interval"),
                confirmations=S("confirmations"), min_gap=int(S("min_gap_hours") * 3600), wayback=args.wayback):
            done += 1
            cb(done, total)
            results.append((b, status, code, new_url, err))
            if new_url and args.follow_redirects:
                b.url, b.norm = new_url, normalize_url(new_url)
                moved += 1
        if linkcheck.looks_offline(results):
            offline = True
            o.say("\n" + tr("offline_warning", n=len(results), err=repr(results[0][4][:80])))
            break
        lib.upsert(chunk)
    counts = Counter(b.status or "unchecked" for b in lib.all(snippet=0))
    o.set(checked=total, status=dict(counts), redirects_rewritten=moved, offline=offline)
    o.say(tr("link_status", status=dict(counts)))
    if moved:
        o.say(f"rewrote {moved} permanently redirected URLs")
    o.say("`dead` = failed on several checks over time; `suspect` = failed recently, recheck later "
          "(`check --recheck-suspect`); `restricted` = alive but blocks bots.")
    o.finish()


def cmd_prune(args, S):
    lib, o = _lib(args), Out(args)
    doomed = [b for b in lib.all(snippet=0) if b.status in args.status]
    for b in doomed[: args.show]:
        o.say(f"[{b.status} {b.http_code}] {b.title[:50]}  {b.url}" + (f"\n    saved copy: {b.wayback}" if b.wayback else ""))
    o.say(tr("prune_summary", n=len(doomed), status=args.status))
    o.set(matches=[_row(b, status=b.status, wayback=b.wayback) for b in doomed[:500]], count=len(doomed))
    if args.apply:
        o.say("snapshot:", lib.snapshot("pre-prune"))
        with lib.op("prune"):
            lib.delete(b.id for b in doomed)
        o.say(tr("prune_removed", n=len(doomed)))
    else:
        o.say("dry run; add --apply to delete")
    o.finish()


def register(add, apply_flag, net_flags):
    add("stats", cmd_stats, "library health report")

    sp = add("dedupe", cmd_dedupe, "merge duplicates: exact, variant (mobile/AMP/canonical), fuzzy review")
    sp.add_argument("--level", choices=dedupe_mod.LEVELS, default="exact")
    sp.add_argument("--fuzzy", action="store_true", help="also list look-alike pages for review")
    sp.add_argument("--same-folder-only", action="store_true", help="only merge copies that sit in the same folder")
    sp.add_argument("--interactive", action="store_true", help="decide look-alike groups one by one (terminal)")
    sp.add_argument("--plan-out", help="write a reviewable plan instead of applying")
    apply_flag(sp)

    apply_flag(add("clean-urls", cmd_clean_urls, "strip tracking parameters (and nothing else) from stored URLs"))

    sp = add("check", cmd_check, "detect dead/redirected links with retries, per-host throttling and multi-run confirmation")
    sp.add_argument("--workers", type=int)
    sp.add_argument("--timeout", type=float)
    sp.add_argument("--per-host", type=int, dest="per_host")
    sp.add_argument("--confirmations", type=int)
    sp.add_argument("--min-gap-hours", type=float, dest="min_gap_hours")
    sp.add_argument("--due-days", type=float, help="only links not checked within N days (plus suspects)")
    sp.add_argument("--recheck-suspect", action="store_true")
    sp.add_argument("--wayback", action="store_true", help="look up Wayback Machine copies of failing links")
    sp.add_argument("--follow-redirects", action="store_true", help="rewrite permanently moved (301/308) URLs")
    net_flags(sp)

    sp = add("prune", cmd_prune, "delete bookmarks by link status (default: confirmed dead only)")
    sp.add_argument("--status", nargs="+", default=["dead"], choices=["dead", "suspect", "restricted"])
    apply_flag(sp)

