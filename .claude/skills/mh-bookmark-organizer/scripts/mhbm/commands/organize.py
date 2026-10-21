"""Command group: organize."""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from .. import categorize, feedback, restructure
from ..i18n import t as tr
from ..model import clean_folder, clean_tags
from .common import (
    Out,
    _apply_plan_cmd,
    _lib,
    _preview_plan,
    _row,
    _select,  # noqa: F401
    _tail,
    _write_plan,
    exclusions_for,
    model_for,
    rules_for,
)


def cmd_organize(args, S):
    lib, o = _lib(args), Out(args)
    bms = lib.all()
    model = None if args.no_learn else model_for(lib, args, bms, retrain=args.retrain)
    target = S("min_confidence")
    plan = categorize.propose(bms, categorize.load_taxonomy(S("taxonomy")), model, only_unfiled=not args.refile,
                              min_conf=target, retag=not args.no_tags, rules=rules_for(lib),
                              allow_new_folders=True if args.allow_new_folders else None,
                              max_up=2 if args.coarse else 1, known_folders=lib.folders() | lib.used_folders(),
                              exclude=exclusions_for(lib, bms))
    src = Counter(c["source"] for c in plan.values())
    levels = Counter(c.get("level", "exact") for c in plan.values())
    state = tr("model_ready") if model and model.ready else tr("model_rules_only")
    o.say(tr("organize_model", state=state, pct=f"{target:.0%}"))
    _preview_plan(o, bms, plan, args.show)
    o.say(tr("classified", n=len(plan), sources=dict(src), levels=dict(levels)))
    o.set(classified=len(plan), sources=dict(src), levels=dict(levels), precision_target=target,
          plan=plan if args.explain else None)
    if args.plan_out:
        feedback.issue(lib, plan, bms)  # so applying (or trimming) the plan later can be judged against what was suggested
        _write_plan(args.plan_out, plan, o)
    elif args.apply:
        _apply_plan_cmd(lib, bms, plan, "organize", o, 99, True)
    else:
        o.say("dry run; add --apply, or --plan-out FILE to review/trim first")
    o.finish()


def cmd_learn(args, S):
    lib, o = _lib(args), Out(args)
    bms = lib.all()
    if args.curve or args.text_effect:
        return _learn_studies(args, lib, o, bms)
    if args.seeds and args.seeds > 1:           # repeat the cross-validation on different splits (not cached; takes N times longer)
        from .. import learn

        r = learn.evaluate(bms, args.seeds, progress=None if (args.quiet or args.json) else lambda msg: print(f"  {msg}", file=sys.stderr))
        m = None
        o.set(**r)
        if not r.get("ready"):
            o.say("not enough filed bookmarks (need 10 in 2+ folders)")
            return o.finish()
    else:
        m = model_for(lib, args, bms, retrain=args.retrain)
        o.set(ready=m.ready, **{"mode": m.mode, **m.report})
        if not m.ready:
            o.say(f"not enough filed bookmarks ({len(m.docs)} in {len(m.folders)} folders; need 10 in 2+ folders)")
            return o.finish()
        r = m.report
    if r.get("mode") == "blend":
        o.say(f"{r['training']} filed bookmarks in {r['folders']} folders - {r['note']}")
        return o.finish()
    o.say(tr("learn_title", n=r["training"], f=r["folders"]))
    o.say(tr("learn_measured"))
    o.say("  " + tr("learn_top", top1=f"{r['top1']:.0%}", top3=f"{r['top3']:.0%}") + "\n")
    if "top1_range" in r:
        lo, hi = r["top1_range"]
        o.say(f"  averaged over {r['runs']} different splits; top-1 ranged {lo:.0%}-{hi:.0%}\n")
    o.say("  " + tr("learn_table_head"))
    for tgt, t in r["targets"].items():
        def cell(x):
            return f"{x['share']:>4.0%} @ {x['precision']:.0%}" if x["precision"] is not None else "   -"
        o.say(f"    {float(tgt):.0%}:  {cell(t['exact']):>12} | {cell(t['parent']):>12} | {cell(t['grandparent']):>12}"
              f"   => {cell(t['overall'])} {tr('answered')}")
    o.say("\n" + tr("learn_hint"))
    o.finish()


def _learn_studies(args, lib, o, bms):
    """``learn --curve`` and ``learn --text-effect``: slower studies that train several times and do not touch the cached model."""
    from .. import learn

    def progress(msg):
        if not (args.quiet or args.json):
            print(f"  {msg}", file=sys.stderr)

    if args.curve:
        rows = learn.curve(bms, progress=progress)
        o.set(curve=rows)
        if not rows:
            o.say("not enough filed bookmarks for a curve (it needs at least 150 filed bookmarks)")
            return o.finish()
        o.say("accuracy as the number of filed bookmarks grows (judged on sittings the model has not seen)\n")
        for r in rows:
            o.say(f"  learning from {r['filed']:>5} bookmarks ({r['fraction']:.0%}):  best guess right {r['top1']:.0%}, among top 3 {r['top3']:.0%};"
                  f"  answering at 80%: {r['share']:.0%} of bookmarks @ {r['precision'] or 0:.0%} right")
        gain = rows[-1]["top1"] - rows[-2]["top1"] if len(rows) > 1 else 0.0
        o.say("\n" + (f"the last quarter of your data still added {gain:+.0%} to the best-guess rate: keep filing and it keeps improving"
                      if gain >= 0.01 else "the curve has flattened: more filed bookmarks of the same kind will not help much"))
        return o.finish()
    r = learn.text_effect(bms, args.seeds or 1, progress=progress)
    o.set(**r)
    if not r["pages"]:
        o.say("no page text is archived yet, so there is nothing to compare: run `archive`, then `learn --text-effect`")
        return o.finish()
    if r["with"].get("mode") != "stacked":
        o.say("not enough filed bookmarks for the comparison (it needs at least 150)")
        return o.finish()
    a, b = r["without"], r["with"]
    o.say(f"{r['pages']} of {len(bms)} bookmarks have archived page text\n")
    o.say(f"  titles and addresses only:  best guess right {a['top1']:.0%}, among top 3 {a['top3']:.0%}")
    o.say(f"  plus the page text:         best guess right {b['top1']:.0%}, among top 3 {b['top3']:.0%}   ({b['top1'] - a['top1']:+.0%})")
    for tgt in ("0.8", "0.9"):
        x, y = a["targets"][tgt]["overall"], b["targets"][tgt]["overall"]
        o.say(f"  answering at {float(tgt):.0%}: {x['share']:.0%} @ {x['precision'] or 0:.0%}  ->  {y['share']:.0%} @ {y['precision'] or 0:.0%}")
    return o.finish()


def cmd_restructure(args, S):
    lib, o = _lib(args), Out(args)
    bms = lib.all()
    model = model_for(lib, args, bms)
    props = restructure.analyse(bms, max_size=args.max_size, min_size=args.min_size, max_depth=args.max_depth,
                                model=model)
    for p in props[: args.show]:
        o.say(f"[{p['kind']}] {p['folder']}: {p['reason']} -> {len(p['moves'])} bookmark(s)")
        for i, f in list(p["moves"].items())[:3]:
            o.say(f"      {next(b.title for b in bms if b.id == i)[:50]} => {f}")
    plan = restructure.to_plan(props)
    o.set(proposals=[{k: v for k, v in p.items() if k != "moves"} | {"moves": len(p["moves"])} for p in props])
    o.say(f"{len(plan)} bookmarks would move")
    if args.plan_out:
        _write_plan(args.plan_out, plan, o)
    elif args.apply:
        _apply_plan_cmd(lib, bms, plan, "restructure", o, 99, True, prune_empty=True)
    else:
        o.say("dry run; add --apply, or --plan-out FILE to review first")
    o.finish()


def cmd_ai_batch(args, S):
    lib, o = _lib(args), Out(args)
    bms = lib.all()
    batch = categorize.ai_batch(bms, categorize.load_taxonomy(S("taxonomy")), model_for(lib, args, bms),
                                limit=args.limit, only_unfiled=not args.refile, rules=rules_for(lib),
                                min_conf=S("min_confidence"), exclude=exclusions_for(lib, bms))
    if args.out:
        Path(args.out).write_text(json.dumps(batch, indent=2, ensure_ascii=False), encoding="utf-8")
        o.say(f"{len(batch['items'])} items needing AI judgement -> {args.out}")
        o.set(items=len(batch["items"]), out=args.out)
    else:
        o.set(**batch)
        o.say(json.dumps(batch, indent=2, ensure_ascii=False))
    o.finish()


def cmd_apply_plan(args, S):
    lib, o = _lib(args), Out(args)
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    _apply_plan_cmd(lib, lib.all(), plan, "apply-plan", o, args.max_depth if args.max_depth is not None else S("max_depth"), args.apply)
    o.finish()


def cmd_tag(args, S):
    lib, o = _lib(args), Out(args)
    b = lib.get(args.id)
    if not b:
        sys.exit(f"no bookmark with id {args.id}")
    gone = set(clean_tags(args.remove))
    b.tags = clean_tags([t for t in b.tags if t not in gone] + args.add)
    if args.folder is not None:
        b.folder = categorize.resolve_folder(args.folder, lib.folders() | lib.used_folders()) if args.folder else ""
    if args.title:
        b.title = args.title
    with lib.op("tag"):
        lib.upsert([b])
    o.set(**_row(b, tags=b.tags))
    o.say(b.id, b.folder, b.tags)
    o.finish()


def cmd_folders(args, S):
    lib, o = _lib(args), Out(args)
    if args.rename:
        old, new = args.rename
        bms = lib.all(snippet=0)
        hit = [b for b in bms if b.folder == old or b.folder.startswith(old + "/")]
        for b in hit:
            b.folder = clean_folder(new + b.folder[len(old):])
        o.say(f"{len(hit)} bookmarks {'moved' if args.apply else 'will move'}: {old} -> {new}")
        o.set(moved=len(hit))
        if args.apply:
            lib.snapshot("pre-rename")
            with lib.op("rename-folder"):
                lib.upsert(hit)
                lib.set_folders({clean_folder(new + f[len(old):]) if f == old or f.startswith(old + "/") else f
                                 for f in lib.folders()} | lib.used_folders())
        return o.finish()
    bms = lib.all(snippet=0)
    used = lib.used_folders()
    if args.prune_empty:
        empty = sorted(lib.folders() - used)
        o.say(f"{len(empty)} empty folders" + ("" if args.apply else " (dry run; add --apply)"))
        for f in empty[: args.show]:
            o.say("  ", f)
        if args.apply:
            with lib.op("prune-empty-folders"):
                lib.set_folders(used)
        o.set(empty=empty)
        return o.finish()
    counts = Counter(b.folder or "(unfiled)" for b in bms)
    for f in sorted(lib.folders() | set(counts)):
        o.say(f"{counts.get(f, 0):>5}  {f}")
    o.set(folders={f: counts.get(f, 0) for f in sorted(lib.folders() | set(counts))})
    o.finish()


def cmd_feedback(args, S):
    """What happened to the suggestions: how often they were accepted, and whether the stated confidence holds up."""
    lib, o = _lib(args), Out(args)
    if args.action == "clear":
        n = len(lib.feedback_rows())
        o.say(f"{n} recorded decisions" + ("" if args.apply else " (dry run; add --apply to forget them)"))
        if args.apply:
            lib.clear_feedback()
        o.set(cleared=n if args.apply else 0)
        return o.finish()
    if args.action == "list":
        rows = lib.feedback_rows(limit=args.limit)
        o.set(decisions=rows)
        for r in rows:
            o.say(f"{r['action']:<9} {r['channel']:<14} {(r['confidence'] or 0):.0%}  {_tail(r['suggested'], 40)}"
                  + ("" if r["action"] == "accepted" else f"  ->  {_tail(r['final'], 40)}"))
        if not rows:
            o.say("no decisions recorded yet: they are collected when you review a plan, use `triage`, save from the extension or the web UI")
        return o.finish()
    sm = feedback.summary(lib)
    o.set(**sm)
    if not sm["total"]:
        o.say("no decisions recorded yet; suggestions are judged when you review a plan (`review`), use `triage`, the extension or the web UI")
        return o.finish()
    acts = sm["by_action"]
    o.say(f"{sm['total']} decisions: " + ", ".join(f"{acts.get(a, 0)} {a}" for a in feedback.ACTIONS)
          + f"  ({sm['explicit']} looked at one by one; only those check the model's confidence)")
    o.say("by where it happened: " + ", ".join(f"{c} {n}" for c, n in sorted(sm["by_channel"].items())))
    if sm["bands"]:
        o.say("\nquoted confidence -> what you actually accepted (look-at-each-one decisions only)")
        for b in sm["bands"]:
            o.say(f"  {b['from']:.0%}-{b['to']:.0%}:  quoted {b['quoted']:.0%}, accepted {b['observed']:.0%}   ({b['accepted']} of {b['decisions']})")
        if sm["explicit"] < 30:
            o.say("  (fewer than 30 decisions: the confidence is not corrected yet)")
        else:
            o.say("  the model's confidence is corrected towards these rates")
    o.finish()


def cmd_rules(args, S):
    from .. import rules as rules_mod

    lib, o = _lib(args), Out(args)
    rules = rules_mod.load(lib.root)
    if args.action == "list":
        o.set(rules=rules)
        for r in rules:
            kind, value = next(iter(r["when"].items()))
            o.say(f"#{r['id']:<3} {kind}={value}  ->  {r['folder']}" + (f"  tags: {', '.join(r['tags'])}" if r["tags"] else ""))
        if not rules:
            o.say("no rules yet; `rules add --host example.com --folder \"Some/Folder\"` or `rules suggest`")
    elif args.action == "add":
        given = [(k, getattr(args, k)) for k in rules_mod.KINDS if getattr(args, k)]
        if len(given) != 1 or not args.folder:
            sys.exit("give exactly one of --host/--url-prefix/--title-regex/--keyword, plus --folder")
        folder = categorize.resolve_folder(args.folder, lib.folders() | lib.used_folders())
        rule = rules_mod.add(rules, given[0][0], given[0][1], folder, clean_tags(args.tags or []))
        rules_mod.save(lib.root, rules)
        o.set(added=rule)
        o.say(f"added rule #{rule['id']}: {given[0][0]}={given[0][1]} -> {folder}")
    elif args.action == "remove":
        keep = [r for r in rules if r["id"] != args.id]
        if len(keep) == len(rules):
            sys.exit(f"no rule #{args.id}")
        rules_mod.save(lib.root, keep)
        o.say(f"removed rule #{args.id}")
    elif args.action == "suggest":
        found = rules_mod.suggest(lib.all(snippet=0), existing=rules)
        o.set(suggestions=found)
        for s in found[: args.show]:
            o.say(f"{s['host']:<32} always filed in  {_tail(s['folder'], 50)}  ({s['filed']} filed, {s['purity']:.0%}); {s['unfiled']} unfiled")
        if args.apply:
            for s in found:
                rules_mod.add(rules, "host", s["host"], s["folder"])
            rules_mod.save(lib.root, rules)
            o.say(f"added {len(found)} rules")
        elif found:
            o.say("add them all with `rules suggest --apply`, or pick with `rules add`")
    elif args.action == "test":
        from ..model import Bookmark

        b = Bookmark(url=args.url, title=args.title or "")
        hit = rules_mod.apply(b, rules, lib.folders() | lib.used_folders())
        o.set(match=hit)
        o.say(f"-> {hit['folder']}  ({hit['reason']})" if hit else "no rule matches")
    o.finish()


def register(add, apply_flag, net_flags):
    sp = add("organize", cmd_organize, "file + tag unfiled bookmarks: learned from your folders, then taxonomy rules")
    sp.add_argument("--taxonomy")
    sp.add_argument("--refile", action="store_true", help="also move already-filed bookmarks")
    sp.add_argument("--no-tags", action="store_true")
    sp.add_argument("--no-learn", action="store_true", help="rules only")
    sp.add_argument("--allow-new-folders", action="store_true",
                    help="let taxonomy rules create folders that do not exist yet (default: only when there is no tree to learn from)")
    sp.add_argument("--precision", "--min-confidence", type=float, dest="min_confidence",
                    help="answer only when at least this likely to be right (default 0.85)")
    sp.add_argument("--coarse", action="store_true", help="also allow answers two levels above the best match")
    sp.add_argument("--retrain", action="store_true", help="discard the cached filing model first")
    sp.add_argument("--explain", action="store_true", help="include the full plan in --json output")
    sp.add_argument("--plan-out")
    apply_flag(sp)

    sp = add("learn", cmd_learn, "train on your existing folders and report how accurate the filing is")
    sp.add_argument("--retrain", action="store_true", help="discard the cached model and train again")
    sp.add_argument("--precision", "--min-confidence", type=float, dest="min_confidence")
    sp.add_argument("--seeds", type=int, default=1, metavar="N",
                    help="repeat the accuracy measurement on N different splits and average (slower; does not touch the cached model)")
    sp.add_argument("--curve", action="store_true", help="show how accuracy grows with the number of filed bookmarks (trains several times)")
    sp.add_argument("--text-effect", dest="text_effect", action="store_true",
                    help="compare accuracy with and without the archived page text (run `archive` first; trains twice)")

    sp = add("restructure", cmd_restructure, "tune the folder tree: split big, fold tiny, flatten deep, merge look-alikes")
    sp.add_argument("--max-size", type=int, default=40)
    sp.add_argument("--min-size", type=int, default=2, help="folders with fewer bookmarks than this are folded (default 2)")
    sp.add_argument("--max-depth", type=int, dest="max_depth", help="also flatten folders deeper than N (off by default)")
    sp.add_argument("--plan-out")
    apply_flag(sp)

    sp = add("ai-batch", cmd_ai_batch, "export bookmarks nothing could place, for the agent to judge")
    sp.add_argument("--taxonomy")
    sp.add_argument("--limit", type=int, default=200)
    sp.add_argument("--refile", action="store_true")
    sp.add_argument("--out")

    sp = add("apply-plan", cmd_apply_plan, "validate + apply a {id: {folder,tags,title,notes,delete}} plan")
    sp.add_argument("plan")
    sp.add_argument("--max-depth", type=int, dest="max_depth")
    apply_flag(sp)

    sp = add("tag", cmd_tag, "edit one bookmark's tags/folder/title")
    sp.add_argument("id")
    sp.add_argument("--add", nargs="*", default=[])
    sp.add_argument("--remove", nargs="*", default=[])
    sp.add_argument("--folder")
    sp.add_argument("--title")

    sp = add("folders", cmd_folders, "list folders; --rename OLD NEW; --prune-empty")
    sp.add_argument("--rename", nargs=2, metavar=("OLD", "NEW"))
    sp.add_argument("--prune-empty", action="store_true")
    apply_flag(sp)

    sp = add("feedback", cmd_feedback, "what happened to the suggestions: accepted, corrected, rejected; does the stated confidence hold up?")
    sp.add_argument("action", nargs="?", choices=["stats", "list", "clear"], default="stats")
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--apply", action="store_true", help="for `clear`: really forget the recorded decisions")

    sp = add("rules", cmd_rules, "your own filing rules: host/url/title/keyword -> folder, applied before the model")
    sp.add_argument("action", choices=["list", "add", "remove", "suggest", "test"])
    sp.add_argument("--host")
    sp.add_argument("--url-prefix", dest="url_prefix")
    sp.add_argument("--title-regex", dest="title_regex")
    sp.add_argument("--keyword")
    sp.add_argument("--folder")
    sp.add_argument("--tags", nargs="*")
    sp.add_argument("--id", type=int)
    sp.add_argument("--url")
    sp.add_argument("--title")
    sp.add_argument("--apply", action="store_true", help="for `suggest`: add all suggested rules")
    sp.add_argument("--show", type=int, default=30)
