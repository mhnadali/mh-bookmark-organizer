"""Command group: library."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .. import archive as archive_mod
from .. import dashboard as dashboard_mod
from ..i18n import t as tr
from .common import (
    Out,
    _batches,
    _lib,
    _progress,
    _row,
    _select,
    )


def cmd_archive(args, S):
    lib, o = _lib(args), Out(args)
    bms = _select(lib, args)
    if args.unarchived:
        bms = [b for b in bms if not b.archive_path]
    if args.limit:
        bms = bms[: args.limit]
    ok = fail = 0
    errors = []
    cb = _progress(args, tr("progress_archiving"))
    done = 0
    for chunk in _batches(bms, 100):
        for b, success, err in archive_mod.archive_all(
                chunk, lib.root, workers=S("archive_workers"), timeout=S("archive_timeout"),
                assets=not args.no_assets, keep_snapshot=not args.text_only, respect_robots=not args.ignore_robots,
                per_host=S("per_host"), interval=S("interval")):
            done += 1
            cb(done, len(bms))
            ok += success
            fail += not success
            if not success:
                errors.append({"url": b.url, "error": err})
                if args.verbose:
                    o.say(f"skip {b.url}: {err}")
        lib.upsert(chunk)
        lib.save_pages(chunk)
    o.set(indexed=ok, skipped=fail, errors=errors[:100])
    o.say(f"indexed {ok} pages, skipped {fail}")
    o.finish()


def cmd_search(args, S):
    lib, o = _lib(args), Out(args)
    hits = lib.search(" ".join(args.query), args.limit, args.sort)
    o.set(results=[_row(b, tags=b.tags, snippet=s, status=b.status, saved_copy=b.archive_path) for b, s in hits])
    for b, snip in hits:
        o.say(f"{b.title or b.url}\n  {b.url}\n  [{b.folder or 'unfiled'}] {' '.join('#' + t for t in b.tags)}"
              + (f"\n  {snip}" if snip else ""))
    o.say(tr("results", n=len(hits)))
    o.finish()


def cmd_dashboard(args, S):
    lib, o = _lib(args), Out(args)
    out = Path(args.out) if args.out else lib.root / "dashboard.html"
    out.write_text(dashboard_mod.render(lib.all(snippet=200), lib.folders(), time.strftime("%Y-%m-%d %H:%M")), encoding="utf-8")
    o.say(f"dashboard written to {out}  (open it in a browser; works offline)")
    o.set(out=str(out))
    o.finish()


def cmd_review(args, S):
    """Write an offline page for reviewing a plan item by item."""
    from .. import review

    lib, o = _lib(args), Out(args)
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    out = Path(args.out) if args.out else lib.root / "review.html"
    out.write_text(review.render(plan, lib.all(snippet=0), lib.folders() | lib.used_folders()), encoding="utf-8")
    o.say(f"review page for {len(plan)} items written to {out}\nOpen it, untick or edit items, 'Download approved plan', then: apply-plan approved-plan.json --apply")
    o.set(out=str(out), items=len(plan))
    o.finish()


def cmd_mark(args, S):
    """Set the reading state / favourite flag of bookmarks."""
    lib, o = _lib(args), Out(args)
    if args.state is None and not args.favorite and not args.unfavorite:
        sys.exit("nothing to do: give --state and/or --favorite/--unfavorite")
    todo = []
    for bid in args.ids:
        b = lib.get(bid)
        if b is None:
            sys.exit(f"no bookmark with id {bid}")
        if args.state is not None:
            b.state = "" if args.state == "none" else args.state
        if args.favorite:
            b.favorite = True
        if args.unfavorite:
            b.favorite = False
        todo.append(b)
    with lib.op("mark"):
        lib.upsert(todo)
    o.set(updated=[_row(b, state=b.state, favorite=b.favorite) for b in todo])
    o.say(f"updated {len(todo)} bookmark(s)")
    o.finish()


def cmd_reading_list(args, S):
    """What is left to read, oldest first (or newest with --newest)."""
    lib, o = _lib(args), Out(args)
    wanted = {"unread": ("unread",), "reading": ("reading",), "all": ("unread", "reading")}[args.which]
    items = [b for b in lib.all(snippet=0) if b.state in wanted]
    items.sort(key=lambda b: b.added or 0, reverse=args.newest)
    items = items[: args.limit]
    o.set(items=[_row(b, state=b.state, favorite=b.favorite, added=b.added) for b in items])
    for b in items:
        o.say(f"{b.id}  [{b.state}]{' *' if b.favorite else ''} {b.title or b.url}\n        {b.url}")
    o.say(f"{len(items)} to read" if items else "nothing to read - mark bookmarks with `mark ID --state unread`")
    o.finish()


def register(add, apply_flag, net_flags):
    sp = add("archive", cmd_archive, "fetch pages, save sanitised single-file snapshots, build the full-text index")
    sp.add_argument("--unarchived", action="store_true", help="only pages not yet archived")
    sp.add_argument("--no-assets", action="store_true", help="do not inline images/styles")
    sp.add_argument("--text-only", action="store_true", help="index text, keep no snapshot file")
    sp.add_argument("--ignore-robots", action="store_true")
    sp.add_argument("--verbose", action="store_true")
    sp.add_argument("--workers", type=int, dest="archive_workers")
    sp.add_argument("--timeout", type=float, dest="archive_timeout")
    sp.add_argument("--per-host", type=int, dest="per_host")
    net_flags(sp)

    sp = add("search", cmd_search, 'search: words "phrase" -exclude tag: folder: site: status: is: before: after: title:')
    sp.add_argument("query", nargs="+")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--sort", choices=["relevance", "recent"], default="relevance")

    add("dashboard", cmd_dashboard, "write an offline HTML dashboard").add_argument("--out")

    sp = add("mark", cmd_mark, "set reading state (unread/reading/read/none) and favourite flag")
    sp.add_argument("ids", nargs="+")
    sp.add_argument("--state", choices=["unread", "reading", "read", "none"])
    sp.add_argument("--favorite", action="store_true")
    sp.add_argument("--unfavorite", action="store_true")

    sp = add("reading-list", cmd_reading_list, "list bookmarks still to read")
    sp.add_argument("--which", choices=["unread", "reading", "all"], default="all")
    sp.add_argument("--newest", action="store_true", help="newest first (default oldest first)")
    sp.add_argument("--limit", type=int, default=25)

    sp = add("review", cmd_review, "build an offline page to review a plan item by item and download the approved plan")
    sp.add_argument("plan")
    sp.add_argument("--out")
