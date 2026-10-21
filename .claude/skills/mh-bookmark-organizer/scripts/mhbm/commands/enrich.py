"""Command group: enrich - tags, synonyms, auto-tagging, title cleanup, canonical URLs."""
from __future__ import annotations

import json
import sys
from collections import Counter

from .. import autotag, canonical, synonyms, titles
from ..model import clean_tags
from .common import Out, _apply_plan_cmd, _lib, _write_plan


def _preview_changes(o, bms, plan, limit, field):
    by_id = {b.id: b for b in bms}
    for bid, c in list(plan.items())[:limit]:
        b = by_id.get(bid)
        if b is None:
            continue
        if field == "tags":
            o.say(f"{(b.title or b.url)[:60]}\n    + {', '.join(c['tags'])}")
        elif field == "title":
            o.say(f"{b.title[:70]!r}\n    -> {c['title'][:70]!r}   ({c['reason']})")
        else:
            o.say(f"{b.url}\n    -> {c['url']}   ({c['reason']})")


def _plan_command(args, S, name, plan, field, o, lib, bms, extra=None):
    o.say(f"{len(plan)} bookmarks would change ({name})")
    _preview_changes(o, bms, plan, args.show, field)
    o.set(count=len(plan), plan=plan if getattr(args, "explain", False) else None, **(extra or {}))
    if args.plan_out:
        _write_plan(args.plan_out, plan, o)
    elif args.apply:
        _apply_plan_cmd(lib, bms, plan, name, o, 99, True)
    else:
        o.say("dry run; add --apply, or --plan-out FILE to review/trim first")
    o.finish()


def cmd_autotag(args, S):
    lib, o = _lib(args), Out(args)
    bms = lib.all()
    plan = autotag.propose(bms, max_tags=args.max, min_df=args.min_df, only_untagged=not args.all)
    _plan_command(args, S, "autotag", plan, "tags", o, lib, bms)


def cmd_titles(args, S):
    lib, o = _lib(args), Out(args)
    bms = lib.all()
    plan = titles.propose(bms, keep_counters=args.keep_counters, strip_brand=not args.no_brand)
    _plan_command(args, S, "titles", plan, "title", o, lib, bms)


def cmd_canonical(args, S):
    lib, o = _lib(args), Out(args)
    bms = lib.all()
    plan, review = canonical.propose(bms)
    if not any((b.meta or {}).get("canonical") for b in bms):
        o.say("no canonical URLs known yet - run `archive` first (it records each page's declared canonical URL)")
    for r in review[: args.show]:
        o.say(f"review: {r['url']}\n    canonical {r['canonical']}  ({r['reason']})")
    _plan_command(args, S, "canonical", plan, "url", o, lib, bms, {"needs_review": review[:200]})


def cmd_tags(args, S):
    lib, o = _lib(args), Out(args)
    bms = lib.all(snippet=0)
    counts = Counter(t for b in bms for t in b.tags)
    if args.rename:
        old, new = (clean_tags([x])[0] if clean_tags([x]) else "" for x in args.rename)
        hit = [b for b in bms if old in b.tags]
        for b in hit:
            b.tags = clean_tags([new if t == old else t for t in b.tags])
        o.say(f"{len(hit)} bookmarks {'retagged' if args.apply else 'would be retagged'}: {old} -> {new}")
        o.set(retagged=len(hit))
        if args.apply and hit:
            with lib.op("rename-tag"):
                lib.upsert(hit)
        return o.finish()
    if args.normalize or args.suggest_merge:
        data = synonyms.load(lib.root)
        if args.suggest_merge:
            found = synonyms.suggest_tag_merges(counts)
            o.set(suggestions=found)
            for s in found[: args.show]:
                o.say(f"{s['keep']} <- {s['merge']}   ({s['reason']}; {s['uses'][0]} vs {s['uses'][1]} uses)")
            if args.apply:
                for s in found:
                    synonyms.add(data, "tags", s["keep"], [s["merge"]])
                synonyms.save(lib.root, data)
                synonyms.configure(lib.root)
                o.say(f"saved {len(found)} synonyms")
            elif found:
                o.say("save them all with `tags --suggest-merge --apply` (then `tags --normalize --apply`)")
        raw = {r[0]: json.loads(r[1] or "[]") for r in lib.db.execute("SELECT id, tags FROM bookmarks")}
        fresh = lib.all(snippet=0)  # re-read after any synonym change so tags are normalised with the new map
        changed = [b for b in fresh if raw.get(b.id) != b.tags]
        if args.normalize:
            o.say(f"{len(changed)} bookmarks carry tags that differ from the synonym map" + ("" if args.apply else " (dry run; add --apply)"))
            o.set(to_normalise=len(changed))
            if args.apply and changed:
                o.say("snapshot:", lib.snapshot("pre-normalize"))
                with lib.op("normalize-tags"):
                    lib.upsert(changed)
        return o.finish()
    rows = [(t, n) for t, n in counts.most_common() if n >= args.min]
    o.set(tags=dict(rows))
    for t, n in rows[: args.show if args.show else None]:
        o.say(f"{n:>5}  {t}")
    o.say(f"{len(rows)} tags ({sum(1 for b in bms if not b.tags)} bookmarks untagged)")
    o.finish()


def cmd_synonyms(args, S):
    lib, o = _lib(args), Out(args)
    data = synonyms.load(lib.root)
    if args.action == "list":
        o.set(**data)
        for kind in ("tags", "folders"):
            for canon, aliases in sorted(data[kind].items()):
                o.say(f"{kind[:-1]:<7} {canon}  <-  {', '.join(aliases)}")
        if not any(data.values()):
            o.say("no synonyms yet: `synonyms add --tag machine-learning ml` or `tags --suggest-merge`")
    elif args.action == "add":
        kind = "tags" if args.tag else "folders" if args.folder else None
        names = args.tag or args.folder
        if not kind or len(names) < 2:
            sys.exit("usage: synonyms add --tag CANONICAL ALIAS [ALIAS...]  (or --folder ...)")
        synonyms.add(data, kind, names[0], names[1:])
        synonyms.save(lib.root, data)
        o.say(f"saved; {kind[:-1]} {names[0]} <- {', '.join(names[1:])}. Run `tags --normalize --apply` to update existing bookmarks.")
    elif args.action == "remove":
        kind = "tags" if args.tag else "folders"
        names = args.tag or args.folder or []
        if not names:
            sys.exit("usage: synonyms remove --tag NAME  (or --folder NAME)")
        if not synonyms.remove(data, kind, names[0]):
            sys.exit(f"no such synonym: {names[0]}")
        synonyms.save(lib.root, data)
        o.say("removed")
    o.finish()


def register(add, apply_flag, net_flags):
    def plan_flags(sp):
        sp.add_argument("--plan-out", help="write a reviewable plan instead of applying")
        apply_flag(sp)

    sp = add("autotag", cmd_autotag, "suggest keyword tags from titles, URLs, page text and your folders")
    sp.add_argument("--max", type=int, default=3, help="tags per bookmark")
    sp.add_argument("--min-df", type=int, default=3, dest="min_df", help="a tag must occur in at least this many bookmarks")
    sp.add_argument("--all", action="store_true", help="also add tags to bookmarks that already have some")
    plan_flags(sp)

    sp = add("titles", cmd_titles, "clean titles: notification counters, repeated site names, empty/URL titles")
    sp.add_argument("--keep-counters", action="store_true", help="keep '(3)' style counters")
    sp.add_argument("--no-brand", action="store_true", help="do not strip site-name prefixes/suffixes")
    plan_flags(sp)

    sp = add("canonical", cmd_canonical, "update URLs to the page's declared canonical URL when the change is cosmetic")
    plan_flags(sp)

    sp = add("tags", cmd_tags, "list tags; --rename OLD NEW; --suggest-merge; --normalize")
    sp.add_argument("--min", type=int, default=1)
    sp.add_argument("--rename", nargs=2, metavar=("OLD", "NEW"))
    sp.add_argument("--suggest-merge", action="store_true", dest="suggest_merge")
    sp.add_argument("--normalize", action="store_true", help="rewrite stored tags using the synonym map")
    apply_flag(sp)

    sp = add("synonyms", cmd_synonyms, "tag/folder synonyms (ml = machine-learning)")
    sp.add_argument("action", choices=["list", "add", "remove"])
    sp.add_argument("--tag", nargs="+")
    sp.add_argument("--folder", nargs="+")
