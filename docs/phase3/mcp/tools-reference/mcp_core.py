"""Reference helpers for the Phase 3 MCP server (prototype, stdlib only, Python 3.9+).

Everything here is exercised by test_mcp_core.py and scenario_tests.py in this directory.  It is a *specification
aid*: the implementer may copy, split or rewrite it, but the behaviours are the ones the spec in TOOLS.md promises.

Contents
  validate()          tiny JSON-Schema subset validator (no third-party dependency)
  sanitize_text()     strips characters that hide instructions from the human but not from the model
  clip()              length caps for untrusted fields
  redact_config()     `config` output without secrets
  PathPolicy          allow-dir confinement for read / write / write-new / dir paths (symlink-safe)
  fingerprint()       cheap digest of the library state a preview was computed on
  PreviewStore        server-side proposals: preview_id -> {tool, args, library fingerprint, plan digest, confirm_text}
  build_argv()        argv DSL interpreter (the "exact argv mapping" in tools.json)
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path

# --------------------------------------------------------------------------- schema validation
_TYPES = {"string": str, "boolean": bool, "object": dict, "array": list, "null": type(None)}


def _type_ok(expected, value) -> bool:
    if isinstance(expected, list):
        return any(_type_ok(t, value) for t in expected)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[expected])


def validate(schema: dict, value, path: str = "$") -> list:
    """Errors (strings) for ``value`` against the subset of JSON Schema used in tools.json; [] means valid.

    Strict on purpose: no coercion ("5" is not an integer).  Messages name the argument and the rule, because the
    model reads them and retries (MCP 2025-11-25: input validation problems are tool execution errors, isError=true).
    """
    errs: list = []
    t = schema.get("type")
    if t is not None and not _type_ok(t, value):
        return [f"{path}: expected {t}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: must be one of {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        errs.append(f"{path}: must be {schema['const']!r}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errs.append(f"{path}: shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append(f"{path}: longer than {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errs.append(f"{path}: does not match the required pattern {schema['pattern']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: must be <= {schema['maximum']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errs.append(f"{path}: needs at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append(f"{path}: at most {schema['maxItems']} item(s)")
        if schema.get("uniqueItems") and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            errs.append(f"{path}: items must be unique")
        if "items" in schema:
            for i, v in enumerate(value):
                errs += validate(schema["items"], v, f"{path}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errs.append(f"{path}: missing required argument '{name}'")
        if schema.get("additionalProperties") is False:
            for k in value:
                if k not in props:
                    errs.append(f"{path}: unknown argument '{k}' (allowed: {', '.join(sorted(props))})")
        elif isinstance(schema.get("additionalProperties"), dict):
            for k, v in value.items():
                if k not in props:
                    errs += validate(schema["additionalProperties"], v, f"{path}.{k}")
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            errs.append(f"{path}: at most {schema['maxProperties']} entries")
        for k, sub in props.items():
            if k in value:
                errs += validate(sub, value[k], f"{path}.{k}" if path != "$" else k)
    return errs


def apply_defaults(schema: dict, args: dict) -> dict:
    out = dict(args)
    for k, sub in schema.get("properties", {}).items():
        if k not in out and "default" in sub:
            out[k] = sub["default"]
    return out


# --------------------------------------------------------------------------- untrusted text
# C0/C1 controls (except tab/newline), bidi overrides/isolates, BOM, line/paragraph separators, Unicode "tag" characters
# (U+E0000-E007F, used to smuggle invisible instructions) and variation-selector supplements.  ZWNJ/ZWJ (U+200C/D) and
# LRM/RLM (U+200E/F) are *kept*: Persian and Arabic text needs them (the fixture's "پس‌انداز" contains a ZWNJ).
_BAD = re.compile("[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f‪-‮⁦-⁩﻿"
                  "\U000e0000-\U000e007f\U000e0100-\U000e01ef]")


def sanitize_text(s, limit: int = 0) -> str:
    s = "" if s is None else str(s)
    s = s.replace(" ", " ").replace(" ", " ")
    s = _BAD.sub("", s)
    return clip(s, limit) if limit else s


def clip(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[: max(0, limit - 1)] + "…"


# --------------------------------------------------------------------------- config redaction
SAFE_CONFIG_KEYS = ("workers", "timeout", "per_host", "interval", "confirmations", "min_gap_hours", "max_depth",
                    "archive_workers", "archive_timeout", "min_confidence", "extra_tracking_params")


def redact_config(raw: dict, home: str) -> dict:
    """``config --json`` minus anything that can hold a secret or a private path.

    ``notify`` can contain a webhook URL (``notify.webhook.url``) and SMTP user/host/addresses; ``taxonomy`` is a path
    on the user's disk; ``config_file`` reveals the user name.  Only the shape is kept.
    """
    eff = raw.get("effective", {})
    out = {k: eff[k] for k in SAFE_CONFIG_KEYS if k in eff}
    notify = eff.get("notify") or {}
    out["notify"] = {"configured": bool(notify.get("on")), "providers": [p for p in notify.get("on", []) if p in ("desktop", "email", "webhook")],
                     "when": notify.get("when", "problems") if notify else None}
    out["taxonomy"] = "custom" if eff.get("taxonomy") else "built-in"
    out["config_file"] = "<home>/config.json"
    return out


# --------------------------------------------------------------------------- path confinement
class PathError(ValueError):
    pass


class PathPolicy:
    """Confines user-chosen paths to ``allow_dirs`` (plus the server's own ``home`` subdirectories where a tool says so).

    * ``..`` and symlinks are resolved *and* any symlink component of a write target is refused (a link created after the
      check cannot redirect a write);
    * ``read``: must exist, be a regular file, at most ``max_bytes``;
    * ``write-new``: must not exist (unless ``overwrite``), parent must exist, extension must match the format;
    * ``dir-new``: must not exist or be an empty directory (obsidian export);
    * names starting with '.' and anything below .git, .ssh, .claude, .config, .gnupg are refused as write targets;
    * the allow list itself must not be '/' or the user's home directory itself (checked once at start-up).
    """

    DENY_PARTS = {".git", ".ssh", ".claude", ".config", ".gnupg", ".aws", ".kube"}

    def __init__(self, allow_dirs, home):
        self.allow = [Path(d).resolve() for d in allow_dirs]
        self.home = Path(home).resolve()
        for d in self.allow:
            if d == Path(d.anchor) or d == Path.home().resolve():
                raise ValueError(f"--allow-dir {d} is too broad (filesystem root or your home directory); give a dedicated directory")

    def _inside(self, p: Path, roots) -> bool:
        return any(p == r or r in p.parents for r in roots)

    def _check_links(self, raw: Path):
        cur = Path(raw.anchor) if raw.is_absolute() else Path.cwd()
        for part in raw.parts[1 if raw.is_absolute() else 0:]:
            cur = cur / part
            if cur.is_symlink():
                raise PathError(f"{cur} is a symbolic link; links are not followed for output paths")

    def resolve(self, raw: str, mode: str, *, ext=(), overwrite: bool = False, max_bytes: int = 64 * 2**20, extra_roots=()) -> Path:
        if "\x00" in raw or not raw.strip():
            raise PathError("empty or invalid path")
        p = Path(os.path.expanduser(raw))
        target = p.resolve()
        roots = list(self.allow) + [Path(r).resolve() for r in extra_roots]
        if not self._inside(target, roots):
            shown = ", ".join(str(r) for r in roots) or "(none)"
            raise PathError(f"path is outside the allowed directories ({shown}); start the server with --allow-dir to widen it")
        if mode == "read":
            if not target.is_file():
                raise PathError(f"{raw}: not a file")
            if target.stat().st_size > max_bytes:
                raise PathError(f"{raw}: larger than {max_bytes // 2**20} MB")
            return target
        self._check_links(p if p.is_absolute() else Path.cwd() / p)
        rel_parts = set(target.parts)
        if (self.DENY_PARTS & rel_parts) or target.name.startswith("."):
            raise PathError("refusing to write to a hidden or configuration location")
        if mode == "write-new":
            if ext and target.suffix.lower() not in ext:
                raise PathError(f"output file must end in {' or '.join(ext)}")
            if target.exists() and not overwrite:
                raise PathError(f"{raw} already exists (pass overwrite=true to replace it)")
            if not target.parent.is_dir():
                raise PathError(f"{target.parent}: directory does not exist")
            return target
        if mode == "dir-new":
            if target.exists() and (not target.is_dir() or any(target.iterdir())):
                raise PathError(f"{raw} exists and is not an empty directory")
            return target
        raise ValueError(mode)


# --------------------------------------------------------------------------- library fingerprint / previews
def fingerprint(db) -> str:
    """Digest of everything a plan can depend on.  ``db`` is a sqlite3 connection.  ~10 ms per 5k bookmarks (measured)."""
    h = hashlib.sha256()
    for row in db.execute("SELECT id,url,title,folder,tags,notes,status,state,favorite FROM bookmarks ORDER BY id"):
        h.update(("\x1f".join(str(c) for c in row) + "\x1e").encode("utf-8", "replace"))
    for (p,) in db.execute("SELECT path FROM folders ORDER BY path"):
        h.update(("F" + p + "\x1e").encode("utf-8", "replace"))
    h.update(str(db.execute("SELECT COALESCE(MAX(op),0) FROM ops").fetchone()[0]).encode())
    return h.hexdigest()[:24]


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def canon_plan(plan: dict) -> str:
    """Plan digest input: order-insensitive for tag lists (autotag's tag order differs between processes - verified)."""
    def norm(v):
        if isinstance(v, dict):
            return {k: (sorted(x) if k == "tags" and isinstance(x, list) else norm(x)) for k, x in v.items()}
        if isinstance(v, list):
            return [norm(x) for x in v]
        return v
    return canon(norm(plan))


class PreviewError(Exception):
    def __init__(self, code, message, hint=""):
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint


class PreviewStore:
    """Server-side memory of proposals.  ``pv_<16 hex>`` is unguessable (secrets.token_hex); the recipe never leaves the server,
    so bm_apply needs no arguments but the id and the confirm text.

    A proposal records the tool, its arguments, the library fingerprint and the canonical digest of what was shown.  bm_apply
    re-checks all three; any writer in between (web UI, extension, CLI, cron, another tool) voids it.  Single use, 10 minutes,
    at most ``MAX`` live proposals (oldest dropped).  A restart drops everything - by design.
    """

    TTL = 600
    MAX = 200

    def __init__(self, ttl: int = TTL, clock=time.time):
        self.ttl, self.clock = ttl, clock
        self._items: dict = {}

    def issue(self, tool, args, fp, digest, confirm_text, deletes=0, payload=None) -> str:
        now = self.clock()
        for k in [k for k, v in self._items.items() if v["exp"] < now]:
            del self._items[k]
        while len(self._items) >= self.MAX:
            del self._items[next(iter(self._items))]
        pid = "pv_" + secrets.token_hex(8)
        self._items[pid] = {"tool": tool, "args": dict(args), "fp": fp, "digest": digest, "confirm_text": confirm_text, "deletes": deletes,
                            "payload": payload, "exp": now + self.ttl}
        return pid

    def get(self, pid: str) -> dict:
        rec = self._items.get(pid) if isinstance(pid, str) else None
        if rec is None:
            raise PreviewError("PREVIEW_UNKNOWN", "unknown, expired or already used preview_id (previews live 10 minutes and only in this server session)",
                               "call the propose tool again")
        if self.clock() > rec["exp"]:
            del self._items[pid]
            raise PreviewError("PREVIEW_UNKNOWN", "the preview expired", "call the propose tool again")
        return rec

    @staticmethod
    def check(rec: dict, confirm: str, fp: str = None, digest: str = None) -> None:
        if confirm != rec["confirm_text"]:
            raise PreviewError("CONFIRMATION_MISMATCH", "confirm must repeat the preview's confirm_text exactly", rec["confirm_text"])
        if fp is not None and (fp != rec["fp"] or digest != rec["digest"]):
            raise PreviewError("PREVIEW_STALE", "the library (or what the proposal would do) changed since the preview was made; nothing was applied",
                               "call the propose tool again and show the user the new preview")

    def consume(self, pid: str) -> None:
        self._items.pop(pid, None)


# --------------------------------------------------------------------------- argv DSL
_ALLOWED = (ast.Expression, ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Constant, ast.Name, ast.Load)


def _expr(src: str, env: dict):
    tree = ast.parse(src, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ValueError(f"unsupported expression {src!r}")
    return eval(compile(tree, "<expr>", "eval"), {"__builtins__": {}}, env)  # noqa: S307 - whitelisted AST only


def build_argv(spec: list, args: dict, derived: dict | None = None) -> list:
    """Interpret the argv DSL of tools.json.

    token forms
      "literal"                                  emitted as is
      {"opt": "--x", "arg": "a"}                 ``--x=VALUE`` when args[a] is present (``=`` form: VALUE may start with '-')
      {"opt": "--x", "arg": "a", "map": {..}}    value looked up in map first
      {"opt": "--x", "expr": "offset+limit+1"}   ``--x=<expression over args>``
      {"opt": "--x", "value": ""}                ``--x=`` literal value
      {"flag": "--x", "arg": "a"}                ``--x`` when args[a] is truthy
      {"flag": "--x", "arg": "a", "eq": v}       ``--x`` when args[a] == v
      {"multi": "--x", "arg": "a"}               ``--x v1 v2 ...`` (list values; the schema forbids a leading '-')
      {"pair": "--x", "args": ["a", "b"]}        ``--x A B``
      {"pos": "a"} / {"pos_list": "a"}           positional(s); all are appended after a single "--"
    every token may carry ``"when": "<arg>"`` (only if truthy) or ``"when_eq": ["<arg>", value]``.
    """
    derived = derived or {}
    env = {**args, **derived}
    out: list = []
    positionals: list = []

    def get(name):
        return derived[name] if name in derived else args.get(name)

    for tok in spec:
        if isinstance(tok, str):
            out.append(tok)
            continue
        if "when" in tok and not get(tok["when"]):
            continue
        if "when_eq" in tok and get(tok["when_eq"][0]) != tok["when_eq"][1]:
            continue
        if "opt" in tok:
            if "value" in tok:
                v = tok["value"]
            elif "expr" in tok:
                v = _expr(tok["expr"], env)
            else:
                v = get(tok["arg"])
                if v is None or (v is False):
                    continue
                v = tok.get("map", {}).get(v, v) if "map" in tok else v
                if tok.get("skip_if") is not None and v == tok["skip_if"]:
                    continue
            out.append(f"{tok['opt']}={v}")
        elif "flag" in tok:
            v = get(tok["arg"]) if "arg" in tok else True
            if ("eq" in tok and v == tok["eq"]) or ("eq" not in tok and v):
                out.append(tok["flag"])
        elif "multi" in tok:
            v = get(tok["arg"])
            if v:
                out.append(tok["multi"])
                out += [str(x) for x in v]
        elif "pair" in tok:
            out.append(tok["pair"])
            out += [str(get(a)) for a in tok["args"]]
        elif "pos" in tok:
            positionals.append(str(get(tok["pos"])))
        elif "pos_list" in tok:
            positionals += [str(x) for x in get(tok["pos_list"])]
        else:
            raise ValueError(f"bad argv token {tok!r}")
    if positionals:
        out += ["--"] + positionals
    return out
