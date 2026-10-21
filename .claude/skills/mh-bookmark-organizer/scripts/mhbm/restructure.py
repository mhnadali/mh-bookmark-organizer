"""Folder-tree tuning: split bloated folders, fold tiny ones, flatten deep ones, merge look-alike names."""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from .learn import FolderModel
from .urlnorm import host_of


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


class _Working:
    """The folder tree as it would be after the proposals so far (each step judges the result of the previous ones)."""

    def __init__(self, bms):
        self.by_id = {b.id: b for b in bms}
        self.order = {b.id: n for n, b in enumerate(bms)}
        self.state = {b.id: b.folder for b in bms}
        self.by_folder: dict = defaultdict(set)
        for i, f in self.state.items():
            self.by_folder[f].add(i)
        self.proposals: list = []

    def commit(self, kind, folder, reason, moves) -> None:
        moves = {i: f for i, f in moves.items() if self.state[i] != f}
        if not moves:
            return
        for i, f in moves.items():
            self.by_folder[self.state[i]].discard(i)
            self.by_folder[f].add(i)
            self.state[i] = f
        self.proposals.append({"kind": kind, "folder": folder, "reason": reason, "moves": moves})

    def members(self, folder, deep=False) -> list:
        ids = set(self.by_folder.get(folder, ()))
        if deep:
            prefix = folder + "/"
            for f, group in self.by_folder.items():
                if f.startswith(prefix):
                    ids |= group
        return sorted(ids, key=self.order.__getitem__)

    def folders(self) -> list:
        return sorted(f for f, ids in self.by_folder.items() if ids and f)


def _flatten(w: _Working, max_depth: int) -> None:
    w.commit("flatten", "(deep folders)", f"deeper than {max_depth} levels", {
        i: "/".join(f.split("/")[:max_depth]) for i, f in w.state.items() if _depth(f) > max_depth})


def _merge_similar(w: _Working) -> None:
    """Sibling folders whose names differ only by case, punctuation or a plural (Recipe / Recipes / recipes)."""
    parents = defaultdict(set)
    for f in w.folders():
        parents[f.rsplit("/", 1)[0] if "/" in f else ""].add(f)
    for kids in parents.values():
        groups = defaultdict(list)
        for k in kids:
            groups[_norm_name(k.rsplit("/", 1)[-1])].append(k)
        for names in (g for g in groups.values() if len(g) > 1):
            target = max(names, key=lambda f: (len(w.members(f, True)), -len(f)))
            moves = {}
            for other in names:
                if other != target:
                    for i in w.members(other, True):
                        moves[i] = target + w.state[i][len(other):]
            w.commit("merge-similar", target, "near-identical folder names: " + ", ".join(sorted(names)), moves)


def _group_candidates(w: _Working, left, stop) -> Counter:
    """How many of the remaining bookmarks share each tag (not one already in the folder's name) and each host."""
    cands: Counter = Counter()
    for i in left:
        b = w.by_id[i]
        for t in b.tags:
            if not (set(re.findall(r"[a-z0-9]+", t)) & stop):
                cands[("tag", t)] += 1
        h = host_of(b.url)
        if h:
            cands[("host", h)] += 1
    return cands


def _split_moves(w: _Working, folder, ids, min_group) -> dict:
    """Peel groups off a bloated folder, largest shared tag or host first, until no group of ``min_group`` is left."""
    stop = set(re.findall(r"[a-z0-9]+", folder.lower()))
    left, moves = set(ids), {}
    while left:
        cands = _group_candidates(w, left, stop)
        if not cands:
            break
        (kind, value), n = cands.most_common(1)[0]
        if n < min_group:
            break
        name = value.replace("-", " ").title() if kind == "tag" else _pretty_host(value)
        chosen = [i for i in left if (value in w.by_id[i].tags if kind == "tag" else host_of(w.by_id[i].url) == value)]
        for i in chosen:
            moves[i] = f"{folder}/{name}"
        left -= set(chosen)
    return moves


def _split_bloated(w: _Working, max_size: int, min_group: int, limit: int) -> None:
    for folder in w.folders():
        ids = w.members(folder)
        if len(ids) <= max_size or _depth(folder) + 1 > limit:
            continue
        moves = _split_moves(w, folder, ids, min_group)
        if len(moves) >= 0.3 * len(ids):
            w.commit("split", folder, f"{len(ids)} bookmarks in one folder (limit {max_size})", moves)


def _fold_tiny(w: _Working, min_size: int, model) -> None:
    """Fold tiny leaf folders into their parent (or, for top-level ones, into the learned best match).

    Decided on the sizes as they are NOW, so a folder that just received a fold is never folded again."""
    sizes = Counter(w.state.values())
    inner = {f.rsplit("/", 1)[0] for f in sizes if "/" in f}
    received: set = set()
    for folder in sorted({f for f in sizes if f}, key=lambda f: -_depth(f)):
        n = sizes[folder]
        if n >= min_size or folder in inner or folder in received:
            continue
        ids = w.members(folder)
        if "/" in folder:
            parent = folder.rsplit("/", 1)[0]
            received.add(parent)
            w.commit("fold", folder, f"only {n} bookmark(s)", dict.fromkeys(ids, parent))
        elif model is not None and model.ready:
            moves = {}
            for i in ids:
                p = model.predict(w.by_id[i], 0.6)
                if p and p["folder"] != folder:
                    moves[i] = p["folder"]
                    received.add(p["folder"])
            w.commit("fold", folder, f"only {n} bookmark(s); moved to the closest existing folder", moves)


def analyse(bms, *, max_size: int = 40, min_size: int = 2, max_depth: int | None = None, min_group: int = 5,
            model: FolderModel | None = None) -> list:
    """Return ordered proposals ``{kind, folder, reason, moves: {id: new_folder}}`` on an evolving working state.

    Flattening deep folders is opt-in (``max_depth``): a deliberately deep tree is the user's structure, not a defect.
    ``min_size`` is the smallest folder worth keeping (2 = only single-bookmark folders are folded).
    """
    w = _Working(bms)
    if max_depth:
        _flatten(w, max_depth)
    _merge_similar(w)
    _split_bloated(w, max_size, min_group, max_depth or 12)
    _fold_tiny(w, min_size, model)
    return w.proposals


def to_plan(proposals) -> dict:
    """Collapse proposals into a plan keyed by id (later proposals win)."""
    plan = {}
    for p in proposals:
        for i, folder in p["moves"].items():
            plan[i] = {"folder": folder, "reason": f"{p['kind']}: {p['reason']}"}
    return plan
