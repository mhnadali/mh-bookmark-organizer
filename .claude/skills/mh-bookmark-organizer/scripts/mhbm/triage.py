"""Triage: decide once for a whole group of unfiled bookmarks instead of one by one.

Bookmarks saved together, or from the same site, usually end up in the same folder (in a real library about 70% of a
bookmark's saving-session mates share its folder). So the unfiled bookmarks are grouped into *clusters* and each cluster
gets one suggestion: the model's folder probabilities averaged over its members. A person then accepts it for all,
picks one of the alternatives, types a folder, or sets members aside. Every decision is recorded (see ``feedback``) and,
in the interactive command, fed back into the model straight away so the next clusters are suggested with the folders
you have just chosen in mind.

Grouping is by simple, explainable links, applied in this order and never beyond ``MAX_CLUSTER`` members:
same site and same sitting, same site and same best guess, same sitting and same best guess, and overlapping guesses
within a sitting.
"""
from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from . import learn
from .model import is_unfiled
from .urlnorm import host_of

MAX_CLUSTER = 30
MIN_TOP = 0.15          # a best guess weaker than this is noise, not a reason to group two bookmarks
OVERLAP = 0.25          # two bookmarks whose guesses overlap this much (sum of the smaller probabilities) are related
OVERLAP_PAIRWISE_MAX = 40


def site_of(url: str) -> str:
    """The site a bookmark is from: its host without ``www``. Sub-domains are different sites on purpose
    (``docs.example.com`` and ``mail.example.com`` rarely belong in the same folder)."""
    return host_of(url)


@dataclass
class Cluster:
    members: list
    folder: str = ""
    support: float = 0.0           # averaged probability of the suggested folder over the members
    agreement: float = 0.0         # share of members whose own best guess is the suggested folder
    alternatives: list = field(default_factory=list)   # [(folder, probability)], best first, without the suggestion
    kind: str = "single"           # site | sitting | mixed | single
    source: str = "triage"
    reason: str = ""
    level: str = "exact"

    @property
    def id(self) -> str:
        return hashlib.sha1("|".join(sorted(b.id for b in self.members)).encode()).hexdigest()[:10]

    def to_json(self, limit: int = 12) -> dict:
        return {"id": self.id, "kind": self.kind, "size": len(self.members), "folder": self.folder, "support": round(self.support, 3),
                "agreement": round(self.agreement, 3), "alternatives": [[f, round(p, 3)] for f, p in self.alternatives], "reason": self.reason,
                "source": self.source, "members": [{"id": b.id, "title": b.title, "url": b.url} for b in self.members[:limit]],
                "more": max(0, len(self.members) - limit)}


class _Groups:
    """Union-find that refuses a merge which would make a cluster bigger than ``MAX_CLUSTER``."""

    def __init__(self, n):
        self.parent = list(range(n))
        self.size = [1] * n

    def find(self, i):
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a, b) -> bool:
        a, b = self.find(a), self.find(b)
        if a == b:
            return True
        if self.size[a] + self.size[b] > MAX_CLUSTER:
            return False
        if self.size[a] < self.size[b]:
            a, b = b, a
        self.parent[b] = a
        self.size[a] += self.size[b]
        return True


def _distributions(model, bms, exclude):
    if model is None or not getattr(model, "ready", False):
        return {b.id: {} for b in bms}
    return {b.id: model.distribution(b, exclude.get(b.id, ())) for b in bms}


def _top(d):
    return max(d.items(), key=lambda kv: (kv[1], kv[0])) if d else ("", 0.0)


def build(bms, model=None, *, rules=None, known_folders=(), exclude=None, only_unfiled=True) -> list:
    """Clusters for the unfiled bookmarks in ``bms``, the most worthwhile decisions first.

    Bookmarks a user rule files are grouped by the rule; everything else by the links described in the module docstring.
    Clusters of one are returned too (``kind == "single"``), after the real clusters.
    """
    from . import rules as rules_mod

    exclude = exclude or {}
    todo = [b for b in bms if not only_unfiled or is_unfiled(b)]
    if not todo:
        return []
    clusters: list = []
    by_rule: dict = defaultdict(list)
    rest = []
    for b in todo:
        hit = rules_mod.apply(b, rules, known_folders) if rules else None
        if hit:
            by_rule[(hit["folder"], hit["reason"])].append(b)
        else:
            rest.append(b)
    for (folder, reason), members in by_rule.items():
        for chunk in _chunks(members, MAX_CLUSTER):
            clusters.append(Cluster(members=chunk, folder=folder, support=1.0, agreement=1.0, kind="rule", source="rules", reason=reason))
    if rest:
        clusters += _cluster(rest, _distributions(model, rest, exclude))
    clusters.sort(key=lambda c: (-(len(c.members) > 1), -len(c.members) * (c.support or 0.01), c.id))
    return clusters


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _cluster(bms, dists) -> list:
    sid = learn.session_ids(bms)
    tops = [_top(dists[b.id]) for b in bms]
    groups = _Groups(len(bms))

    def link(keys):
        buckets: dict = defaultdict(list)
        for i, k in enumerate(keys):
            if k is not None:
                buckets[k].append(i)
        for members in buckets.values():
            members.sort(key=lambda i: (bms[i].added, bms[i].id))
            for a, b in zip(members, members[1:]):
                groups.union(a, b)

    domain = [site_of(b.url) or None for b in bms]
    link([(sid[i], domain[i]) if domain[i] else None for i in range(len(bms))])          # same site, same sitting
    # same site and the same best guess, even on different days. Linking by site alone merges whole topics into one
    # giant, useless group, because many sites serve several of your folders (measured on a real library).
    # (bookmarks the model has no real guess for are linked by site alone: there is nothing else to go on)
    link([(domain[i], tops[i][0] if tops[i][1] >= MIN_TOP else None) if domain[i] else None for i in range(len(bms))])
    link([(sid[i], tops[i][0]) if tops[i][0] and tops[i][1] >= MIN_TOP else None for i in range(len(bms))])  # same sitting, same guess
    by_sitting: dict = defaultdict(list)
    for i, s in enumerate(sid):
        by_sitting[s].append(i)
    for members in by_sitting.values():
        if 1 < len(members) <= OVERLAP_PAIRWISE_MAX:
            for x in range(len(members)):
                for y in range(x + 1, len(members)):
                    a, b = members[x], members[y]
                    da, db = dists[bms[a].id], dists[bms[b].id]
                    if da and db and sum(min(p, db.get(f, 0.0)) for f, p in da.items()) >= OVERLAP:
                        groups.union(a, b)
    out: dict = defaultdict(list)
    for i in range(len(bms)):
        out[groups.find(i)].append(i)
    clusters = []
    for idxs in out.values():
        members = [bms[i] for i in sorted(idxs, key=lambda i: (bms[i].added, bms[i].id))]
        clusters.append(_summarise(members, [dists[b.id] for b in members], {sid[i] for i in idxs}, {domain[i] for i in idxs}))
    return clusters


def _summarise(members, dists, sittings, domains) -> Cluster:
    n = len(members)
    pooled: Counter = Counter()
    for d in dists:
        for f, p in d.items():
            pooled[f] += p / n
    ranked = sorted(pooled.items(), key=lambda kv: (-kv[1], kv[0]))
    folder, support = (ranked[0] if ranked else ("", 0.0))
    agreement = sum(1 for d in dists if d and _top(d)[0] == folder) / n if folder else 0.0
    if n == 1:
        kind = "single"
    elif len(domains) == 1 and None not in domains:
        kind = "site"
    elif len(sittings) == 1:
        kind = "sitting"
    else:
        kind = "mixed"
    why = {"site": "from the same site", "sitting": "saved together", "mixed": "related", "single": ""}[kind]
    reason = (f"{n} bookmarks {why}; " if n > 1 else "") + (f"{agreement:.0%} of them lean the same way" if folder and n > 1 else "")
    return Cluster(members=members, folder=folder, support=support, agreement=agreement, alternatives=ranked[1:5], kind=kind,
                   reason=reason.strip("; "))


def to_plan(clusters, *, min_support: float = 0.0, min_size: int = 1) -> dict:
    """A reviewable plan ``{id: entry}`` with every member of the chosen clusters carrying the cluster's suggestion."""
    plan: dict = {}
    for c in clusters:
        if not c.folder or c.support < min_support or len(c.members) < min_size:
            continue
        for b in c.members:
            plan[b.id] = {"folder": c.folder, "source": c.source, "level": c.level, "confidence": round(c.support, 3), "cluster": c.id,
                          "reason": c.reason or "triage suggestion", "alternatives": [[f, round(p, 3)] for f, p in c.alternatives[:3]]}
    return plan


def decide(lib, cluster: Cluster, folder: str, *, exclude_ids=(), channel: str = "triage", action: str = "file", known=None) -> dict:
    """Apply one decision for a cluster; returns ``{"filed": n, "left": n, "action": ...}``.

    ``action`` is ``file`` (move the members to ``folder``), ``reject`` (turn the suggestion down, move nothing) or ``skip``
    (no decision, nothing recorded). Members in ``exclude_ids`` are set aside and not judged.
    """
    from . import categorize, feedback

    members = [b for b in cluster.members if b.id not in set(exclude_ids)]
    if action == "skip" or not members:
        return {"filed": 0, "left": len(cluster.members), "action": "skip", "bookmarks": []}
    if action == "reject":
        for b in members:
            if cluster.folder:
                feedback.settle(lib, b, b.folder, channel, suggested=cluster.folder, confidence=cluster.support, level=cluster.level,
                                source=cluster.source, action="rejected")
        return {"filed": 0, "left": len(cluster.members), "action": "reject", "bookmarks": []}
    target = categorize.resolve_folder(folder, known if known is not None else lib.folders() | lib.used_folders())
    for b in members:
        b.folder = target
    lib.upsert(members)
    lib.add_folders(lib.used_folders())
    for b in members:
        if cluster.folder:
            feedback.settle(lib, b, target, channel, suggested=cluster.folder, confidence=cluster.support, level=cluster.level, source=cluster.source)
    return {"filed": len(members), "left": len(cluster.members) - len(members), "action": "file", "folder": target, "bookmarks": members}

