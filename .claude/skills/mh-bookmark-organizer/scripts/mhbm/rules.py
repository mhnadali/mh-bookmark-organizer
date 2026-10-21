"""Your own filing rules ("teach"): deterministic overrides that run before the learned model.

``<home>/rules.json`` holds a list of ``{"id", "when": {...}, "folder", "tags"}``; ``when`` accepts exactly one of
``host`` (the site or any of its subdomains), ``url_prefix``, ``title_regex`` and ``keyword`` (whole word in title/URL).
"""
from __future__ import annotations

import json
import re
import signal
import sys
import threading
from collections import Counter, defaultdict
from pathlib import Path

from .categorize import resolve_folder
from .model import Bookmark, clean_folder, clean_tags, is_unfiled
from .urlnorm import host_of

KINDS = ("host", "url_prefix", "title_regex", "keyword")


class RuleError(ValueError):
    pass


MAX_REGEX = 200          # characters of a ``title_regex``
SEARCH_TITLE = 400       # characters of a title it is matched against
MATCH_SECONDS = 0.25     # one rule may spend this long on one title (where the platform can enforce it)
MAX_UNBOUNDED = 3        # ``.*a.*b.*c.*d`` is O(n^4) on a title that does not match


def regex_problem(pattern: str) -> str:
    """Why ``pattern`` could take forever on some title ("" when it is fine).

    Python's regex engine backtracks, and a pattern such as ``(a+)+$`` needs time exponential in the length of the text on an unlucky title.
    Rules can come from a file or from a model, so the shapes that cause that are refused up front: a repeated group that itself contains
    an unbounded repeat (``(a+)+``, ``(\\w+\\s?)*``, ``(.*a){5}``) or an alternation (``(a|aa)+``) or a quantifier repeated many times
    (``(a?){30}``, ``(a{1,30}){1,30}``), more than ``MAX_UNBOUNDED`` unbounded repeats in a row (``\\w*\\w*\\w*\\w*x``), inline flags that
    change how the pattern is read (``(?x)``), and back-references. Everything ordinary (``^\\[Docs\\]``, ``k8s|kubernetes``,
    ``\\bGPT-?\\d+\\b``, ``(?:foo|bar)\\s+\\d+``) is accepted. This is a screen, not a proof: where the platform allows (POSIX), ``matches``
    also stops a match that runs longer than ``MATCH_SECONDS``."""
    if len(pattern) > MAX_REGEX:
        return f"longer than {MAX_REGEX} characters"
    if re.search(r"\\[1-9]|\(\?P=", pattern):
        return "back-references are not allowed"
    if re.search(r"\(\?[a-wyzA-Z]*x[a-zA-Z]*[:)]", pattern):
        return "inline flags that change how the pattern is read (like (?x)) are not allowed"
    problem, unbounded = _scan_groups(pattern)
    if problem:
        return problem
    if unbounded > MAX_UNBOUNDED:
        return f"more than {MAX_UNBOUNDED} unbounded repeats (* + {{n,}}); matching time grows with a power of the title length"
    return ""


def _scan_groups(pattern: str) -> tuple:
    """(problem, number of unbounded repeats) from one pass over the pattern, tracking what every open group contains."""
    stack: list = [[False, False, False, 0]]   # per open group: unbounded repeat inside? any repeat inside? alternation inside? where its text starts
    unbounded = 0
    i, in_class = 0, False
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            i += 2
            continue
        if in_class:
            in_class = ch != "]"
        elif ch == "[":
            in_class = True
        elif ch == "(":
            prefix = re.match(r"\(\?(?:P<\w+>|<[=!]|[:=!]|[a-zA-Z]*[:)])", pattern[i:])   # (?: (?P<n> (?= (?<! (?i: ... - not repeats
            i += prefix.end() if prefix else 1
            stack.append([False, False, False, i])
            continue
        elif ch == "|":
            stack[-1][2] = True
        elif ch == ")" and len(stack) > 1:
            inner = stack.pop()
            problem = _repeated_group_problem(inner, pattern[i + 1:], pattern[inner[3]:i])
            if problem:
                return problem, unbounded
            stack[-1][0] = stack[-1][0] or inner[0]
            stack[-1][1] = stack[-1][1] or inner[1]
        elif ch in "*+" or (ch == "{" and re.match(r"\{\d+,\}", pattern[i:])):
            stack[-1][0] = stack[-1][1] = True
            unbounded += 1
        elif ch == "?" or (ch == "{" and re.match(r"\{\d*,?\d*\}", pattern[i:])):
            stack[-1][1] = True
        i += 1
    return "", unbounded


def _alternatives_overlap(group: str) -> bool:
    """Could two alternatives of ``group`` match the same text? Judged by how each begins: the same literal, or anything that is not a plain
    literal character (a class, an escape, a dot, a group) counts as possibly overlapping."""
    alts, depth, cur, i, in_class = [], 0, "", 0, False
    while i < len(group):
        ch = group[i]
        if ch == "\\":
            cur += group[i:i + 2]
            i += 2
            continue
        if in_class:
            in_class = ch != "]"
        elif ch == "[":
            in_class = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "|" and depth == 0:
            alts.append(cur)
            cur = ""
            i += 1
            continue
        cur += ch
        i += 1
    alts.append(cur)
    firsts = [a[:1] for a in alts]
    return len(set(firsts)) != len(firsts) or any(not f.isalnum() for f in firsts)


def _repeated_group_problem(inner: list, rest: str, text: str = "") -> str:
    """``inner`` = [has an unbounded repeat, has any repeat, has an alternation, ...] of a group that was just closed; ``rest`` follows the ')'
    and ``text`` is what the group holds."""
    repeated = re.match(r"[*+]|\{(\d*)(,?)(\d*)\}", rest)
    if not repeated or repeated.group(0) in ("{1}", "{0,1}", "{,1}"):
        return ""
    if inner[0]:
        return "a repeated group contains another unbounded repeat (like (a+)+), which can make matching exponentially slow"
    unbounded_outer = repeated.group(0) in ("*", "+") or (repeated.group(2) and not repeated.group(3))
    if inner[2] and unbounded_outer and _alternatives_overlap(text):
        return "a repeated group contains alternatives that can match the same text (like (a|aa)+), which can make matching exponentially slow"
    upper = repeated.group(3) or repeated.group(1)
    if inner[1] and (unbounded_outer or (upper and int(upper) >= 10)):
        return "a group holding a repeat is itself repeated many times (like (a?){30}), which can make matching exponentially slow"
    return ""


class _Slow(Exception):
    """A match ran past its deadline."""


_slow_rules: set = set()          # ids of rules whose pattern ran past its deadline in this process: skipped from then on


def _search_with_deadline(pattern: str, text: str) -> bool:
    """``re.search`` that gives up after ``MATCH_SECONDS`` where the platform lets a timer interrupt the regex engine (POSIX, main thread)."""
    if not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread():
        return re.search(pattern, text, re.I) is not None

    def on_alarm(signum, frame):
        raise _Slow

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, MATCH_SECONDS)
    try:
        return re.search(pattern, text, re.I) is not None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def path_for(home) -> Path:
    return Path(home) / "rules.json"


def load(home) -> list:
    p = path_for(home)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise RuleError(f"{p}: {e}") from e
    return [validate(r) for r in data] if isinstance(data, list) else []


def save(home, rules) -> None:
    Path(home).mkdir(parents=True, exist_ok=True)
    path_for(home).write_text(json.dumps(rules, indent=2, ensure_ascii=False), encoding="utf-8")


def validate(rule: dict) -> dict:
    when = rule.get("when") or {}
    if len(when) != 1 or next(iter(when)) not in KINDS:
        raise RuleError(f"rule {rule.get('id')}: 'when' needs exactly one of {', '.join(KINDS)}")
    kind, value = next(iter(when.items()))
    if not str(value).strip():
        raise RuleError(f"rule {rule.get('id')}: empty {kind}")
    if kind == "title_regex":
        try:
            re.compile(value)
        except re.error as e:
            raise RuleError(f"rule {rule.get('id')}: bad regular expression ({e})") from e
        if problem := regex_problem(str(value)):
            raise RuleError(f"rule {rule.get('id')}: unsafe regular expression: {problem}")
    folder = clean_folder(rule.get("folder", ""))
    if not folder:
        raise RuleError(f"rule {rule.get('id')}: a folder is required")
    return {"id": int(rule.get("id", 0)), "when": {kind: str(value)}, "folder": folder, "tags": clean_tags(rule.get("tags", []))}


def add(rules: list, kind: str, value: str, folder: str, tags=()) -> dict:
    if kind == "host":
        value = host_of(value if "//" in value else "https://" + value) or value.lower()
    rule = validate({"id": max([r["id"] for r in rules], default=0) + 1, "when": {kind: value}, "folder": folder, "tags": list(tags)})
    rules.append(rule)
    return rule


def matches(b: Bookmark, rule: dict) -> bool:
    kind, value = next(iter(rule["when"].items()))
    if kind == "host":
        h = host_of(b.url)
        return h == value or h.endswith("." + value)
    if kind == "url_prefix":
        return b.url.lower().startswith(value.lower())
    if kind == "title_regex":
        if rule.get("id") in _slow_rules:
            return False
        try:
            return _search_with_deadline(value, (b.title or "")[:SEARCH_TITLE])
        except _Slow:
            _slow_rules.add(rule.get("id"))
            print(f"warning: rule #{rule.get('id')} (title_regex {value!r}) takes too long on a title and is skipped; simplify it", file=sys.stderr)
            return False
    return re.search(rf"(?<!\w){re.escape(value.lower())}(?!\w)", (b.title + " " + b.url).lower()) is not None


def apply(b: Bookmark, rules: list, known_folders=()):
    """First matching rule as a plan entry, or None. The folder resolves onto the user's existing spelling."""
    for rule in rules:
        if matches(b, rule):
            kind, value = next(iter(rule["when"].items()))
            return {"folder": resolve_folder(rule["folder"], known_folders), "tags": rule["tags"], "source": "rule",
                    "confidence": 1.0, "level": "exact", "reason": f"your rule #{rule['id']}: {kind} {value}"}
    return None


def suggest(bms, min_filed: int = 3, purity: float = 0.9, existing=()) -> list:
    """Sites that are (almost) always filed in one folder and still have unfiled bookmarks."""
    have = {next(iter(r["when"].values())) for r in existing if "host" in r["when"]}
    by_host: dict = defaultdict(Counter)
    loose: Counter = Counter()
    for b in bms:
        h = host_of(b.url)
        if not h:
            continue
        if is_unfiled(b):
            loose[h] += 1
        else:
            by_host[h][b.folder] += 1
    out = []
    for h, folders in by_host.items():
        total = sum(folders.values())
        folder, n = folders.most_common(1)[0]
        if total >= min_filed and n / total >= purity and loose[h] and h not in have:
            out.append({"host": h, "folder": folder, "filed": total, "purity": round(n / total, 2), "unfiled": loose[h]})
    return sorted(out, key=lambda x: (-x["unfiled"], -x["filed"]))
