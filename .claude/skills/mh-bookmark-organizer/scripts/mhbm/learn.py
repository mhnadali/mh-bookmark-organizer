"""Learn how *you* file bookmarks.

Several independent signals each suggest folders for a bookmark:

* two TF-IDF nearest-neighbour models (title words, URL words, host, character n-grams, tags, page text),
* a naive-Bayes text model,
* time proximity (bookmarks saved around the same time tend to be filed together),
* folder-name affinity and per-folder statistics (size, same-site share, nearest-in-time member).

A small logistic-regression **stacker** learns how much to trust each signal for *your* library. Its inputs are
out-of-fold predictions made with whole saving sessions held out, so it is trained and measured on exactly the kind of
bookmark you want filed: one saved at a different time from the examples it learns from. The stacker's probabilities are
then **calibrated** (isotonic regression), which turns "score 0.7" into "right about 70% of the time", and the model can
answer at the depth it is sure about: the exact folder, or its parent when siblings look alike.

Everything runs locally with the standard library; the trained model is cached next to the library.
"""
from __future__ import annotations

import bisect
import hashlib
import heapq
import json
import math
import operator
import os
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from .model import Bookmark, is_unfiled
from .textnorm import char_ngrams, words
from .urlnorm import host_of

_np: Any = None
try:  # optional speed-up (pip install .[fast]); everything works without it
    import numpy as _numpy

    _np = _numpy
except ImportError:  # pragma: no cover - depends on the environment
    pass

MODEL_VERSION = 5
MIN_SUPPORT = 1
LEVELS = {0: "exact", 1: "parent", 2: "grandparent"}
STACK_EPOCHS = 20
MIN_STEPS = 600    # optimisation steps the combiner always gets, so a small library's fit does not depend on batch order
MAX_EPOCHS = 150
MAX_NODE_SHARE = 0.2  # an answer higher up the tree must narrow things down: at most this share of the library
SESSION_GAP = 1800  # seconds between saves that still count as one sitting
_CFG_MAIN = {"host": 1, "hp": 1, "cng": 0.15}
_CFG_TEXT = {"host": 0.3, "dom": 0.3, "hl": 0.1, "hp": 0, "cng": 0.15}
_CFG_NB = {"host": 1, "hp": 1}
_NAMES = ("knn", "knn_text", "nb", "t1d", "t3d", "aff", "cent", "cent_text")
_STRENGTH = ("knn", "knn_text", "cent", "cent_text", "t1d")
_FUSE = ("knn", "knn_text", "cent", "cent_text", "nb", "t3d")  # scorers whose average ranks the anchors that candidates are expanded from
EXPAND = True          # also consider the neighbours (siblings, parent, children) of the best candidates
ANCHORS = 2            # how many top candidates to expand
EXPAND_EACH = 3        # at most this many siblings / children added per anchor
TOPN_TIME = 8          # the two time scorers contribute more candidates: sittings are strong evidence
MAX_CANDIDATES = 28
OOF_MAX_ITEMS = 6000  # at most about this many bookmarks are judged out-of-sample (whole sittings are sampled)
PARALLEL_MIN_DOCS = 600  # fewer filed bookmarks than this and starting worker processes costs more than it saves
AFFINITY_MAX_FOLDERS = 60  # folder-name words used by more folders than this are ignored by the name-overlap scorer
COMMON_CAP = 400  # a word in more documents than this is never used to look neighbours up
FEATURE_CACHE_MAX = 6000  # memoise features only for libraries up to this many filed bookmarks (memory grows with it)
POOL_OWN = 0.35        # sittings: weight of a bookmark's own evidence; the rest is the average over its sitting-mates
POOL_K = 12            # ...taking at most this many sitting-mates, the nearest in time


# ------------------------------------------------------------------ features
def features(b: Bookmark, cfg=None) -> dict:
    cfg = cfg or {}
    f: Any = Counter()  # holds float weights
    host = host_of(b.url)
    u = urlsplit(b.url)
    if host:
        f["h:" + host] += cfg.get("host", 3)
        labels = host.split(".")
        if len(labels) >= 2:
            f["d:" + ".".join(labels[-2:])] += cfg.get("dom", 1.5)
        for label in labels[:-1]:
            if len(label) > 2:
                f["hl:" + label] += cfg.get("hl", 0.5)
        segs = [s for s in u.path.split("/") if s]
        if segs and cfg.get("hp", 0):
            f["hp:" + host + "/" + segs[0]] += cfg["hp"]
    for w in words(b.title):
        f[w] += cfg.get("title", 1)
    for w in words(u.path.replace("/", " ").replace("-", " ").replace("_", " ")):
        f["u:" + w] += cfg.get("path", 0.5)
    if cfg.get("cng", 0):
        for g in char_ngrams(b.title):
            f["c:" + g] += cfg["cng"]
    for t in b.tags:
        f["t:" + t] += 2
    for w, n in Counter(words(b.content[:1500])).items():
        f[w] += min(n, 3) * 0.3
    return f


class FeatureCache:
    """Remembers ``features(b, cfg)`` while a model trains: the same bookmarks are featurised in every cross-validation
    fold, so without this each is done thirty times. It is switched off (and emptied) when training ends, so a long-lived
    process answering for new bookmarks never grows it. Cached results are read-only for the scorers."""

    def __init__(self):
        self.active = True
        self.store: dict = {}

    def __call__(self, b: Bookmark, cfg, key: str):
        if not self.active:
            return features(b, cfg)
        hit = self.store.get((id(b), key))
        if hit is None:
            hit = self.store[(id(b), key)] = (b, features(b, cfg))  # holding b keeps id(b) from being reused
        return hit[1]

    def close(self) -> None:
        self.active = False
        self.store.clear()


_PLAIN = FeatureCache()
_PLAIN.active = False  # a cache that never caches: used when none is given


def _by_score(item):
    """Sort key for ``(name, score)`` pairs: by score, ties broken by name so results never depend on dict or hash order."""
    return (item[1], item[0])


def _norm(d: dict) -> dict:
    s = sum(d.values()) or 1.0
    return {k: v / s for k, v in d.items()}


# ----------------------------------------------------------------- scorers
class _Prep:
    """TF-IDF preparation shared by the neighbour and centroid scorers (same documents, same features): the document
    frequencies, the inverse document frequencies and every document's unit-length weighted vector, computed once."""

    def __init__(self, train, cfg, feats=_PLAIN, key="main"):
        self.cfg, self.feats, self.key = cfg, feats, key
        vecs = [feats(b, cfg, key) for b in train]
        df: Counter = Counter()
        for v in vecs:
            df.update(v.keys())
        n = len(vecs)
        self.idf = {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}
        # tokens in >5% of documents say almost nothing about a folder but dominate lookup cost (and a fixed ceiling keeps a
        # query's cost from growing with the library)
        ceiling = min(max(50, 0.05 * n), COMMON_CAP)
        self.common = {t for t, c in df.items() if c > ceiling}
        idf, mul = self.idf, operator.mul
        self.unit: list = []
        for v in vecs:
            w = {t: x * idf[t] for t, x in v.items()}
            vals = list(w.values())
            norm = math.sqrt(sum(map(mul, vals, vals))) or 1.0
            self.unit.append({t: x / norm for t, x in w.items()})


class KNNScorer:
    def __init__(self, train, cfg, k=10, feats=_PLAIN, key="main", prep=None):
        self.cfg, self.k, self.docs = cfg, k, train
        self.feats, self.key = feats, key
        prep = prep or _Prep(train, cfg, feats, key)
        self.idf, self.common = prep.idf, prep.common
        self.index: dict = defaultdict(list)
        for i, w in enumerate(prep.unit):
            for t, x in w.items():
                self.index[t].append((i, x))
        self.position = {d.id: i for i, d in enumerate(train)}
        self._memo: Optional[tuple] = None

    def neighbors(self, b):
        # scoring a bookmark and then explaining the answer ask for the same neighbours: remember the latest, keyed by what
        # the answer depends on (not by object identity), so an edited bookmark is never answered from a stale entry
        sig = (b.id, b.url, b.title, b.added, tuple(b.tags), len(b.content))
        if self._memo is not None and self._memo[0] == sig:
            return self._memo[1]
        found = self._neighbors(b)
        self._memo = (sig, found)
        return found

    def _neighbors(self, b):
        feats = {t: x for t, x in self.feats(b, self.cfg, self.key).items() if t in self.idf}
        w = {t: x * self.idf[t] for t, x in feats.items() if t not in self.common}
        norm = math.sqrt(sum(x * x for x in w.values())) or 1.0
        acc: dict = defaultdict(float)
        for t, x in w.items():
            xq = x / norm
            for i, y in self.index[t]:
                acc[i] += xq * y
        acc.pop(self.position.get(b.id, -1), None)  # a bookmark is not its own neighbour
        top = heapq.nlargest(self.k, zip(acc.values(), acc))
        return [(self.docs[i], s) for s, i in top]

    def scores(self, b) -> dict:
        votes: Counter = Counter()
        for d, s in self.neighbors(b):
            votes[d.folder] += s
        return votes


class NBScorer:
    """Multinomial naive Bayes, length-tempered, returning a posterior over folders."""

    def __init__(self, train, cfg, alpha=0.15, feats=_PLAIN, key="nb"):
        self.cfg, self.alpha = cfg, alpha
        self.feats, self.key = feats, key
        self.cf: dict = defaultdict(Counter)
        self.tot: Counter = Counter()
        self.prior = Counter(b.folder for b in train)
        self.post: dict = defaultdict(list)  # token -> [(folder, count)]
        vocab = set()
        for b in train:
            for t, x in feats(b, cfg, key).items():
                self.cf[b.folder][t] += x
                self.tot[b.folder] += x
                vocab.add(t)
        self.V, self.N = len(vocab), len(train)
        a = alpha
        self.la = la = math.log(a)
        # everything that does not depend on the query is worked out once
        self.log_prior = {f: math.log(n / self.N) for f, n in self.prior.items()}
        self.log_room = {f: la - math.log(self.tot[f] + a * self.V) for f in self.prior}
        for f, c in self.cf.items():
            for t, x in c.items():
                self.post[t].append((f, math.log(x + a) - la))

    def scores(self, b) -> dict:
        feats = self.feats(b, self.cfg, self.key)
        sx = sum(feats.values())
        lp = {f: lg + sx * self.log_room[f] for f, lg in self.log_prior.items()}
        for t, x in feats.items():
            for f, gain in self.post.get(t, ()):
                lp[f] += x * gain
        m = max(lp.values())
        temper = max(1, len(feats))
        return _norm({f: math.exp((v - m) / temper) for f, v in lp.items()})


class TimeScorer:
    """Kernel vote over the bookmarks saved closest in time."""

    def __init__(self, train, tau, n):
        items = sorted((b.added, b.folder, b.id) for b in train if b.added)
        self.items, self.times = items, [t for t, _, _ in items]
        self.tau, self.n = float(tau), n

    def scores(self, b) -> dict:
        if not b.added or not self.items:
            return {}
        i = bisect.bisect_left(self.times, b.added)
        votes: Any = Counter()
        for t, f, bid in self.items[max(0, i - self.n): i + self.n]:
            if bid != b.id:
                votes[f] += math.exp(-abs(t - b.added) / self.tau)
        return votes


class AffinityScorer:
    """Overlap between a bookmark's words and the words in the names of a folder's path."""

    def __init__(self, train):
        self.fw = {f: {w for part in f.split("/")[-3:] for w in words(part)} for f in sorted({b.folder for b in train})}
        df = Counter(w for s in self.fw.values() for w in s)
        n = len(self.fw)
        self.idf = {w: math.log((n + 1) / (c + 1)) + 1 for w, c in df.items()}
        self.folders_with: dict = defaultdict(list)  # word -> folders whose path names contain it (inverted: no pass over all folders)
        for f, ws in self.fw.items():
            for w in ws:
                self.folders_with[w].append(f)
        for w in [w for w, fs in self.folders_with.items() if len(fs) > AFFINITY_MAX_FOLDERS]:
            del self.folders_with[w]  # a word in this many folder names (say "misc") singles out none of them

    def scores(self, b) -> dict:
        bw = set(words(b.title)) | set(words(urlsplit(b.url).netloc.replace(".", " ")))
        out: dict = {}
        for w in sorted(bw):  # words in a fixed order: float sums must not depend on hash seeds
            for f in self.folders_with.get(w, ()):
                out[f] = out.get(f, 0.0) + self.idf[w]
        return {f: out[f] for f in sorted(out) if out[f]}


class CentroidScorer:
    """Cosine similarity to each folder's TF-IDF centroid: judges a bookmark against the whole folder, not single members."""

    def __init__(self, train, cfg, feats=_PLAIN, key="main", prep=None):
        self.cfg = cfg
        self.feats, self.key = feats, key
        prep = prep or _Prep(train, cfg, feats, key)
        self.idf, self.common = prep.idf, prep.common
        cent: dict = defaultdict(lambda: defaultdict(float))
        for b, w in zip(train, prep.unit):
            c = cent[b.folder]
            for t, x in w.items():
                c[t] += x
        self.index: dict = defaultdict(list)
        for f, c in cent.items():
            vals = list(c.values())
            norm = math.sqrt(sum(map(operator.mul, vals, vals))) or 1.0
            for t, x in c.items():
                self.index[t].append((f, x / norm))

    def scores(self, b) -> dict:
        feats = {t: x for t, x in self.feats(b, self.cfg, self.key).items() if t in self.idf}
        w = {t: x * self.idf[t] for t, x in feats.items() if t not in self.common}
        norm = math.sqrt(sum(x * x for x in w.values())) or 1.0
        acc: dict = defaultdict(float)
        for t, x in w.items():
            xq = x / norm
            for f, y in self.index[t]:
                acc[f] += xq * y
        return acc


class FolderStats:
    """Per-folder side information: size, same-site share and distance in time to the nearest member."""

    def __init__(self, train):
        self.size = Counter(b.folder for b in train)
        self.host: dict = defaultdict(Counter)
        self.dom: dict = defaultdict(Counter)
        self.times: dict = defaultdict(list)
        for b in train:
            h = host_of(b.url)
            self.host[b.folder][h] += 1
            self.dom[b.folder][".".join(h.split(".")[-2:])] += 1
            if b.added:
                self.times[b.folder].append(b.added)
        for ts in self.times.values():
            ts.sort()

    def row(self, b, folder) -> list:
        h = host_of(b.url)
        d = ".".join(h.split(".")[-2:])
        n = max(self.size[folder], 1)
        ts = self.times.get(folder, [])
        dt = 1e9
        if b.added and ts:
            i = bisect.bisect_left(ts, b.added)
            dt = min(abs(ts[j] - b.added) for j in (i - 1, i) if 0 <= j < len(ts))
        near = [0, 0]  # members saved within +-3 days / +-30 days: is the folder "active" around that time?
        if b.added and ts:
            for slot, win in enumerate((3 * 86400, 30 * 86400)):
                near[slot] = bisect.bisect_right(ts, b.added + win) - bisect.bisect_left(ts, b.added - win)
        return [math.log(1 + self.size[folder]), self.host[folder][h] / n, 1.0 if self.host[folder][h] else 0.0,
                self.dom[folder][d] / n, math.log1p(min(dt, 1e8)) / 18.0, 1.0 if dt < 1800 else 0.0,
                1.0 if dt < 86400 else 0.0, math.log1p(near[0]), near[0] / n, math.log1p(near[1]), near[1] / n]


class Base:
    """All base scorers fitted on one training set."""

    def __init__(self, train, feats=_PLAIN):
        self.train = train
        main, text = _Prep(train, _CFG_MAIN, feats, "main"), _Prep(train, _CFG_TEXT, feats, "text")
        self.knn = KNNScorer(train, _CFG_MAIN, 10, feats, "main", main)
        self.knn_text = KNNScorer(train, _CFG_TEXT, 10, feats, "text", text)
        self.nb = NBScorer(train, _CFG_NB, feats=feats, key="nb")
        self.t1d = TimeScorer(train, 86400, 20)
        self.t3d = TimeScorer(train, 259200, 30)
        self.aff = AffinityScorer(train)
        self.cent = CentroidScorer(train, _CFG_MAIN, feats, "main", main)
        self.cent_text = CentroidScorer(train, _CFG_TEXT, feats, "text", text)
        self.stats = FolderStats(train)
        self.size = Counter(b.folder for b in train)
        self.siblings: dict = defaultdict(list)   # parent path -> folders with bookmarks directly in them
        self.children: dict = defaultdict(list)   # folder -> sub-folders with bookmarks directly in them
        self.up: dict = {}                        # folder -> nearest ancestor that holds bookmarks itself
        for f in sorted(self.size, key=lambda x: (-self.size[x], x)):
            self.siblings[_parent(f)].append(f)
        for f in self.size:
            node = _parent(f)
            while node and node not in self.size:
                node = _parent(node)
            if node:
                self.up[f] = node
                self.children[node].append(f)

    def distributions(self, b):
        """``(normalised distributions, raw scores)``; the raw ones keep how *strong* the evidence is."""
        raw = {"knn": dict(self.knn.scores(b)), "knn_text": dict(self.knn_text.scores(b)), "nb": self.nb.scores(b),
               "t1d": dict(self.t1d.scores(b)), "t3d": dict(self.t3d.scores(b)), "aff": dict(self.aff.scores(b)),
               "cent": dict(self.cent.scores(b)), "cent_text": dict(self.cent_text.scores(b))}
        return {n: (v if n == "nb" else _norm(v)) for n, v in raw.items()}, raw

    def candidate_rows(self, b, topn=6):
        """``(distributions, folders, feature rows)`` for the candidates suggested by any scorer.

        Candidates are the best folders of every scorer plus, because a deep tree is full of near-misses, the siblings,
        parent and children of the best few (the right folder is usually one of them when it is not the first guess).
        """
        dist, raw = self.distributions(b)
        totals = [math.log1p(5.0 * sum(raw[n].values())) for n in _STRENGTH]
        cands: set = set()
        for n in _NAMES:
            cands.update(f for f, _ in heapq.nlargest(TOPN_TIME if n in ("t1d", "t3d") else topn, dist[n].items(), key=_by_score))
        fused = {f: sum(dist[n].get(f, 0.0) for n in _FUSE) / len(_FUSE) for f in cands}
        anchors = sorted(cands, key=lambda f: (-fused[f], f))[:ANCHORS]
        if EXPAND:
            for a in anchors:
                kin = [f for f in self.siblings.get(_parent(a), ()) if f != a][:EXPAND_EACH]
                kin += self.children.get(a, [])[:EXPAND_EACH]
                if a in self.up:
                    kin.append(self.up[a])
                cands.update(kin)
            if len(cands) > MAX_CANDIDATES:  # keep the strongest; expansions rank by their own evidence
                cands = set(sorted(cands, key=lambda f: (-fused.get(f, 0.0), -self.size[f], f))[:MAX_CANDIDATES])
        folders = sorted(cands)
        tops = {n: (max(dist[n].items(), key=_by_score)[0] if dist[n] else None) for n in _NAMES}
        lead = anchors[0] if anchors else ""
        lead_parts = lead.split("/") if lead else []
        rows = []
        for f in folders:
            v = [dist[n].get(f, 0.0) for n in _NAMES]
            v += [x ** 0.5 for x in v]
            v += [1.0 if tops[n] == f else 0.0 for n in _NAMES]
            v += [min(raw[n].get(f, 0.0), 3.0) for n in _STRENGTH]  # absolute similarity, not just the share
            v += totals                                              # how much evidence exists at all for this bookmark
            v += self.stats.row(b, f)
            v += self._relation(f, lead, lead_parts, fused.get(f, 0.0))
            rows.append(v)
        return dist, folders, rows

    @staticmethod
    def _relation(f, lead, lead_parts, fused) -> list:
        """How ``f`` relates to the best guess in the tree: itself, sibling, ancestor, descendant, shared path."""
        if not lead:
            return [0.0, 0.0, 0.0, 0.0, 0.0, fused]
        parts = f.split("/")
        common = 0
        for x, y in zip(parts, lead_parts):
            if x != y:
                break
            common += 1
        return [1.0 if f == lead else 0.0,
                1.0 if f != lead and _parent(f) == _parent(lead) else 0.0,
                1.0 if f != lead and _is_under(f, lead) else 0.0,
                1.0 if f != lead and _is_under(lead, f) else 0.0,
                common / max(len(parts), len(lead_parts)), fused]


# ------------------------------------------------------- logistic regression
def _shuffled(order: list, rnd: random.Random) -> None:
    """Shuffle ``order`` in place with one ``random()`` per element and a C-level sort (``rnd.shuffle`` is a Python loop)."""
    keys = [rnd.random() for _ in order]
    order.sort(key=keys.__getitem__)


def numpy_active() -> bool:
    """True when the optional numpy speed-up is in use (installed, and not switched off with ``MHBM_NO_NUMPY=1``)."""
    return _np is not None and not os.environ.get("MHBM_NO_NUMPY")


def fit_logistic(rows, labels, epochs=None, lr=0.2, l2=1e-4, batch=256, seed=0):
    """Pointwise logistic regression: Adam with a cosine learning-rate decay. Returns ``(mu, sd, weights, bias)``.

    The same algorithm runs in pure Python or, when numpy is available, vectorised (about 10-30 times faster); both draw
    the same mini-batches, so they agree to rounding error.
    """
    if not epochs:  # a small problem has few mini-batches per pass: give it enough passes for a stable fit
        per_pass = max(1, (len(rows) + batch - 1) // batch)
        epochs = min(MAX_EPOCHS, max(STACK_EPOCHS, -(-MIN_STEPS // per_pass)))
    if numpy_active() and len(rows) * len(rows[0]) >= 2000:
        return _fit_logistic_numpy(rows, labels, epochs, lr, l2, batch, seed)
    n, d = len(rows), len(rows[0])
    cols = list(zip(*rows))
    mu = [sum(c) / n for c in cols]
    sd = [math.sqrt(sum((x - m) ** 2 for x in c) / n) + 1e-6 for c, m in zip(cols, mu)]
    Z = [[(x - m) / s for x, m, s in zip(r, mu, sd)] for r in rows]
    w, bias = [0.0] * d, 0.0
    m, v = [0.0] * d, [0.0] * d
    mb = vb = 0.0
    order = list(range(n))
    rnd = random.Random(seed)
    t = 0
    mul = operator.mul
    total = epochs * ((n + batch - 1) // batch)
    for _ in range(epochs):
        _shuffled(order, rnd)
        for s in range(0, n, batch):
            idx = order[s: s + batch]
            g, gb = [0.0] * d, 0.0
            for i in idx:
                z = Z[i]
                a = sum(map(mul, w, z)) + bias
                p = 1.0 / (1.0 + math.exp(-a)) if a > -30 else 0.0
                e = p - labels[i]
                gb += e
                g = [gj + e * zj for gj, zj in zip(g, z)]
            t += 1
            c1, c2 = 1 - 0.9 ** t, 1 - 0.999 ** t
            inv = 1.0 / len(idx)
            step = lr * 0.5 * (1 + math.cos(math.pi * t / total))
            for j in range(d):
                gj = g[j] * inv + l2 * w[j]
                m[j] = 0.9 * m[j] + 0.1 * gj
                v[j] = 0.999 * v[j] + 0.001 * gj * gj
                w[j] -= step * (m[j] / c1) / (math.sqrt(v[j] / c2) + 1e-8)
            gbj = gb * inv
            mb = 0.9 * mb + 0.1 * gbj
            vb = 0.999 * vb + 0.001 * gbj * gbj
            bias -= step * (mb / c1) / (math.sqrt(vb / c2) + 1e-8)
    return mu, sd, w, bias


def _fit_logistic_numpy(rows, labels, epochs, lr, l2, batch, seed):
    np = _np
    X = np.asarray(rows, dtype=np.float64)
    y = np.asarray(labels, dtype=np.float64)
    n, d = X.shape
    mu = X.mean(axis=0)
    sd = np.sqrt(((X - mu) ** 2).mean(axis=0)) + 1e-6
    Z = (X - mu) / sd
    w, m, v = np.zeros(d), np.zeros(d), np.zeros(d)
    bias = mb = vb = 0.0
    order = list(range(n))
    rnd = random.Random(seed)
    t = 0
    total = epochs * ((n + batch - 1) // batch)
    for _ in range(epochs):
        _shuffled(order, rnd)
        perm = np.asarray(order)
        for s in range(0, n, batch):
            idx = perm[s: s + batch]
            Zb = Z[idx]
            a = Zb @ w + bias
            p = np.where(a > -30, 1.0 / (1.0 + np.exp(-np.maximum(a, -30.0))), 0.0)
            e = p - y[idx]
            t += 1
            c1, c2 = 1 - 0.9 ** t, 1 - 0.999 ** t
            inv = 1.0 / len(idx)
            step = lr * 0.5 * (1 + math.cos(math.pi * t / total))
            g = (e @ Zb) * inv + l2 * w
            m = 0.9 * m + 0.1 * g
            v = 0.999 * v + 0.001 * g * g
            w = w - step * (m / c1) / (np.sqrt(v / c2) + 1e-8)
            gbj = float(e.sum()) * inv
            mb = 0.9 * mb + 0.1 * gbj
            vb = 0.999 * vb + 0.001 * gbj * gbj
            bias -= step * (mb / c1) / (math.sqrt(vb / c2) + 1e-8)
    return mu.tolist(), sd.tolist(), w.tolist(), float(bias)


def logit(model, row) -> float:
    mu, sd, w, bias = model
    return sum(wj * (x - m) / s for wj, x, m, s in zip(w, row, mu, sd)) + bias


def softmax(zs) -> list:
    m = max(zs)
    e = [math.exp(z - m) for z in zs]
    s = sum(e)
    return [x / s for x in e]


def folder_probs(zs) -> list:
    """Per-folder probabilities from the stacker's log-odds.

    Each candidate's own probability is sigmoid(z); they are only rescaled when they add up to more than 1. Unlike a
    softmax this stays low when *nothing* looks like a good match, so a bookmark without evidence gets no confident answer.
    """
    s = [1.0 / (1.0 + math.exp(-z)) if z > -30 else 0.0 for z in zs]
    total = max(1.0, sum(s))
    return [x / total for x in s]


def isotonic(xs, ys) -> dict:
    """Pool-adjacent-violators fit: ``{"xmin": lowest x seen, "pts": [(x_upper_edge, calibrated_value), ...]}``.

    Equal x values are pooled first: a lookup at a tied x must see one value, not whichever block came first.
    """
    grouped: dict = defaultdict(lambda: [0.0, 0.0])
    for x, y in zip(xs, ys):
        g = grouped[x]
        g[0] += float(y)
        g[1] += 1.0
    blocks: list = []  # [mean, weight, x_max]
    for x in sorted(grouped):
        total, weight = grouped[x]
        blocks.append([total / weight, weight, x])
        while len(blocks) > 1 and blocks[-2][0] >= blocks[-1][0]:
            v2, w2, x2 = blocks.pop()
            v1, w1, _ = blocks.pop()
            blocks.append([(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2, x2])
    return {"xmin": min(grouped) if grouped else 0.0, "pts": [(b[2], b[0]) for b in blocks]}


def calibrated(cal, x) -> float:
    """Look ``x`` up in an isotonic fit. Below anything the fit has ever seen the value shrinks towards 0: no evidence, no confidence."""
    pts = cal.get("pts") if cal else None
    if not pts:
        return 0.0
    xmin = cal.get("xmin", 0.0)
    if x < xmin:
        return pts[0][1] * (x / xmin) if xmin > 0 else 0.0
    edges = [p[0] for p in pts]
    return pts[min(bisect.bisect_left(edges, x), len(pts) - 1)][1]


# ------------------------------------------------------------------ sessions
def session_ids(bms, gap=SESSION_GAP) -> list:
    """Group bookmarks saved close together; those without a date each stand alone."""
    order = sorted((i for i, b in enumerate(bms) if b.added), key=lambda i: bms[i].added)
    sid = [-1] * len(bms)
    cur, last = -1, None
    for i in order:
        if last is None or bms[i].added - last > gap:
            cur += 1
        sid[i] = cur
        last = bms[i].added
    for i in range(len(bms)):
        if sid[i] < 0:
            cur += 1
            sid[i] = cur
    return sid


def _fold_of(bms, k, seed) -> list:
    sid = session_ids(bms)
    uniq = sorted(set(sid))
    random.Random(seed).shuffle(uniq)
    fold = {s: j % k for j, s in enumerate(uniq)}
    return [fold[s] for s in sid]


def fingerprint(train) -> str:
    h = hashlib.sha1(f"v{MODEL_VERSION}".encode())
    for b in sorted(train, key=lambda x: x.id):
        h.update(f"{b.id}|{b.folder}|{b.added}|{len(b.tags)}\n".encode())
    return h.hexdigest()[:16]


def _parent(folder: str) -> str:
    return folder.rsplit("/", 1)[0] if "/" in folder else ""


def _ancestors(folder):
    parts = folder.split("/")
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def _is_under(node, folder) -> bool:
    return folder == node or folder.startswith(node + "/")


def _node_masses(folders, probs) -> dict:
    mass: Counter = Counter()
    for f, p in zip(folders, probs):
        for node in _ancestors(f):
            mass[node] += p
    return mass


def pool_probs(dists, times, own=POOL_OWN, k=POOL_K) -> list:
    """Collective inference over one saving session: blend each member's folder probabilities with the average of its
    nearest sitting-mates'. Things saved together tend to be filed together; ``dists`` are ``{folder: p}`` dicts."""
    out = []
    for i, d in enumerate(dists):
        others = sorted((j for j in range(len(dists)) if j != i), key=lambda j: (abs(times[j] - times[i]), j))[:k]
        if not others:
            out.append(dict(d))
            continue
        pooled: Counter = Counter()
        for j in others:
            for f, p in dists[j].items():
                pooled[f] += p
        mix = {f: own * d.get(f, 0.0) + (1 - own) * pooled.get(f, 0.0) / len(others) for f in sorted(set(d) | set(pooled))}
        out.append(mix)
    return out


class Outcomes:
    """What really happened to past suggestions, used to correct the model's stated confidence.

    The model quotes a calibrated probability, measured on cross-validated data. The user's accept / reject decisions are
    ground truth about how those probabilities hold up in actual use. Confidence is cut into bands of 0.05 from 0.5 up; each
    band starts from the belief "what was quoted is right" worth ``prior`` imaginary decisions and moves with real ones
    (a Beta posterior mean), so a handful of outcomes cannot swing it but hundreds can. The bands are then made monotone
    (higher confidence never means a lower rate) and read with linear interpolation.
    """

    LOW, WIDTH, BANDS = 0.5, 0.05, 10

    def __init__(self, records, prior: float = 12.0):
        self.n = 0
        right = [0.0] * self.BANDS
        total = [0.0] * self.BANDS
        for conf, accepted in records:
            conf = float(conf)
            if conf < self.LOW:
                continue
            i = min(int((conf - self.LOW) / self.WIDTH), self.BANDS - 1)
            total[i] += 1.0
            right[i] += 1.0 if accepted else 0.0
            self.n += 1
        self.centers = [self.LOW + self.WIDTH * (i + 0.5) for i in range(self.BANDS)]
        blocks: list = []  # [mean, weight, first_band, last_band]
        for i in range(self.BANDS):
            blocks.append([(prior * self.centers[i] + right[i]) / (prior + total[i]), prior + total[i], i, i])
            while len(blocks) > 1 and blocks[-2][0] >= blocks[-1][0]:
                v2, w2, _, hi = blocks.pop()
                v1, w1, lo, _ = blocks.pop()
                blocks.append([(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2, lo, hi])
        self.values = [0.0] * self.BANDS
        for mean, _, lo, hi in blocks:
            for i in range(lo, hi + 1):
                self.values[i] = mean

    def adjust(self, conf: float) -> float:
        if self.n == 0:
            return conf
        if conf < self.centers[0]:  # below what is ever suggested: shrink along with the lowest band, never lift
            return conf * min(1.0, self.values[0] / self.centers[0])
        if conf >= self.centers[-1]:
            return self.values[-1]
        pos = (conf - self.centers[0]) / self.WIDTH
        i = int(pos)
        frac = pos - i
        return self.values[i] * (1 - frac) + self.values[i + 1] * frac


# --------------------------------------------------------------------- model
class FolderModel:
    """Predict the folder (or the parent folder) for an unfiled bookmark.

    ``FolderModel(bms)`` trains from scratch (a few seconds per thousand bookmarks); pass ``cache=path`` to keep the
    trained stacker and calibration on disk and reuse them while the filed bookmarks stay the same (``cache_write=False``: use
    the cache when it fits but never write it, for read-only libraries).
    """

    def __init__(self, bms, *, min_support: int = MIN_SUPPORT, k: int = 5, min_docs: int = 10, stack_min: int = 150,
                 cache=None, progress=None, seed: int = 1, train_fraction: float = 1.0, cache_write: bool = True):
        train = [b for b in bms if not is_unfiled(b)]
        counts = Counter(b.folder for b in train)
        self.docs = [b for b in train if counts[b.folder] >= min_support]
        self.folders = {b.folder for b in self.docs}
        self.ready = len(self.docs) >= min_docs and len(self.folders) >= 2
        self.mode = "off"
        self.train_fraction = train_fraction  # < 1 only for ``curve``: learn from part of the data to see how accuracy grows
        self.report: dict = {}
        self.k = 7
        self._stack: Optional[tuple] = None
        self._cal: dict = {}
        self.outcomes: Optional[Any] = None  # real accept/reject history, see ``Outcomes``
        self._feats: FeatureCache = _PLAIN
        self.base: Optional[Base] = None
        self.node_share: dict = {}
        if not self.ready:
            return
        under: Counter = Counter()
        for b in self.docs:
            for node in _ancestors(b.folder):
                under[node] += 1
        self.node_share = {node: n / len(self.docs) for node, n in under.items()}
        say = progress or (lambda msg: None)
        fp = fingerprint(self.docs)
        loaded = self._load(cache, fp)
        say("fitting scorers")
        self._feats = FeatureCache() if len(self.docs) <= FEATURE_CACHE_MAX else _PLAIN
        try:
            self.base = Base(self.docs, self._feats)
            self.knn = self.base.knn  # kept for explanations
            if loaded:
                return
            if len(self.docs) < stack_min:
                self.mode = "blend"
                self.report = {"mode": "blend", "training": len(self.docs), "folders": len(self.folders),
                               "note": f"fewer than {stack_min} filed bookmarks: using a fixed blend of scorers, uncalibrated"}
                return
            self.mode = "stacked"
            if not self._train_stacker(min_support, k, seed, say):
                self.mode = "blend"      # nothing could be judged on held-out data (e.g. every folder holds one bookmark)
                self.report = {"mode": "blend", "training": len(self.docs), "folders": len(self.folders),
                               "note": "no folder has enough bookmarks to be judged on held-out data: using a fixed blend of scorers, uncalibrated"}
                return
            if cache_write:
                self._save(cache, fp)
        finally:
            self._feats.close()  # from here on bookmarks are featurised on demand, never remembered

    # ------------------------------------------------------------ training
    def _oof_items(self, min_support, k, seed, say) -> list:
        """Held-out predictions (whole sessions held out): ``[(truth, folders, rows, session, saved_at)]``.

        The folds are independent, so on a machine with several cores (and a library big enough to be worth it) they are
        computed in parallel worker processes; the answer is the same either way.
        """
        docs = self.docs
        fold = _fold_of(docs, k, seed)
        sid = session_ids(docs)
        jobs = []
        for f in range(k):
            tr = [b for b, x in zip(docs, fold) if x != f]
            if self.train_fraction < 1.0:  # keep a share of whole sittings, so neighbours stay together
                rnd = random.Random(seed + 99 + f)
                tr_sid = {s: rnd.random() for s in sorted({s for s, x in zip(sid, fold) if x != f})}
                tr = [b for b, s, x in zip(docs, sid, fold) if x != f and tr_sid[s] < self.train_fraction]
            counts = Counter(b.folder for b in tr)
            tr = [b for b in tr if counts[b.folder] >= min_support]
            if tr:
                held = [(b, s) for b, x, s in zip(docs, fold, sid) if x == f]
                if len(docs) > OOF_MAX_ITEMS:  # a huge library needs a few thousand honest judgements, not all of them
                    rnd = random.Random(seed + 77 + f)
                    keep = {s: rnd.random() < OOF_MAX_ITEMS / len(docs) for s in sorted({s for _, s in held})}
                    held = [(b, s) for b, s in held if keep[s]]
                jobs.append((tr, held))
        say(f"cross-validating {len(jobs)} folds")
        items = []
        for part in _run_folds(jobs, worker_count(len(docs), len(jobs)), self._feats):
            items.extend(part)
        return items

    def _train_stacker(self, min_support, k, seed, say) -> bool:
        """Fit the combiner and the calibration. False when there was nothing to learn them from (the caller falls back to the blend)."""
        items = self._oof_items(min_support, k, seed, say)
        if not items:
            return False
        say("training the combiner")
        X = [r for it in items for r in it[2]]
        y = [1.0 if f == it[0] else 0.0 for it in items for f in it[1]]
        self._stack = fit_logistic(X, y)
        # nested pass: probabilities for each item from a stacker that never saw its session
        sids = sorted({it[3] for it in items})
        random.Random(seed + 10).shuffle(sids)
        sfold = {s: j % k for j, s in enumerate(sids)}
        nested = []
        for f in range(k):
            tr = [it for it in items if sfold[it[3]] != f]
            te = [it for it in items if sfold[it[3]] == f]
            if not tr or not te:
                continue
            model = fit_logistic([r for it in tr for r in it[2]],
                                 [1.0 if fo == it[0] else 0.0 for it in tr for fo in it[1]])
            for truth, folders, rows, s, added in te:
                nested.append((truth, folders, folder_probs([logit(model, r) for r in rows]), s, added))
        if not nested:
            return False
        say("calibrating")
        self._calibrate(nested, seed, k)
        return True

    def add_examples(self, bms) -> int:
        """Learn from bookmarks that have just been filed, without retraining the combiner or the calibration.

        Only the scorers are refitted (a second or two for a few thousand bookmarks): the freshly filed bookmarks become
        neighbours, same-site and same-time evidence for the ones still waiting. Returns how many were added.
        """
        if not self.ready:
            return 0
        have = {b.id for b in self.docs}
        new = list({b.id: b for b in bms if not is_unfiled(b) and b.id not in have}.values())
        if not new:
            return 0
        self.docs = self.docs + new
        self.folders = {b.folder for b in self.docs}
        under: Counter = Counter()
        for b in self.docs:
            for node in _ancestors(b.folder):
                under[node] += 1
        self.node_share = {node: n / len(self.docs) for node, n in under.items()}
        self.base = Base(self.docs)
        self.knn = self.base.knn
        return len(new)

    def set_outcomes(self, records) -> None:
        """Feed it the user's accept / reject history: ``[(confidence quoted, accepted?)]``. Empty clears it."""
        records = list(records)
        self.outcomes = Outcomes(records) if records else None

    def _base(self) -> Base:
        assert self.base is not None, "model is not ready"
        return self.base

    def _node_options(self, folders, probs, max_up=2):
        """Candidate answers for one bookmark: the best folder and its ancestors, with raw probabilities."""
        top = folders[max(range(len(probs)), key=probs.__getitem__)]
        mass = _node_masses(folders, probs)
        parts = top.split("/")
        opts = []
        for up in range(0, min(max_up, len(parts) - 1) + 1):
            node = "/".join(parts[: len(parts) - up])
            if up and self.node_share.get(node, 0.0) > MAX_NODE_SHARE:
                continue  # "somewhere under Start" says nothing: it holds most of the library
            opts.append((up, node, mass[node]))
        return top, opts

    def _calibrate(self, nested, seed, k):
        """Fit the calibration maps and measure honestly (calibrate on other sessions, evaluate on held-out ones).

        The report also says how much better the *ranking* gets when bookmarks saved together are judged together
        (``top1_pooled``): that is what ``triage`` uses. It does not change which answers are confident enough to apply.
        """
        def entry(truth, folders, probs):
            top, opts = self._node_options(folders, probs)
            return [(up, raw, _is_under(node, truth)) for up, node, raw in opts], top

        data = [entry(t, f, p) + (t, s) for t, f, p, s, _ in nested]   # (opts, top, truth, session)

        def fit(rows):
            return {up: isotonic([o[1] for d in rows for o in d[0] if o[0] == up],
                                 [o[2] for d in rows for o in d[0] if o[0] == up]) for up in range(3)}

        self._cal = fit(data)
        sids = sorted({d[3] for d in data})
        random.Random(seed + 20).shuffle(sids)
        sfold = {s: j % k for j, s in enumerate(sids)}
        cals = {f: fit([d for d in data if sfold[d[3]] != f]) for f in range(k)}
        n = len(data)
        top1 = sum(1 for d in data if d[1] == d[2]) / n
        top3 = sum(1 for t, folders, probs, _s, _a in nested
                   if any(folders[i] == t for i in sorted(range(len(probs)), key=lambda i: -probs[i])[:3])) / n
        by_session: dict = defaultdict(list)
        for i, it in enumerate(nested):
            by_session[it[3]].append(i)
        hit1 = hit3 = 0
        for idxs in by_session.values():
            dists = [dict(zip(nested[i][1], nested[i][2])) for i in idxs]
            mixed = pool_probs(dists, [nested[i][4] for i in idxs]) if len(idxs) > 1 else dists
            for i, d in zip(idxs, mixed):
                order = sorted(d, key=lambda f: -d[f])[:3]
                hit1 += bool(order) and order[0] == nested[i][0]
                hit3 += nested[i][0] in order
        report = {"mode": "stacked", "training": len(self.docs), "folders": len(self.folders), "evaluated": n,
                  "top1": round(top1, 3), "top3": round(top3, 3),
                  "top1_pooled": round(hit1 / n, 3), "top3_pooled": round(hit3 / n, 3), "targets": {}}
        for target in (0.7, 0.8, 0.9):
            tiers = Counter()
            right = Counter()
            for opts, _top, _truth, sid in data:
                cal = cals[sfold[sid]]
                for up, raw, correct in opts:
                    if calibrated(cal[up], raw) >= target:
                        tiers[up] += 1
                        right[up] += correct
                        break
            report["targets"][str(target)] = {
                LEVELS[up]: {"share": round(tiers[up] / n, 3), "precision": round(right[up] / tiers[up], 3) if tiers[up] else None}
                for up in range(3)}
            tot = sum(tiers.values())
            report["targets"][str(target)]["overall"] = {"share": round(tot / n, 3),
                                                         "precision": round(sum(right.values()) / tot, 3) if tot else None}
        self.report = report

    # ---------------------------------------------------------- persistence
    def _save(self, cache, fp):
        if not cache:
            return
        mu, sd, w, bias = self._stack

        Path(cache).write_text(json.dumps({
            "v": MODEL_VERSION, "fingerprint": fp, "mode": self.mode, "stack": {"mu": mu, "sd": sd, "w": w, "b": bias},
            "cal": {str(u): c for u, c in self._cal.items()}, "report": self.report}), encoding="utf-8")

    def _load(self, cache, fp) -> bool:
        if not cache or not Path(cache).exists():
            return False

        try:
            data = json.loads(Path(cache).read_text(encoding="utf-8"))
            if data.get("v") != MODEL_VERSION or data.get("fingerprint") != fp:
                return False
            s = data["stack"]
            self._stack = (s["mu"], s["sd"], s["w"], s["b"])
            self._cal = {int(u): {"xmin": c["xmin"], "pts": [tuple(p) for p in c["pts"]]} for u, c in data["cal"].items()}
            self.report, self.mode = data["report"], data["mode"]
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    # ------------------------------------------------------------ prediction
    def _scored(self, b: Bookmark, exclude=()):
        """``(folders, probabilities)`` for ``b`` from the stacked model, minus folders the user has already turned down."""
        _, folders, rows = self._base().candidate_rows(b)
        zs = [logit(self._stack, r) for r in rows]
        if exclude:
            keep = [i for i, f in enumerate(folders) if f not in exclude]
            folders, zs = [folders[i] for i in keep], [zs[i] for i in keep]
        return folders, (folder_probs(zs) if zs else [])

    def rank(self, b: Bookmark, n: int = 5, exclude=()) -> list:
        """Best leaf folders with their (uncalibrated) probability, for display and for the agent."""
        if not self.ready:
            return []
        if self.mode == "stacked":
            folders, probs = self._scored(b, exclude)
            return sorted(zip(folders, probs), key=lambda x: -x[1])[:n]
        dist, _ = self._base().distributions(b)
        blend: Counter = Counter()
        for name, w in (("knn", 1), ("knn_text", 1), ("nb", 1), ("t1d", 1), ("t3d", 0.5)):
            for f, v in dist[name].items():
                if f not in exclude:
                    blend[f] += w * v
        tot = sum(blend.values()) or 1.0
        return [(f, v / tot) for f, v in blend.most_common(n)]

    def _decide(self, b, folders, probs, min_conf, max_up, cal):
        if not folders:
            return None
        top, opts = self._node_options(folders, probs, max_up)
        alts = sorted(zip(folders, probs), key=lambda x: -x[1])[:3]
        for up, node, raw in opts:  # deepest first
            conf = calibrated(cal.get(up), raw)
            if self.outcomes is not None:
                conf = self.outcomes.adjust(conf)
            if conf >= min_conf:
                return self._answer(b, node, LEVELS[up], conf, top, alts)
        return None

    def predict(self, b: Bookmark, min_conf: float = 0.85, *, max_up: int = 1, exclude=()):
        """Answer for ``b`` on its own, or None. ``min_conf`` is the target probability that the answer is right.

        Returns ``{folder, level, confidence, leaf, alternatives, tags, why}`` where ``level`` is ``exact``/``parent``/
        ``grandparent``: the model answers one or two levels up when it is sure of the area but not the exact folder.
        ``exclude`` lists folders that must not be suggested (ones the user has already rejected for this bookmark).
        """
        if not self.ready:
            return None
        if self.mode == "stacked":
            folders, probs = self._scored(b, exclude)
            return self._decide(b, folders, probs, min_conf, max_up, self._cal)
        # small library: fixed blend, answer only when the neighbours clearly agree
        nb = [(d, s) for d, s in self.knn.neighbors(b) if d.folder not in exclude]
        if not nb:
            return None
        votes: Counter = Counter()
        for d, s in nb:
            votes[d.folder] += s
        folder, best = votes.most_common(1)[0]
        conf = best / (sum(votes.values()) or 1)
        agree = [(d, s) for d, s in nb if d.folder == folder]
        top_sim = nb[0][1]
        if conf < 0.6 or not (top_sim >= 0.5 or (len(agree) >= 2 and top_sim >= 0.2)):
            return None
        return self._answer(b, folder, "exact", round(conf, 2), folder, [(folder, conf)])

    def predict_many(self, bms, min_conf: float = 0.85, *, max_up: int = 1, exclude=None) -> dict:
        """``{id: answer or None}`` for a batch; ``exclude`` maps a bookmark id to folders already rejected for it."""
        exclude = exclude or {}
        return {b.id: self.predict(b, min_conf, max_up=max_up, exclude=exclude.get(b.id, ())) for b in bms}

    def distribution(self, b: Bookmark, exclude=()) -> dict:
        """``{folder: probability}`` for ``b`` (uncalibrated; the input to sitting-wide judgement in ``triage``)."""
        if not self.ready:
            return {}
        return dict(self.rank(b, 10**6, exclude))

    def _answer(self, b, node, level, conf, leaf, alts):
        near = [(d, s) for d, s in self.knn.neighbors(b) if _is_under(node, d.folder)]
        tag_w: Counter = Counter()
        for d, s in near:
            for t in d.tags:
                tag_w[t] += s
        best = sum(s for _, s in near) or 1.0
        return {"folder": node, "level": level, "confidence": round(conf, 3), "leaf": leaf,
                "alternatives": [(f, round(p, 3)) for f, p in alts],
                "tags": [t for t, w in tag_w.most_common(4) if w >= 0.5 * best / max(1, len(near)) * 2][:4],
                "why": [f"{d.title[:40] or d.url[:40]} ({s:.2f})" for d, s in near[:3]]}

    def evaluate(self, **_kw) -> dict:
        """The cross-validation report computed at training time."""
        return dict(self.report)


def evaluate(bms, seeds: int = 3, progress=None) -> dict:
    """Honest accuracy estimate: train and cross-validate with ``seeds`` different splits and average the reports.

    The splits are grouped by saving session, so a bookmark is never judged by neighbours saved in the same sitting.
    ``top1_range`` shows how much the number moves between splits; a wide range means the library is too small to trust.
    """
    reports = []
    for seed in range(1, max(1, seeds) + 1):
        m = FolderModel(bms, seed=seed, progress=progress)
        if m.mode != "stacked":
            return {"ready": m.ready, "mode": m.mode, "runs": 1, **m.report}
        reports.append(m.report)

    def mean(values):
        values = [v for v in values if v is not None]
        return round(sum(values) / len(values), 3) if values else None

    first = reports[0]
    out = {"ready": True, "mode": "stacked", "runs": len(reports), "training": first["training"], "folders": first["folders"],
           "evaluated": first["evaluated"], "top1": mean([r["top1"] for r in reports]), "top3": mean([r["top3"] for r in reports]),
           "top1_pooled": mean([r["top1_pooled"] for r in reports]), "top3_pooled": mean([r["top3_pooled"] for r in reports]),
           "top1_range": [min(r["top1"] for r in reports), max(r["top1"] for r in reports)], "targets": {}}
    for target, tiers in first["targets"].items():
        out["targets"][target] = {tier: {"share": mean([r["targets"][target][tier]["share"] for r in reports]),
                                         "precision": mean([r["targets"][target][tier]["precision"] for r in reports])} for tier in tiers}
    return out


def worker_count(n_docs: int, n_jobs: int) -> int:
    """How many worker processes to use for the cross-validation folds (1 = in this process).

    ``MHBM_WORKERS=N`` overrides; ``MHBM_WORKERS=1`` switches parallelism off. Small libraries are not worth the start-up.
    """
    env = os.environ.get("MHBM_WORKERS", "")
    if env.isdigit():
        return max(1, min(int(env), n_jobs))
    if n_docs < PARALLEL_MIN_DOCS:
        return 1
    try:
        cores = len(os.sched_getaffinity(0))  # the cores this process may really use (containers, taskset)
    except (AttributeError, OSError):
        cores = os.cpu_count() or 1
    return max(1, min(n_jobs, cores))


def _fold_items(tr, held, feats) -> list:
    base = Base(tr, feats)
    out = []
    for b, s in held:
        _, folders, rows = base.candidate_rows(b)
        if folders:
            out.append((b.folder, folders, rows, s, b.added))
    return out


def _fold_job(job) -> list:  # module level: worker processes import it by name
    feats = FeatureCache()
    try:
        return _fold_items(job[0], job[1], feats)
    finally:
        feats.close()


def _run_folds(jobs, workers: int, feats) -> list:
    """Results of ``_fold_items`` for every job, in order: in parallel processes if asked to, here otherwise (and here
    again if the processes cannot be started, e.g. in a locked-down sandbox)."""
    if workers > 1 and len(jobs) > 1:
        try:
            import multiprocessing
            from concurrent.futures import ProcessPoolExecutor

            # "spawn" is safe in a process that already runs threads (the web UI, the native host) and the same everywhere
            with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
                return list(pool.map(_fold_job, jobs))
        except Exception:  # any trouble with the pool: fall through to the plain loop
            pass
    return [_fold_items(tr, held, feats) for tr, held in jobs]


def curve(bms, fractions=(0.25, 0.5, 0.75, 1.0), seed: int = 1, progress=None) -> list:
    """How accuracy grows with the number of filed bookmarks: the model is trained on a share of the saving sittings
    and judged on others. ``[{fraction, filed, top1, top3, share, precision}]`` (share/precision at the 0.8 target)."""
    out = []
    for f in fractions:
        m = FolderModel(bms, seed=seed, train_fraction=f, progress=progress)
        if m.mode != "stacked":
            return []
        t = m.report["targets"]["0.8"]["overall"]
        out.append({"fraction": f, "filed": round(m.report["training"] * f), "top1": m.report["top1"], "top3": m.report["top3"],
                    "share": t["share"], "precision": t["precision"]})
    return out


def text_effect(bms, seeds: int = 1, progress=None) -> dict:
    """What the archived page text adds: the same cross-validation with and without it."""
    import copy

    blank = []
    for b in bms:
        c = copy.copy(b)
        c.content = ""
        blank.append(c)
    return {"pages": sum(1 for b in bms if b.content), "without": evaluate(blank, seeds, progress), "with": evaluate(bms, seeds, progress)}

