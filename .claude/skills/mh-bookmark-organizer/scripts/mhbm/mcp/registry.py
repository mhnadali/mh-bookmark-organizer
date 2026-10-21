"""The tool table: one declarative entry per tool (schema, annotations, how to build the command line, how to shape the answer).

Conventions enforced by ``selfcheck()`` (and by the unit tests):

* every ``inputSchema`` is an object with ``additionalProperties: false``; every string has ``maxLength``; every array has ``maxItems``;
  every number has bounds - so a request is bounded before any pattern or command runs;
* no value is ever coerced (see validate.py): ``apply: "false"`` is an error, not a truthy string;
* free text is passed to the CLI as ``--flag=value`` or after ``--``, so a value that starts with ``-`` can never become an option;
* a tool is *read-only* only if it provably writes nothing (``readOnlyHint``), *destructive* only if it can delete or overwrite user
  data, *open-world* only if it talks to hosts outside this machine.
"""
from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import validate
from .paths import PathError
from .plans import ToolError

MAX_IMPORT_BYTES = 256 << 20
# literal copies of browsers.ALL / formats.READ_FORMATS / formats.WRITE_FORMATS (minus "obsidian": it writes a directory, this tool writes one new
# file): this process must not import the heavy modules (a test keeps them equal)
BROWSERS = ["chrome", "chromium", "brave", "edge", "vivaldi", "opera", "opera-gx", "yandex", "firefox", "librewolf", "waterfox", "floorp", "zen", "safari"]
EXPORT_FORMATS = ["html", "chrome", "csv", "json", "md", "raindrop", "pocket", "linkwarden", "karakeep", "instapaper"]
IMPORT_FORMATS = ["html", "chrome", "firefox", "csv", "json"]


# ------------------------------------------------------------------ schema helpers
def Int(lo, hi, desc, default=None):
    s = {"type": "integer", "minimum": lo, "maximum": hi, "description": desc}
    if default is not None:
        s["default"] = default
    return s


def Num(lo, hi, desc):
    return {"type": "number", "minimum": lo, "maximum": hi, "description": desc}


def Str(maxlen, desc, minlen=1, pattern=None):
    s = {"type": "string", "minLength": minlen, "maxLength": maxlen, "description": desc}
    if pattern:
        s["pattern"] = pattern
    return s


def Bool(desc):
    return {"type": "boolean", "description": desc}


def Enum(values, desc, default=None):
    s = {"type": "string", "enum": list(values), "description": desc}
    if default is not None:
        s["default"] = default
    return s


def Arr(item, maxn, desc, minn=0):
    return {"type": "array", "items": item, "minItems": minn, "maxItems": maxn, "description": desc}


BM_ID = Str(64, "Bookmark id as returned by bm_search / bm_triage", pattern=r"^[A-Za-z0-9_.:][A-Za-z0-9_.:-]*$")
TAG = Str(64, "A tag (no leading '-')", pattern=r"^[^\s\-][^\r\n]*$")
FOLDER = Str(300, "Folder path like 'Work/Docs' ('/' separates levels)", minlen=0)
PLAN_ID = Str(18, "plan_id returned by a plan tool", pattern=r"^p-[0-9a-f]{16}$")


def out(required: dict, optional: Optional[dict] = None) -> dict:
    """A deliberately loose outputSchema: only the keys the CLI always prints, and their types; extra keys are allowed."""
    props = {k: ({"type": t} if isinstance(t, str) else {"type": list(t)}) for k, t in {**required, **(optional or {})}.items()}
    return {"type": "object", "properties": props, "required": sorted(required)}


@dataclass
class Prepared:
    argv: list
    post: Optional[Callable] = None            # (output_dict) -> output_dict
    cleanup: list = field(default_factory=list)   # always run after the call (temp copies, reserved file names)
    on_fail: list = field(default_factory=list)   # run only when the call did not succeed (a plan file nobody can apply)
    precheck: Optional[Callable] = None        # () -> None, may raise ToolError; run while the write lock is held, just before the command starts


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    properties: dict
    build: Callable                            # (args, ctx) -> Prepared
    required: tuple = ()
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = False
    timeout: float = 120.0
    output: Optional[dict] = None
    network: bool = False
    ok_exit: tuple = (0,)                      # exit codes that still carry a complete JSON answer (doctor exits 2 when a check failed)
    meta: dict = field(default_factory=dict)

    @property
    def input_schema(self) -> dict:
        s = {"type": "object", "properties": self.properties, "additionalProperties": False}
        if self.required:
            s["required"] = list(self.required)
        return s

    @property
    def writes(self) -> bool:
        return not self.read_only

    def annotations(self) -> dict:
        a = {"title": self.title, "readOnlyHint": self.read_only, "openWorldHint": self.open_world}
        if not self.read_only:
            a.update(destructiveHint=self.destructive, idempotentHint=self.idempotent)
        return a


# ------------------------------------------------------------------ command-line builders
def _s(v) -> str:
    return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)


def cli(*words, opts=(), pos=None, pos_multi=None):
    """``opts``: (argument, flag, kind) with kind ``val`` (``--flag=value``), ``switch`` or ``multi`` (``--flag v1 v2``)."""

    def build(args, ctx):
        argv = list(words)
        for name, flag, kind in opts:
            v = args.get(name)
            if v is None:
                continue
            if kind == "switch":
                if v is True:
                    argv.append(flag)
            elif kind == "val":
                argv.append(f"{flag}={_s(v)}")
            elif kind == "multi" and v:
                argv += [flag, *map(_s, v)]
        if pos and pos in args:
            argv += ["--", _s(args[pos])]
        if pos_multi and args.get(pos_multi):
            argv += ["--", *map(_s, args[pos_multi])]
        return Prepared(argv)
    return build


def plan_tool(command: str, opts=(), *, kind: str):
    """A command with ``--plan-out``: the answer is a ``plan_id`` for ``bm_apply_plan`` instead of a changed library."""

    def build(args, ctx):
        base = cli(command, opts=opts)(args, ctx)
        revision = ctx.revision()
        plan_id, path = ctx.plans.new()
        base.argv.append(f"--plan-out={path}")

        def post(output: dict) -> dict:
            return finish_plan(ctx, plan_id, path, kind, revision, output)
        base.post = post
        base.on_fail.append(lambda: (_unlink(path), ctx.plans.drop(plan_id)))
        return base
    return build


def finish_plan(ctx, plan_id: str, path: str, kind: str, revision: int, output: dict, plan: Optional[dict] = None) -> dict:
    import json
    try:
        if plan is None:
            plan = json.loads(Path(path).read_text(encoding="utf-8"))
        else:
            Path(path).write_text(json.dumps(plan), encoding="utf-8")
    except (OSError, ValueError):
        plan = {}
    summary = {k: v for k, v in output.items() if k not in ("plan_file", "plan")}
    if not plan:
        _unlink(path)
        return {"plan_id": None, "entries": 0, "summary": summary, "sample": [], "note": "nothing to change"}
    ctx.plans.save_meta(plan_id, {"kind": kind, "revision": revision, "entries": len(plan)})
    sample = [dict(id=i, **{k: v for k, v in (e if isinstance(e, dict) else {}).items() if k in ("folder", "tags", "title", "delete", "reason", "confidence")})
              for i, e in list(plan.items())[:10]]
    return {"plan_id": plan_id, "entries": len(plan), "summary": summary, "sample": sample,
            "note": "nothing has been changed yet; call bm_apply_plan with this plan_id to apply exactly these entries (undoable with bm_undo)"}


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def build_prune(args, ctx):
    """prune has no --plan-out: the preview rows become the plan, so what is applied is exactly what was listed."""
    base = cli("prune", opts=[("status", "--status", "multi")])(args, ctx)
    revision = ctx.revision()
    plan_id, path = ctx.plans.new()

    def post(output: dict) -> dict:
        matches = output.get("matches", [])
        plan = {m["id"]: {"delete": True, "reason": f"{m.get('status', 'dead')} link"} for m in matches}
        res = finish_plan(ctx, plan_id, path, "prune", revision, {"count": output.get("count", len(matches))}, plan)
        if output.get("count", 0) > len(matches):
            res["note"] += f". Only the first {len(matches)} of {output['count']} matches are included; run bm_prune again after applying for the rest"
        res["sample"] = [{k: m.get(k) for k in ("id", "title", "url", "status")} for m in matches[:10]]
        return res
    base.post = post
    return base


def build_apply_plan(args, ctx):
    path, meta = ctx.plans.load(args["plan_id"])

    def check() -> None:
        now = ctx.revision()
        if now != meta.get("revision"):
            raise ToolError("the library changed since this plan was made (bookmarks, link checks, archive or the undo history moved on); "
                            "run the plan tool again so you review a current plan", kind="stale_plan")
    check()                                                      # fail fast ...
    base = cli("apply-plan", opts=[("max_depth", "--max-depth", "val")])(args, ctx)
    base.argv[1:1] = ["--apply"]
    base.argv += ["--", path]
    base.precheck = check                                        # ... and again under the write lock, where no other write can slip in
    plan_id = args["plan_id"]

    def post(output: dict) -> dict:
        ctx.plans.drop(plan_id)
        output["plan_id"] = plan_id
        output["applied"] = True
        return output
    base.post = post
    return base


def _read_all(fd: int, limit: int) -> bytes:
    chunks, total = [], 0
    with os.fdopen(fd, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise PathError(f"the file is larger than the {limit // (1 << 20)} MiB limit")
            chunks.append(chunk)
    return b"".join(chunks)


def build_import_file(args, ctx):
    fd, _size = ctx.policy.open_for_read(args["path"], MAX_IMPORT_BYTES)
    data = _read_all(fd, MAX_IMPORT_BYTES)
    suffix = re.sub(r"[^a-z0-9.]", "", os.path.splitext(args["path"])[1].lower())[:8]
    ctx.tmp.mkdir(parents=True, exist_ok=True)
    tmp = ctx.tmp / f"imp-{secrets.token_hex(6)}{suffix}"
    tmp.write_bytes(data)                                        # one read through the verified descriptor; the CLI only sees this copy
    base = cli("import", opts=[("format", "--format", "val"), ("skip_existing", "--skip-existing", "switch")])(args, ctx)
    base.argv += ["--", str(tmp)]
    base.cleanup.append(lambda: _unlink(str(tmp)))
    shown = args["path"]
    base.post = lambda output: dict(output, source=shown)
    return base


def build_export(args, ctx):
    fd, real = ctx.policy.create_new(args["out"])                # reserves the name: O_EXCL, inside the allowed roots, never overwrites
    ctx.tmp.mkdir(parents=True, exist_ok=True)
    tmp = ctx.tmp / f"exp-{secrets.token_hex(6)}"
    state = {"done": False}
    base = cli("export", opts=[("format", "--format", "val"), ("folder", "--folder", "val"), ("tag", "--tag", "val")])(args, ctx)
    base.argv.append(f"--out={tmp}")

    def post(output: dict) -> dict:
        try:
            data = tmp.read_bytes()
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        state["done"] = True
        return {"written": output.get("written", 0), "out": real, "bytes": len(data)}

    def cleanup():
        if not state["done"]:
            try:
                os.close(fd)
            except OSError:
                pass
            _unlink(real)                                        # do not leave an empty reservation behind
        _unlink(str(tmp))
    base.post = post
    base.cleanup.append(cleanup)
    return base


# ------------------------------------------------------------------ the tools
LIMIT_100 = Int(1, 100, "Maximum rows to return (1-100)")
SHOW = lambda default: Int(1, 100, "How many sample rows to include (1-100)", default)  # noqa: E731

TOOLS: list = [
    # ---------------- read-only
    Tool("bm_stats", "Library health report", "Totals, unfiled, duplicates, link status, top folders/domains/tags. Read-only.", {},
         cli("stats"), read_only=True, idempotent=True, timeout=60,
         output=out({"total": "integer", "unfiled": "integer", "duplicate_groups": "integer", "status": "object"},
                    {"untagged": "integer", "untitled": "integer", "archived": "integer", "empty_folders": "array", "top_folders": "array",
                     "top_domains": "array", "top_tags": "array", "max_folder_depth": "integer", "error_titles": "array", "duplicate_extras": "integer"})),
    Tool("bm_search", "Search bookmarks",
         'Full-text search over titles, URLs, folders, tags, notes and archived page text. Syntax: words, "phrase", -exclude, tag:x folder:X '
         "site:host status:dead is:unfiled before:/after:YYYY-MM-DD. Read-only. Results contain untrusted page text (see server instructions).",
         {"query": Str(500, "Search query"), "limit": dict(Int(1, 100, "Maximum results (1-100)", 20)), "sort": Enum(["relevance", "recent"], "Order", "relevance")},
         cli("search", opts=[("limit", "--limit", "val"), ("sort", "--sort", "val")], pos="query"), required=("query",), read_only=True, idempotent=True,
         timeout=60, output=out({"results": "array"})),
    Tool("bm_folders", "List folders", "Folder paths with their bookmark counts. Read-only.", {}, cli("folders"), read_only=True, idempotent=True, timeout=60,
         output=out({"folders": "object"})),
    Tool("bm_tags", "List tags", "Tags with their usage counts. Read-only.", {"min_count": Int(1, 1000, "Only tags used at least this often", 1)},
         cli("tags", opts=[("min_count", "--min", "val")]), read_only=True, idempotent=True, timeout=60, output=out({"tags": "object"})),
    Tool("bm_history", "Undoable operations", "Recent operations that bm_undo can revert, newest first. Read-only.", {"limit": dict(Int(1, 100, "How many (1-100)", 20))},
         cli("history", opts=[("limit", "--limit", "val")]), read_only=True, idempotent=True, timeout=60, output=out({"operations": "array"})),
    Tool("bm_reading_list", "Reading list", "Bookmarks marked unread/reading, oldest first. Read-only.",
         {"which": Enum(["unread", "reading", "all"], "Which states", "all"), "newest_first": Bool("Newest first"), "limit": dict(Int(1, 100, "Maximum rows", 25))},
         cli("reading-list", opts=[("which", "--which", "val"), ("newest_first", "--newest", "switch"), ("limit", "--limit", "val")]),
         read_only=True, idempotent=True, timeout=60, output=out({"items": "array"})),
    Tool("bm_triage", "Group unfiled bookmarks",
         "Group unfiled bookmarks (same site / saved together) and show one filing suggestion per group. Read-only; use bm_organize to get an applicable plan.",
         {"limit": dict(SHOW(20))}, cli("triage", opts=[("limit", "--show", "val")]), read_only=True, idempotent=True, timeout=600,
         output=out({"unfiled": "integer", "decisions": "integer", "clusters": "array"}, {"multi_clusters": "integer"})),
    Tool("bm_feedback", "Suggestion feedback", "How filing suggestions fared: accepted / corrected / rejected. Read-only.",
         {"action": Enum(["stats", "list"], "stats = summary, list = recent decisions", "stats"), "limit": dict(Int(1, 100, "Rows for list", 30))},
         cli("feedback", opts=[("limit", "--limit", "val")], pos="action"), read_only=True, idempotent=True, timeout=60, output=out({})),
    Tool("bm_rules", "Filing rules (read)", "List your own filing rules, test a URL against them, or suggest rules from your filing habits. Read-only.",
         {"action": Enum(["list", "test", "suggest"], "What to do", "list"), "url": Str(2000, "URL for action=test"), "title": Str(300, "Optional title for action=test")},
         lambda a, c: _rules_build(a, c), read_only=True, idempotent=True, timeout=60, output=out({})),
    Tool("bm_browsers", "Detect browsers", "Which installed browsers have a readable bookmark store on this machine. Read-only.", {}, cli("browsers"),
         read_only=True, idempotent=True, timeout=60, output=out({"browsers": "object"})),
    Tool("bm_backups", "List snapshots", "Automatic snapshots taken before changes. Read-only.", {}, cli("backups"), read_only=True, idempotent=True, timeout=60,
         output=out({"snapshots": "array"})),
    Tool("bm_doctor", "Diagnose installation", "Checks Python, SQLite, the library, the search index and network settings. Read-only (never repairs).", {}, cli("doctor"),
         read_only=True, idempotent=True, timeout=120, ok_exit=(0, 2), output=out({"checks": "array", "summary": "object"}, {"version": "string"})),
    Tool("bm_learn", "Filing accuracy report", "Train on your filed bookmarks and report how accurate automatic filing would be. Can take minutes on big libraries.",
         {"seeds": Int(1, 5, "Repeat on N different splits (slower, more reliable)", 1)}, cli("learn", opts=[("seeds", "--seeds", "val")]),
         read_only=True, idempotent=True, timeout=1800, output=out({"ready": "boolean"}, {"mode": "string"})),
    # ---------------- plan producers (write only a plan file + suggestion records, never bookmarks)
    Tool("bm_organize", "Propose filing and tags",
         "Propose folders and tags for unfiled bookmarks from the folders you already use plus taxonomy rules. Changes nothing: returns a plan_id for bm_apply_plan.",
         {"refile": Bool("Also reconsider filed bookmarks"), "no_tags": Bool("Do not propose tags"), "precision": Num(0.5, 0.99, "Answer only when at least this likely to be right"),
          "allow_new_folders": Bool("Let rules create folders that do not exist yet")},
         plan_tool("organize", [("refile", "--refile", "switch"), ("no_tags", "--no-tags", "switch"), ("precision", "--precision", "val"),
                                ("allow_new_folders", "--allow-new-folders", "switch")], kind="organize"),
         timeout=1800, output=out({"plan_id": ("string", "null"), "entries": "integer", "summary": "object", "sample": "array"}, {"note": "string"})),
    Tool("bm_dedupe", "Propose duplicate merges", "Find duplicate bookmarks (exact or variant URLs). Changes nothing: returns a plan_id for bm_apply_plan.",
         {"level": Enum(["exact", "variant"], "exact = same URL, variant = mobile/AMP/tracking variants", "exact"), "same_folder_only": Bool("Only merge copies in the same folder")},
         plan_tool("dedupe", [("level", "--level", "val"), ("same_folder_only", "--same-folder-only", "switch")], kind="dedupe"),
         timeout=300, output=out({"plan_id": ("string", "null"), "entries": "integer", "summary": "object", "sample": "array"}, {"note": "string"})),
    Tool("bm_restructure", "Propose folder tree changes", "Propose splitting big folders, folding tiny ones, flattening deep ones. Changes nothing: returns a plan_id.",
         {"max_size": Int(5, 1000, "Split folders bigger than this", 40), "min_size": Int(1, 50, "Fold folders smaller than this", 2), "max_depth": Int(1, 20, "Also flatten deeper than N")},
         plan_tool("restructure", [("max_size", "--max-size", "val"), ("min_size", "--min-size", "val"), ("max_depth", "--max-depth", "val")], kind="restructure"),
         timeout=600, output=out({"plan_id": ("string", "null"), "entries": "integer", "summary": "object", "sample": "array"}, {"note": "string"})),
    Tool("bm_autotag", "Propose tags", "Propose tags from page text and titles. Changes nothing: returns a plan_id.",
         {"max_tags": Int(1, 10, "Tags per bookmark", 3), "all": Bool("Also bookmarks that already have tags")},
         plan_tool("autotag", [("max_tags", "--max", "val"), ("all", "--all", "switch")], kind="autotag"), timeout=600,
         output=out({"plan_id": ("string", "null"), "entries": "integer", "summary": "object", "sample": "array"}, {"note": "string"})),
    Tool("bm_titles", "Propose title clean-up", "Propose cleaner titles (strip site suffixes, counters). Changes nothing: returns a plan_id.",
         {"keep_counters": Bool("Keep '(3)' style counters"), "no_brand": Bool("Keep site names")},
         plan_tool("titles", [("keep_counters", "--keep-counters", "switch"), ("no_brand", "--no-brand", "switch")], kind="titles"), timeout=300,
         output=out({"plan_id": ("string", "null"), "entries": "integer", "summary": "object", "sample": "array"}, {"note": "string"})),
    Tool("bm_prune", "Propose removing dead links", "List bookmarks whose links were confirmed dead (or suspect/restricted). Changes nothing: returns a plan_id.",
         {"status": dict(Arr(Enum(["dead", "suspect", "restricted"], "Link status"), 3, "Which statuses to remove", 1), default=["dead"])},
         build_prune, timeout=120, output=out({"plan_id": ("string", "null"), "entries": "integer", "summary": "object", "sample": "array"}, {"note": "string"})),
    Tool("bm_apply_plan", "Apply a reviewed plan",
         "Apply exactly the plan a plan tool produced (moves, tags, titles, deletions). Fails if the library changed since the plan was made. "
         "Every apply is snapshotted and journaled; revert it with bm_undo.",
         {"plan_id": PLAN_ID, "max_depth": Int(1, 20, "Reject folders deeper than N")}, build_apply_plan, required=("plan_id",), destructive=True, timeout=600,
         output=out({"valid": "integer", "rejected": "array", "deleted": "integer", "plan_id": "string", "applied": "boolean"}, {"feedback": "object"})),
    # ---------------- direct edits (additive or small, journaled)
    Tool("bm_import_file", "Import a bookmark file",
         "Import an exported bookmark file (HTML, Chrome/Firefox JSON, CSV, JSON, Pocket, Raindrop ...) from an allowed directory. Adds bookmarks; skips exact repeats.",
         {"path": Str(4096, "File path (inside the directories the server allows)"), "format": Enum(IMPORT_FORMATS, "Force a format (default: detect)"),
          "skip_existing": Bool("Also skip URLs already present in other folders")},
         build_import_file, required=("path",), idempotent=True, timeout=600,
         output=out({"source": "string", "read": "integer", "imported": "integer", "library": "integer"}, {"skipped_existing": "integer", "skipped_items": "array"})),
    Tool("bm_import_browser", "Import from a browser", "Read the bookmarks of an installed browser's default profile on this machine and add them. Reads the browser's own store.",
         {"name": Enum(BROWSERS, "Browser"), "skip_existing": Bool("Also skip URLs already present in other folders")},
         cli("import-browser", opts=[("skip_existing", "--skip-existing", "switch")], pos="name"), required=("name",), idempotent=True, timeout=300,
         output=out({"source": "string", "read": "integer", "imported": "integer", "library": "integer"}, {"skipped_existing": "integer", "skipped_items": "array"})),
    Tool("bm_tag", "Edit one bookmark", "Add/remove tags, move to a folder, or retitle ONE bookmark (undoable).",
         {"id": BM_ID, "add": Arr(TAG, 20, "Tags to add"), "remove": Arr(TAG, 20, "Tags to remove"), "folder": FOLDER, "title": Str(500, "New title")},
         cli("tag", opts=[("folder", "--folder", "val"), ("title", "--title", "val"), ("add", "--add", "multi"), ("remove", "--remove", "multi")], pos="id"),
         required=("id",), destructive=True, idempotent=True, timeout=60, output=out({"id": "string", "folder": "string", "tags": "array"}, {"title": "string", "url": "string"})),
    Tool("bm_mark", "Set reading state", "Mark bookmarks unread/reading/read or favourite them (undoable).",
         {"ids": Arr(BM_ID, 50, "Bookmark ids", 1), "state": Enum(["unread", "reading", "read", "none"], "Reading state"), "favorite": Bool("Mark as favourite"),
          "unfavorite": Bool("Remove the favourite flag")},
         cli("mark", opts=[("state", "--state", "val"), ("favorite", "--favorite", "switch"), ("unfavorite", "--unfavorite", "switch")], pos_multi="ids"),
         required=("ids",), idempotent=True, timeout=60, output=out({"updated": "array"})),
    Tool("bm_rules_edit", "Filing rules (write)", "Add or remove one of your own filing rules (host/URL prefix/title regex/keyword -> folder).",
         {"action": Enum(["add", "remove"], "add or remove"), "host": Str(253, "Match this host"), "url_prefix": Str(2000, "Match URLs starting with this"),
          "keyword": Str(100, "Match this keyword"), "folder": FOLDER, "tags": Arr(TAG, 10, "Tags to add"), "rule_id": Int(1, 100000, "Rule number for action=remove")},
         lambda a, c: _rules_edit_build(a, c), required=("action",), destructive=True, timeout=60, output=out({})),
    # ---------------- network
    Tool("bm_check_links", "Check links", "Test whether links still work (retries, per-host throttling, multi-run confirmation). Contacts the bookmarked sites. "
         "wayback=true also sends each failing URL to archive.org.",
         {"limit": dict(Int(1, 500, "Maximum bookmarks to check this call (1-500)", 50)), "ids": Arr(BM_ID, 100, "Only these bookmarks"),
          "recheck_suspect": Bool("Only re-test links marked suspect"), "wayback": Bool("Look up archive.org copies of failing links (shares the URL with archive.org)")},
         cli("check", opts=[("limit", "--limit", "val"), ("recheck_suspect", "--recheck-suspect", "switch"), ("wayback", "--wayback", "switch"), ("ids", "--ids", "multi")]),
         open_world=True, network=True, timeout=1800, output=out({"checked": "integer", "status": "object", "offline": "boolean"}, {"redirects_rewritten": "integer"})),
    Tool("bm_archive", "Archive pages", "Fetch pages and keep a sanitised copy plus searchable text. Contacts the bookmarked sites.",
         {"limit": dict(Int(1, 200, "Maximum pages this call (1-200)", 25)), "ids": Arr(BM_ID, 100, "Only these bookmarks"), "text_only": Bool("Index text, keep no snapshot file"),
          "no_assets": Bool("Do not inline images/styles")},
         cli("archive", opts=[("limit", "--limit", "val"), ("text_only", "--text-only", "switch"), ("no_assets", "--no-assets", "switch"), ("ids", "--ids", "multi")]),
         open_world=True, network=True, timeout=1800, output=out({"indexed": "integer", "skipped": "integer"}, {"errors": "array"})),
    # ---------------- file output / undo
    Tool("bm_export", "Export bookmarks to a new file", "Write the library to a NEW file in an allowed directory (html, chrome, csv, json, md ...). Never overwrites.",
         {"format": Enum(EXPORT_FORMATS, "Output format"), "out": Str(4096, "New file path inside an allowed directory"), "folder": FOLDER, "tag": TAG},
         build_export, required=("format", "out"), timeout=300, output=out({"written": "integer", "out": "string", "bytes": "integer"})),
    Tool("bm_undo", "Undo an operation", "Revert one operation listed by bm_history (pass its number). Removes what that operation added and restores what it changed.",
         {"op": Int(1, 10**9, "Operation number from bm_history")}, cli("undo", opts=[("op", "--op", "val")]), required=("op",), destructive=True, timeout=120,
         output=out({"op": "integer", "command": "string", "restored": "integer", "removed": "integer"})),
]


def _rules_build(args, ctx):
    action = args.get("action", "list")
    if action == "test" and not args.get("url"):
        raise ToolError("action=test needs a url")
    argv = ["rules", action]
    if action == "test":
        argv += [f"--url={args['url']}"] + ([f"--title={args['title']}"] if args.get("title") else [])
    return Prepared(argv)


def _rules_edit_build(args, ctx):
    action = args["action"]
    if action == "remove":
        if "rule_id" not in args:
            raise ToolError("action=remove needs rule_id (see bm_rules)")
        return Prepared(["rules", "remove", f"--id={args['rule_id']}"])
    given = [(k, args[k]) for k in ("host", "url_prefix", "keyword") if args.get(k)]
    if len(given) != 1 or not args.get("folder"):
        raise ToolError("action=add needs exactly one of host / url_prefix / keyword, plus folder")
    argv = ["rules", "add", f"--{given[0][0].replace('_', '-')}={given[0][1]}", f"--folder={args['folder']}"]
    if args.get("tags"):
        argv += ["--tags", *args["tags"]]
    return Prepared(argv)


BY_NAME = {t.name: t for t in TOOLS}


def selfcheck() -> None:
    """Fail loudly (at start-up and in the tests) if a tool definition breaks the conventions above."""
    names = set()
    for t in TOOLS:
        assert t.name not in names and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", t.name), t.name
        names.add(t.name)
        schema = t.input_schema
        validate.check_schema(schema)
        assert all(re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", p) for p in schema["properties"]), t.name
        _walk(t.name, schema)
        if t.output:
            validate.check_schema(t.output)
        assert len(t.description) <= 700, (t.name, len(t.description))
        assert not (t.read_only and (t.destructive or t.network)), t.name
        assert not (t.open_world and not t.network), t.name


def _walk(name: str, s) -> None:
    if not isinstance(s, dict):
        return
    if s.get("type") == "string":
        assert "maxLength" in s or "enum" in s, f"{name}: unbounded string {s}"
    if s.get("type") in ("integer", "number"):
        assert "minimum" in s and "maximum" in s, f"{name}: unbounded number {s}"
    if s.get("type") == "array":
        assert "maxItems" in s, f"{name}: unbounded array {s}"
        _walk(name, s.get("items"))
    if s.get("type") == "object":
        assert s.get("additionalProperties") is False, f"{name}: object without additionalProperties:false"
        for sub in (s.get("properties") or {}).values():
            _walk(name, sub)
