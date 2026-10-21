"""Shared plumbing for the command modules: output, settings, library access and plan helpers."""
from __future__ import annotations

import atexit
import json
import os
import re
import sys
from pathlib import Path

from .. import categorize, formats, i18n, urlnorm
from ..i18n import t as tr
from ..store import Library

BUILTIN: dict = {"workers": 16, "timeout": 10, "per_host": 2, "interval": 0.4, "confirmations": 2,
           "min_gap_hours": 6, "max_depth": 3, "archive_workers": 8, "archive_timeout": 15,
           "min_confidence": 0.85, "taxonomy": None, "extra_tracking_params": [], "notify": {}}


_SECRET_KEY = re.compile(r"pass|secret|token|key|webhook|auth|credential|dsn|url", re.I)
_URL_WITH_LOGIN = re.compile(r"^([a-z][a-z0-9+.-]*://)[^/\s@]*@", re.I)


def redact(value, key: str = ""):
    """``value`` with anything that looks like a secret hidden: values under keys such as ``smtp_password``, ``webhook_url`` or ``api_key``,
    and the login part of a URL. For showing configuration to a person (or a model); never for writing it back."""
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, key) for v in value]
    if _SECRET_KEY.search(key) and value not in (None, "", False, 0):
        return "(set; hidden)"
    if isinstance(value, str):
        return _URL_WITH_LOGIN.sub(r"\1(hidden)@", value)
    return value


_OPEN: list = []


class Out:
    """Human text by default, one JSON document with --json."""

    def __init__(self, args):
        self.json = bool(getattr(args, "json", False))
        self.data: dict = {}

    def say(self, *parts):
        if not self.json:
            print(i18n.bidi(" ".join(str(p) for p in parts)))

    def set(self, **kw):
        self.data.update(kw)

    def finish(self):
        if self.json:
            print(json.dumps(self.data, ensure_ascii=False, indent=2, default=str))


class Settings:
    """CLI flag > <home>/config.json > built-in default."""

    def __init__(self, args, home):
        self.args = args
        path = Path(home) / "config.json"
        try:
            self.cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, json.JSONDecodeError) as e:
            raise formats.FormatError(f"{path}: {e}") from e
        urlnorm.configure(self.cfg.get("extra_tracking_params", []))
        i18n.set_language(getattr(args, "lang", None))
        from .. import synonyms
        synonyms.configure(home)

    def __call__(self, name):
        v = getattr(self.args, name, None)
        return v if v is not None else self.cfg.get(name, BUILTIN.get(name))


def _home(args) -> str:
    return args.home or os.environ.get("MHBM_HOME") or ".mhbm"


def close_all() -> None:
    while _OPEN:
        try:
            _OPEN.pop().close()
        except Exception:
            pass


atexit.register(close_all)


def _lib(args) -> Library:
    lib = Library(_home(args))
    _OPEN.append(lib)
    return lib


def _progress(args, label):
    state = {"last": -1}

    def cb(i, n):
        if getattr(args, "quiet", False) or not n:
            return
        if sys.stderr.isatty():
            print(f"\r{label}: {i}/{n}", end="\n" if i == n else "", file=sys.stderr, flush=True)
        else:
            pct = i * 10 // n
            if pct != state["last"]:
                state["last"] = pct
                print(f"{label}: {i * 100 // n}% ({i}/{n})", file=sys.stderr, flush=True)
    return cb


def _batches(seq, size=200):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _select(lib, args, *, snippet=0):
    bms = lib.all(snippet=snippet)
    if getattr(args, "ids", None):
        wanted = set(args.ids)
        bms = [b for b in bms if b.id in wanted]
    return bms


def _row(b, **extra):
    return {"id": b.id, "title": b.title, "url": b.url, "folder": b.folder, **extra}


def _write_plan(path, plan, o):
    Path(path).write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    o.say(f"plan with {len(plan)} entries written to {path} - edit/trim it, then: apply-plan {path} --apply")
    o.set(plan_file=str(path))


def _tail(path: str, n: int) -> str:
    path = path or "(unfiled)"
    return path if len(path) <= n else "\u2026" + path[-(n - 1):]


def _preview_plan(o, bms, plan, limit):
    by_id = {b.id: b for b in bms}
    for bid, c in list(plan.items())[:limit]:
        b = by_id.get(bid)
        if b is None:
            continue
        src = f"{c.get('source', '')} {c.get('confidence', '')}".strip()
        o.say(f"{b.title[:60] or b.url[:60]}")
        o.say(f"    {_tail(b.folder, 38)}  ->  {_tail(c.get('folder', b.folder), 52)}" + (f"   [{src}]" if src else ""))
        if c.get("reason"):
            o.say(f"    why: {c['reason'][:110]}")


def _apply_plan_cmd(lib, bms, plan, name, o, max_depth, apply, prune_empty=False, channel="plan-file"):
    from .. import feedback

    by_id = {b.id: b for b in bms}
    res = categorize.apply_plan(bms, plan, max_depth=max_depth, known_folders=lib.folders() | lib.used_folders())
    for r in res.rejected[:20]:
        o.say("rejected:", r)
    o.set(valid=res.applied, rejected=res.rejected[:50], deleted=len(res.deleted))
    o.say(f"{len(res.changed)} changes, {len(res.deleted)} deletions, {len(res.rejected)} rejected"
          + (f", {len(res.declined)} declined in review" if res.declined else ""))
    if apply:
        o.say("snapshot:", lib.snapshot("pre-" + name))
        with lib.op(name):
            lib.ensure_unchanged_since_read()      # inside the write lock: nothing can change between this check and the write
            lib.upsert(res.changed)
            lib.delete(b.id for b in res.deleted)
            if prune_empty:
                lib.set_folders(lib.used_folders())
            else:
                lib.add_folders(lib.used_folders())
            counts = feedback.settle_plan(lib, res, plan, by_id, channel)
        o.set(feedback=counts)
        o.say(tr("changes_saved"))
    else:
        o.say(tr("dry_run_apply"))
    return res


def apply_flag(sp):
    sp.add_argument("--apply", action="store_true", help="write changes (default is a dry run)")
    sp.add_argument("--show", type=int, default=15, help="preview rows")


def net_flags(sp):
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--ids", nargs="*")


def model_for(lib, args, bms, *, retrain=False):
    """The filing model for this library: cached while the filed bookmarks are unchanged, trained (with progress) otherwise."""
    from .. import learn

    cache = lib.root / "model.json"
    if retrain:
        if lib.read_only:
            cache = None            # a read-only library never touches the cache: retraining just ignores it
        else:
            cache.unlink(missing_ok=True)
    loud = not (getattr(args, "quiet", False) or getattr(args, "json", False))
    first = [True]

    def progress(msg):
        if loud:
            if first[0]:
                print("training the filing model (once; cached until your filed bookmarks change)...", file=sys.stderr)
                first[0] = False
            print(f"  {msg}", file=sys.stderr, flush=True)

    model = learn.FolderModel(bms, cache=cache, progress=progress, cache_write=not lib.read_only)
    from .. import feedback

    model.set_outcomes(feedback.outcomes(lib))  # what the user actually accepted corrects the quoted confidence
    return model


def exclusions_for(lib, bms) -> dict:
    """Folders already turned down for a bookmark: they are not suggested for it again."""
    from .. import feedback

    return feedback.exclusions_for(lib, bms)


def rules_for(lib):
    from .. import rules

    return rules.load(lib.root)
