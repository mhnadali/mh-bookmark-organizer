"""Command group: triage (decide once for a whole group of unfiled bookmarks)."""
from __future__ import annotations

import sys

from .. import feedback
from .. import triage as triage_mod
from ..model import clean_folder, is_unfiled
from .common import Out, _apply_plan_cmd, _lib, _tail, _write_plan, exclusions_for, model_for, rules_for

REFRESH_EVERY = 3  # re-cluster the remainder after this many decisions (and after every learning step)


def _show(cluster, number: int, total: int, members: int = 8) -> None:
    print(f"\n[{number}/{total}] {cluster.reason or 'one bookmark'}")
    for i, b in enumerate(cluster.members[:members], 1):
        print(f"   {i:>2}. {(b.title or b.url)[:70]}   {b.url[:50]}")
    if len(cluster.members) > members:
        print(f"       ... and {len(cluster.members) - members} more")
    if cluster.folder:
        print(f"   suggestion: {cluster.folder}   (support {cluster.support:.0%}, {cluster.agreement:.0%} of members agree)")
    else:
        print("   no suggestion (nothing similar is filed yet)")
    for k, (f, p) in enumerate(cluster.alternatives[:4], 1):
        print(f"   {k}) {f}   {p:.0%}")


def _prompt(cluster) -> str:
    keys = ("Enter=accept  " if cluster.folder else "") + (f"1-{min(4, len(cluster.alternatives))}=alternative  " if cluster.alternatives else "")
    return f"   [{keys}t=type a folder  x=set members aside  r=reject  s=skip  q=quit] > "


def _members_from(text: str, cluster) -> list:
    out = []
    for tok in text.replace(",", " ").split():
        if tok.isdigit() and 1 <= int(tok) <= len(cluster.members):
            out.append(cluster.members[int(tok) - 1].id)
    return out


def _typed_folder(prompt: str) -> str:
    return clean_folder(input(prompt), strip=False)


def _pick_folder(answer: str, cluster, ask: str):
    """The folder an answer stands for: Enter = the suggestion, a number = that alternative, ``t`` = typed. '' if unusable."""
    if answer == "" and cluster.folder:
        return cluster.folder
    if answer.isdigit() and 1 <= int(answer) <= len(cluster.alternatives):
        return cluster.alternatives[int(answer) - 1][0]
    if answer == "t":
        return _typed_folder(ask)
    return ""


def _choose(cluster):
    """Ask what to do with a cluster: ``None`` to stop, else ``(action, folder, members_to_set_aside)``."""
    while True:
        low = input(_prompt(cluster)).strip().lower()
        if low == "q":
            return None
        if low in ("r", "s"):
            return ("reject" if low == "r" else "skip"), cluster.folder, []
        if low == "x":
            aside = _members_from(input("   numbers of the bookmarks to set aside: "), cluster)
            if not aside:
                continue
            sub = input(_prompt(cluster).replace("x=set members aside  ", "")).strip().lower()
            folder = _pick_folder(sub, cluster, "   folder: ")
            if folder:
                return "file", folder, aside
            continue
        folder = _pick_folder(low, cluster, "   folder (existing or new, like Work/Docs): ")
        if folder:
            return "file", folder, []


def _next_clusters(lib, model, rules, skipped, singles) -> list:
    current = [b for b in lib.all() if is_unfiled(b) and b.id not in skipped]
    known = lib.folders() | lib.used_folders()
    clusters = triage_mod.build(current, model, rules=rules, known_folders=known, exclude=exclusions_for(lib, current))
    return clusters if singles else [c for c in clusters if len(c.members) > 1]


def _interactive(lib, model, bms, rules, o, args) -> dict:
    if not sys.stdin.isatty():
        sys.exit("--interactive needs a terminal; use --plan-out and `review` instead")
    done = {"filed": 0, "decisions": 0, "rejected": 0, "skipped": 0}
    skipped: set = set()
    clusters: list = []
    steps = 0
    while True:
        if not clusters:
            clusters = _next_clusters(lib, model, rules, skipped, args.singles)
            if not clusters:
                return done
        c = clusters.pop(0)
        c.members = [b for b in c.members if b.id not in skipped]
        if not c.members:
            continue
        _show(c, done["decisions"] + 1, done["decisions"] + 1 + len(clusters))
        choice = _choose(c)
        if choice is None:
            return done
        action, folder, aside = choice
        skipped.update(aside)
        with lib.op("triage"):
            res = triage_mod.decide(lib, c, folder, exclude_ids=aside, action=action, known=lib.folders() | lib.used_folders())
        if res["action"] == "file":
            done["filed"] += res["filed"]
            done["decisions"] += 1
            model.add_examples(res["bookmarks"])   # the next suggestions know what was just decided
            clusters, steps = [], 0                # and the rest are grouped again with that knowledge
            print(f"   -> {res['filed']} filed in {_tail(res['folder'], 50)}")
            continue
        skipped.update(b.id for b in c.members)
        if res["action"] == "reject":
            done["rejected"] += 1
            done["decisions"] += 1
            print("   -> suggestion turned down; it will not be offered for these again")
        else:
            done["skipped"] += 1
        steps += 1
        if steps >= REFRESH_EVERY:
            clusters, steps = [], 0


def cmd_triage(args, S):
    lib, o = _lib(args), Out(args)
    bms = lib.all()
    model = model_for(lib, args, bms)
    rules = rules_for(lib)
    if args.interactive:
        done = _interactive(lib, model, bms, rules, o, args)
        o.say(f"\n{done['filed']} bookmarks filed in {done['decisions']} decisions, {done['rejected']} suggestions turned down, {done['skipped']} skipped")
        o.set(**done)
        return o.finish()
    known = lib.folders() | lib.used_folders()
    clusters = triage_mod.build(bms, model, rules=rules, known_folders=known, exclude=exclusions_for(lib, bms))
    unfiled = sum(len(c.members) for c in clusters)
    multi = [c for c in clusters if len(c.members) > 1]
    o.set(unfiled=unfiled, decisions=len(clusters), clusters=[c.to_json() for c in clusters[: args.show]], multi_clusters=len(multi))
    if not clusters:
        o.say("nothing to triage: no unfiled bookmarks")
        return o.finish()
    o.say(f"{unfiled} unfiled bookmarks -> {len(clusters)} decisions ({len(multi)} clusters, {len(clusters) - len(multi)} single)")
    for n, c in enumerate(clusters[: args.show], 1):
        head = f"{n:>3}. {len(c.members):>2} x {(c.members[0].title or c.members[0].url)[:50]}"
        o.say(head + (f"  ->  {_tail(c.folder, 46)}  (support {c.support:.0%}, {c.agreement:.0%} agree)" if c.folder else "  ->  (no suggestion)"))
        if c.reason:
            o.say(f"       {c.reason}")
    plan = triage_mod.to_plan(clusters, min_support=args.min_support, min_size=args.min_size)
    if args.plan_out:
        feedback.issue(lib, plan, bms)
        _write_plan(args.plan_out, plan, o)
    elif args.apply:
        _apply_plan_cmd(lib, bms, plan, "triage", o, 99, True)
    else:
        o.say("\nDecide interactively with `triage --interactive`, or write a plan to review: `triage --plan-out FILE` then `review FILE`")
    o.finish()


def register(add, apply_flag, net_flags):
    sp = add("triage", cmd_triage, "group unfiled bookmarks (same site / saved together) and decide once per group")
    sp.add_argument("--interactive", action="store_true", help="walk through the groups in the terminal")
    sp.add_argument("--singles", action="store_true", help="with --interactive: also go through groups of one")
    sp.add_argument("--min-support", type=float, default=0.3, help="--plan-out/--apply: only groups whose suggestion has at least this support")
    sp.add_argument("--min-size", type=int, default=1, help="--plan-out/--apply: only groups of at least this many")
    sp.add_argument("--plan-out")
    apply_flag(sp)


