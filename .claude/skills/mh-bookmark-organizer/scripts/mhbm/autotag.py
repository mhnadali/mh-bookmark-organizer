"""Keyword tags from your own library.

Tags worth having group several bookmarks, so a candidate must occur in at least ``min_df`` bookmarks (and not in so
many that it says nothing). Candidates come from the title, the URL path, the page text (when archived), the site
name and frequent two-word phrases; they are ranked by TF-IDF, and tags the library already uses are preferred so
your vocabulary stays consistent.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from urllib.parse import urlsplit

from .model import Bookmark, clean_tags, is_unfiled
from .textnorm import words
from .urlnorm import host_of

_GENERIC = frozenset(["free", "online", "official", "site", "website", "login", "sign", "signup", "account", "dashboard", "home", "page", "app", "apps", "tool", "tools", "get", "use", "using", "make", "made", "new", "best", "top", "guide", "blog", "news", "help", "support", "contact", "about", "privacy", "terms", "docs", "documentation", "download", "view", "watch", "read", "learn", "more", "all", "one", "two", "three", "way", "ways", "how", "what", "why", "like", "just", "your", "you", "our", "can", "will", "not", "now", "create", "today", "simple", "company", "business", "service", "services", "solution", "solutions", "platform", "software", "digital", "list", "lists", "page", "pages", "data", "store", "shop", "buy", "sale", "price", "prices", "plan", "plans", "pricing", "team", "works", "work", "get", "started", "start", "tutorial", "tutorials", "tips", "tip", "part", "parts", "overview", "introduction", "intro", "notes", "note", "update", "updates"]) | frozenset(["دانلود", "رایگان", "برنامه", "سایت", "آماده", "چیست", "های", "برای", "است", "این", "آن", "با", "به", "از", "در", "که", "را", "یا", "خرید", "فروش", "قیمت", "ساخت", "صفحه", "اصلی", "خانه", "ورود", "ثبت", "نام", "بهترین", "جدید", "آموزش", "سرویس", "خدمات", "شرکت", "فروشگاه", "آنلاین", "اینترنتی"])


class Vocab:
    def __init__(self, bms):
        self.n = max(1, len(bms))
        self.df: Counter = Counter()
        self.bigram_df: Counter = Counter()
        self.site_df: Counter = Counter()
        self.existing: Counter = Counter()
        self.where: dict = defaultdict(Counter)  # term -> folders it occurs in (filed bookmarks only)
        for b in bms:
            terms, bigrams = self._terms(b)
            uniq = set(terms) | set(bigrams)
            self.df.update(set(terms))
            self.bigram_df.update(set(bigrams))
            self.site_df[_site(b.url)] += 1
            self.existing.update(b.tags)
            if not is_unfiled(b):
                for t in uniq:
                    self.where[t][b.folder] += 1

    def purity(self, term: str) -> float:
        """How concentrated a term is in your folders: 1.0 = always the same folder, ~0 = spread everywhere (noise)."""
        c = self.where.get(term)
        if not c:
            return 0.5  # unknown (only in unfiled bookmarks): neutral
        total = sum(c.values())
        return c.most_common(1)[0][1] / total if total >= 3 else 0.5

    def pmi(self, bigram: str) -> float:
        x, y = bigram.split("-", 1)
        if not self.df[x] or not self.df[y] or not self.bigram_df[bigram]:
            return 0.0  # a pair of known words that has never been seen side by side is not a known phrase
        return math.log(self.bigram_df[bigram] * self.n / (self.df[x] * self.df[y]))

    @staticmethod
    def _terms(b: Bookmark):
        tw = words(b.title)
        terms = tw + words(urlsplit(b.url).path.replace("/", " ").replace("-", " ").replace("_", " ")) + words(b.content[:1500])
        bigrams = [f"{x}-{y}" for x, y in zip(tw, tw[1:])]
        return terms, bigrams


def _site(url: str) -> str:
    labels = host_of(url).split(".")
    return labels[-2] if len(labels) >= 2 else (labels[0] if labels else "")


class _Candidates:
    """Candidate tags with their best score; ``consider`` applies the rules for what is worth proposing at all."""

    def __init__(self, vocab: Vocab, min_df: int, max_df_frac: float):
        n = vocab.n
        self.vocab, self.n, self.min_df = vocab, n, min_df
        # small libraries: a topic may cover a good share of them
        self.max_df = max(10, 0.4 * n if n <= 200 else max_df_frac * n)
        self.scored: dict = {}

    def consider(self, tag, weight, source, df) -> None:
        vocab = self.vocab
        if len(tag) < 3 or tag in _GENERIC or tag.replace("-", "").isdigit():
            return
        if df < self.min_df or df > self.max_df:
            return
        if df >= 6 and vocab.purity(tag) < 0.2:
            return  # spread evenly across your folders: a filler word, not a topic
        score = weight * math.log(self.n / df) * (0.35 + vocab.purity(tag))  # tags that line up with your folders are meaningful
        if vocab.existing.get(tag, 0) >= 3:
            score *= 1.5  # reuse the vocabulary you already have
        if tag not in self.scored or self.scored[tag][0] < score:
            self.scored[tag] = (score, source)


def _gather(b: Bookmark, vocab: Vocab, cands: _Candidates) -> None:
    title_words = words(b.title)
    path_words = words(urlsplit(b.url).path.replace("/", " ").replace("-", " ").replace("_", " "))
    for w in set(title_words):
        cands.consider(w, 1.0, "title", vocab.df[w])
    for w in set(path_words):
        cands.consider(w, 0.4, "url", vocab.df[w])
    for w, c in Counter(words(b.content[:1500])).items():
        cands.consider(w, 0.3 * min(c, 3), "page text", vocab.df[w])
    for x, y in zip(title_words, title_words[1:]):
        bg = f"{x}-{y}"
        if x not in _GENERIC and y not in _GENERIC and vocab.pmi(bg) >= 2.0:  # really a phrase, not two common words side by side
            cands.consider(bg, 1.4, "phrase", vocab.bigram_df[bg])
    site = _site(b.url)
    if site and vocab.site_df[site] >= max(3, cands.min_df):
        cands.consider(re.sub(r"[^\w]", "", site.lower()), 0.8, "site", vocab.site_df[site])


def _choose(scored: dict, max_tags: int) -> list:
    """Best candidates first, dropping a word that a chosen phrase already covers."""
    chosen: list = []
    for tag, (score, source) in sorted(scored.items(), key=lambda kv: -kv[1][0]):
        parts = tag.split("-")
        if any(tag != c and (tag in c.split("-") or (len(parts) > 1 and all(p in c.split("-") for p in parts))) for c, _, _ in chosen):
            continue
        chosen = [c for c in chosen if c[0] not in parts] + [(tag, score, source)]
        if len(chosen) >= max_tags:
            break
    return sorted(chosen, key=lambda c: -c[1])[:max_tags]


def suggest(b: Bookmark, vocab: Vocab, *, max_tags: int = 3, min_df: int = 3, max_df_frac: float = 0.05):
    """``[(tag, score, source)]`` best first, at most ``max_tags``."""
    cands = _Candidates(vocab, min_df, max_df_frac)
    _gather(b, vocab, cands)
    return _choose(cands.scored, max_tags)


def propose(bms, *, max_tags: int = 3, min_df: int = 3, only_untagged: bool = True, max_df_frac: float = 0.05) -> dict:
    vocab = Vocab(bms)
    plan = {}
    for b in bms:
        if only_untagged and b.tags:
            continue
        picks = suggest(b, vocab, max_tags=max_tags, min_df=min_df, max_df_frac=max_df_frac)
        tags = [t for t, _, _ in picks if t not in b.tags]
        tags = clean_tags(tags)
        if tags:
            plan[b.id] = {"tags": tags, "reason": "keywords: " + ", ".join(f"{t} ({s})" for t, _, s in picks if t in tags)}
    return plan
