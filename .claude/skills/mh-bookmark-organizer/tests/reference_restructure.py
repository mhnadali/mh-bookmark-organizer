"""Reference copy of ``restructure.analyse`` as it was before it was split into small steps and indexed.

Kept only so ``test_refactor_equivalence`` can prove the fast version proposes exactly the same changes.
Do not use it elsewhere; it is O(bookmarks x folders). The one change from the original: it walks the folders in
sorted order, because the original's order came from a set and differed from run to run.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from mhbm.learn import FolderModel
from mhbm.urlnorm import host_of


def _depth(path: str) -> int:
    return path.count("/") + 1 if path else 0


def _norm_name(name: str) -> str:
    """Case/punctuation/plural-insensitive folder key: 'Recipe' == 'recipes' == 'Recipes!'."""
    n = re.sub(r"[^a-z0-9]", "", name.lower())
    if len(n) > 4 and n.endswith("ies"):
        return n[:-3] + "y"
    if len(n) > 4 and re.search(r"(s|x|z|ch|sh)es$", n):
        return n[:-2]
    if len(n) > 3 and n.endswith("s") and not n.endswith(("ss", "ews", "us", "is")):
        return n[:-1]
    return n


def _pretty_host(host: str) -> str:
    labels = host.split(".")
    return (labels[-2] if len(labels) >= 2 else host).title()


def analyse(bms, *, max_size: int = 40, min_size: int = 2, max_depth: int | None = None, min_group: int = 5,
            model: FolderModel | None = None) -> list:
    """Return ordered proposals ``{kind, folder, reason, moves: {id: new_folder}}`` on an evolving working state.

    Flattening deep folders is opt-in (``max_depth``): a deliberately deep tree is the user's structure, not a defect.
    ``min_size`` is the smallest folder worth keeping (2 = only single-bookmark folders are folded).
    """
    state = {b.id: b.folder for b in bms}
    by_id = {b.id: b for b in bms}
    proposals = []

    def commit(kind, folder, reason, moves):
        moves = {i: f for i, f in moves.items() if state[i] != f}
        if moves:
            state.update(moves)
            proposals.append({"kind": kind, "folder": folder, "reason": reason, "moves": moves})

    def members(folder, deep=False):
        return [i for i, f in state.items() if f == folder or (deep and f.startswith(folder + "/"))]

    # 1. flatten anything deeper than max_depth (only when asked for)
    if max_depth:
        commit("flatten", "(deep folders)", f"deeper than {max_depth} levels", {
            i: "/".join(f.split("/")[:max_depth]) for i, f in state.items() if _depth(f) > max_depth})
    limit = max_depth or 12

    # 2. merge look-alike sibling folders (Recipe / Recipes / recipes)
    parents = defaultdict(set)
    for f in sorted(set(state.values())):  # (the original iterated the bare set, so its proposal ORDER varied between runs)
        if f:
            parents[f.rsplit("/", 1)[0] if "/" in f else ""].add(f)
    for kids in parents.values():
        groups = defaultdict(list)
        for k in kids:
            groups[_norm_name(k.rsplit("/", 1)[-1])].append(k)
        for names in (g for g in groups.values() if len(g) > 1):
            target = max(names, key=lambda f: (len(members(f, True)), -len(f)))
            moves = {}
            for other in names:
                if other == target:
                    continue
                for i, f in list(state.items()):
                    if f == other or f.startswith(other + "/"):
                        moves[i] = target + f[len(other):]
            commit("merge-similar", target, "near-identical folder names: " + ", ".join(sorted(names)), moves)

    # 3. split bloated folders by their strongest tags / hosts
    for folder in sorted({f for f in state.values() if f}):
        ids = members(folder)
        if len(ids) <= max_size or _depth(folder) + 1 > limit:
            continue
        stop = set(re.findall(r"[a-z0-9]+", folder.lower()))
        left, moves = set(ids), {}
        while len(left) > 0:
            cands: Counter = Counter()
            for i in left:
                b = by_id[i]
                for t in b.tags:
                    if not (set(re.findall(r"[a-z0-9]+", t)) & stop):
                        cands[("tag", t)] += 1
                h = host_of(b.url)
                if h:
                    cands[("host", h)] += 1
            if not cands:
                break
            (kind, value), n = cands.most_common(1)[0]
            if n < min_group:
                break
            name = value.replace("-", " ").title() if kind == "tag" else _pretty_host(value)
            chosen = [i for i in left if (value in by_id[i].tags if kind == "tag" else host_of(by_id[i].url) == value)]
            for i in chosen:
                moves[i] = f"{folder}/{name}"
            left -= set(chosen)
        if len(moves) >= 0.3 * len(ids):
            commit("split", folder, f"{len(ids)} bookmarks in one folder (limit {max_size})", moves)

    # 4. fold tiny leaf folders into their parent (or into the learned best match for top-level ones).
    #    Decided on the sizes as they are NOW, so a folder that just received a fold is never folded again.
    sizes = Counter(state.values())
    inner = {f.rsplit("/", 1)[0] for f in sizes if "/" in f}
    received: set = set()
    for folder in sorted({f for f in sizes if f}, key=lambda f: -_depth(f)):
        n = sizes[folder]
        if n >= min_size or folder in inner or folder in received:
            continue
        ids = members(folder)
        if "/" in folder:
            parent = folder.rsplit("/", 1)[0]
            received.add(parent)
            commit("fold", folder, f"only {n} bookmark(s)", dict.fromkeys(ids, parent))
        elif model is not None and model.ready:
            moves = {}
            for i in ids:
                p = model.predict(by_id[i], 0.6)
                if p and p["folder"] != folder:
                    moves[i] = p["folder"]
                    received.add(p["folder"])
            commit("fold", folder, f"only {n} bookmark(s); moved to the closest existing folder", moves)
    return proposals


def to_plan(proposals) -> dict:
    """Collapse proposals into a plan keyed by id (later proposals win)."""
    plan = {}
    for p in proposals:
        for i, folder in p["moves"].items():
            plan[i] = {"folder": folder, "reason": f"{p['kind']}: {p['reason']}"}
    return plan
