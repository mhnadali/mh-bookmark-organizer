"""Static verification of tools.json against the REAL argparse parser of the repository (read-only import).

  1. every CLI command (48) is covered by a decision in `coverage`
  2. input schemas are well-formed, use only the keyword subset the reference validator supports, and have no top-level
     combinators (the Claude API rejects oneOf/anyOf/allOf at the top level of a tool schema - recalled, unverified)
  3. every argv spec, for every variant and flag combination below, parses with cli.build_parser()
  4. annotations are consistent with the read-only set / apply property / group
  5. every example's arguments validate against its inputSchema

Run: PYTHONPATH=<repo scripts> python3 verify_tools.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, "/home/user/adding-skills/.claude/skills/mh-bookmark-organizer/scripts")
import catalog  # noqa: E402
import mcp_core as core  # noqa: E402
from mhbm import cli  # noqa: E402

DOC = json.load(open(os.path.join(HERE, "..", "tools.json"), encoding="utf-8"))
TOOLS = {t["name"]: t for t in DOC["tools"]}
PARSER = cli.build_parser()
ALLOWED_KW = {"type", "properties", "required", "additionalProperties", "enum", "minimum", "maximum", "minLength", "maxLength", "pattern",
              "items", "minItems", "maxItems", "uniqueItems", "default", "description", "maxProperties", "propertyNames", "const", "title"}
fails: list = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("FAIL:", msg)


def parse(argv):
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            return PARSER.parse_args(["--home", "/x", "--json", "--lang", "en"] + argv)
    except SystemExit:
        return err.getvalue().strip().splitlines()[-1] if err.getvalue() else "exit"


# 1 ---------------------------------------------------------------------------------------------- coverage
sub = next(a for a in PARSER._actions if a.__class__.__name__ == "_SubParsersAction")
cmds = list(sub.choices)
missing = catalog.check_coverage(cmds)
check(not missing, f"CLI commands without a coverage decision: {missing}")
print(f"[1] {len(cmds)} CLI commands, {len(DOC['coverage'])} coverage rows, missing={missing}")
exposed_tools = {r["tool"] for r in DOC["coverage"] if r["tool"]}
check(exposed_tools <= set(TOOLS), f"coverage names unknown tools: {exposed_tools - set(TOOLS)}")
unreferenced = set(TOOLS) - exposed_tools - {"bm_get"}
check(not unreferenced, f"tools not referenced by any coverage row: {unreferenced}")


# 2 ---------------------------------------------------------------------------------------------- schema shape
def walk(schema, where):
    for k in schema:
        check(k in ALLOWED_KW, f"{where}: unsupported keyword {k}")
    if "pattern" in schema:
        try:
            re.compile(schema["pattern"])
        except re.error as e:
            check(False, f"{where}: bad pattern {schema['pattern']!r}: {e}")
    for k, sub_ in schema.get("properties", {}).items():
        walk(sub_, f"{where}.{k}")
    if isinstance(schema.get("items"), dict):
        walk(schema["items"], f"{where}[]")
    if isinstance(schema.get("additionalProperties"), dict):
        walk(schema["additionalProperties"], f"{where}{{}}")
    if "propertyNames" in schema:
        walk(schema["propertyNames"], f"{where}<names>")


for name, t in TOOLS.items():
    s = t["inputSchema"]
    check(s.get("type") == "object" and s.get("additionalProperties") is False, f"{name}: inputSchema must be a closed object")
    for banned in ("anyOf", "oneOf", "allOf", "if", "not", "$ref"):
        check(banned not in s, f"{name}: top-level {banned}")
    walk(s, name)
    check(all(r in s["properties"] for r in s.get("required", [])), f"{name}: required names missing from properties")
    check(len(t["description"].split()) <= 170, f"{name}: description too long ({len(t['description'].split())} words)")
    check(re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name) is not None, f"{name}: bad tool name")
    for k, v in s["properties"].items():
        if v.get("type") == "string":
            check("maxLength" in v or "enum" in v, f"{name}.{k}: unbounded string")
        if v.get("type") == "array":
            check("maxItems" in v, f"{name}.{k}: unbounded array")
        if v.get("type") == "integer":
            check("minimum" in v and "maximum" in v, f"{name}.{k}: unbounded integer")
print(f"[2] {len(TOOLS)} tool schemas checked")

# 4 ---------------------------------------------------------------------------------------------- annotations
for name, t in TOOLS.items():
    a, s = t["annotations"], t["inputSchema"]["properties"]
    ro = t.get("read_only_set", False)
    proposer = t["group"] in ("propose", "admin")
    check(a["readOnlyHint"] == (ro or proposer), f"{name}: readOnlyHint {a['readOnlyHint']} vs read_only_set {ro} / proposer {proposer}")
    check(not (proposer and ro), f"{name}: propose tools are not in the --read-only set (they keep a preview cache in <home>/mcp)")
    check("apply" not in s and "confirm_deletes" not in s and "confirm_replace" not in s, f"{name}: write flags belong to bm_apply only")
    if proposer:
        check(a["idempotentHint"] and not a["openWorldHint"], f"{name}: proposals are idempotent and closed-world")
        check("Proposes only" in t["description"], f"{name}: description must say it proposes only")
        check("preview_id" in json.dumps(t["outputSchema"]), f"{name}: output schema must carry preview_id")
    if name == "bm_apply":
        check(t.get("_meta") == {"anthropic/requiresUserInteraction": True} and a["destructiveHint"] and not a["readOnlyHint"], "bm_apply must be destructive and flagged requiresUserInteraction")
    if name in ("bm_triage_decide", "bm_undo"):
        check(t.get("_meta") == {"anthropic/requiresUserInteraction": True}, f"{name} must be flagged requiresUserInteraction")
    if not ro and name not in ("bm_dashboard", "bm_review_page", "bm_export") and not proposer:
        check("risk" in t, f"{name}: missing entry in the risk register")
    check(len(t["description"]) <= 2000, f"{name}: description {len(t['description'])} chars (Claude Code before v2.1.296 cut upfront descriptions at 2,048)")
    if t["group"] == "net":
        check(a["openWorldHint"] and t["network"]["uses"], f"{name}: net group must be openWorld")
    else:
        check(not a["openWorldHint"], f"{name}: only net tools are openWorld")
    if t["fs"]["path_args"]:
        check(t["group"] in ("files", "edit", "propose"), f"{name}: path args in group {t['group']}")
    for k in ("mutation", "output", "slow", "network", "fs", "impl", "outputSchema", "examples", "annotations"):
        check(k in t, f"{name}: missing {k}")
    if not ro:
        check(t["mutation"]["journal"] != "n/a" or name == "bm_undo", f"{name}: state the journal story")
check(len(DOC["server"]["instructions"]) <= 4096, "server instructions longer than Claude Code's 4,096 character limit")
print("[4] annotations consistent;", sum(1 for t in TOOLS.values() if t.get("read_only_set")), "tools in the read-only set")

# 3 ---------------------------------------------------------------------------------------------- argv parses
D = {"limit": 20, "offset": 0}
SAMPLES = {
    "bm_stats": [{}],
    "bm_search": [{"query": "-x --limit", "sort": "recent", "limit": 20, "offset": 5, "_query": "-x --limit tag:\"a b\""}],
    "bm_browse:folders": [{}], "bm_browse:tags": [{"min_count": 2}],
    "bm_reading_list": [{"which": "unread", "newest": True, "limit": 5}],
    "bm_history:operations": [{"limit": 5}], "bm_history:snapshots": [{}],
    "bm_browsers": [{}],
    "bm_rules:list": [{}], "bm_rules:suggest": [{"limit": 5}], "bm_rules:test": [{"url": "https://a.example/", "title": "-t"}],
    "bm_synonyms:list": [{}], "bm_synonyms:suggest_tag_merges": [{"limit": 5}],
    "bm_feedback:stats": [{}], "bm_feedback:list": [{"limit": 5}],
    "bm_triage": [{"limit": 10}],
    "bm_ai_batch": [{"limit": 25, "offset": 0, "refile": True}],
    "bm_learn": [{"seeds": 3, "study": "none", "retrain": True}, {"seeds": 1, "study": "curve"}, {"seeds": 1, "study": "text_effect"}],
    "bm_doctor": [{}], "bm_config": [{}],
    "bm_tag": [{"id": "abc123", "add": ["a", "b"], "remove": ["c"], "folder": "-X/Y ", "unfile": False, "title": "-t"},
               {"id": "abc123", "unfile": True}],
    "bm_mark": [{"ids": ["a1", "b2"], "state": "unread", "favorite": True}, {"ids": ["a1"], "favorite": False}],
    "bm_import_file": [{"paths": ["/tmp/-a.html", "/tmp/b.json"], "format": "html", "skip_existing": True, "allow_duplicates": True}],
    "bm_import_browser": [{"browser": "chrome", "_profile_path": "/tmp/x/Bookmarks", "skip_existing": True, "allow_duplicates": True}],
    "bm_undo": [{"op": 3}],
    "bm_rules_edit:add": [{"kind": "host", "value": "-x.com", "folder": "A/B", "tags": ["t1", "t2"]}, {"kind": "url_prefix", "value": "https://x/", "folder": "F"}, {"kind": "keyword", "value": "kw", "folder": "F"}],
    "bm_rules_edit:remove": [{"id": 4}],
    "bm_synonyms_edit:add": [{"kind": "tag", "_names": ["ml", "machine"]}, {"kind": "folder", "_names": ["Docs", "Documentation"]}],
    "bm_synonyms_edit:remove": [{"kind": "tag", "_canon": ["ml"]}, {"kind": "folder", "_canon": ["Docs"]}],
    "bm_check_links": [{"limit": 100, "due_days": 7, "recheck_suspect": True, "wayback": True, "ids": ["a1", "b2"]}],
    "bm_archive": [{"unarchived": True, "limit": 10, "text_only": True, "ids": ["a1"]}],
    "bm_export": [{"format": "html", "_out": "/tmp/x.html", "folder": "-F", "tag": "t"}, {"format": "obsidian", "_out": "/tmp/vault"}],
    "bm_dashboard": [{"_out": "/tmp/d.html"}, {}],
    "bm_review_page": [{"_out": "/tmp/r.html", "_plan_file": "/tmp/-p.json"}],
}
BULK_STEPS = {  # tool(:variant) -> (preview args, apply args)
    "bm_clean_urls": {}, "bm_dedupe": {"level": "variant", "same_folder_only": True, "fuzzy": True},
    "bm_prune": {}, "bm_organize": {"refile": True, "no_tags": True, "allow_new_folders": True, "coarse": True, "min_confidence": 0.9},
    "bm_restructure": {"max_size": 30, "min_size": 3, "max_depth": 4}, "bm_autotag": {"max_tags": 2, "min_df": 4, "all": True},
    "bm_titles": {"keep_counters": True, "no_brand": True}, "bm_canonical": {},
    "bm_stage_plan": {"max_depth": 4}, "bm_tags_edit:rename": {"old": "a", "new": "b"}, "bm_tags_edit:normalize": {},
    "bm_folders_edit:rename": {"old": "A/B ", "new": "A/C"}, "bm_folders_edit:prune_empty": {},
    "bm_restore_backup": {"snapshot": "20260101-000000-pre-x.json"}, "bm_feedback_clear": {}, "bm_doctor_fix": {},
}


def argv_for(spec, args):
    if spec and spec[0] == "<":
        return None
    resolved = []
    for tok in spec:
        if isinstance(tok, str):
            resolved.append(tok.replace("--plan-out=<plan>", "--plan-out=/tmp/plan.json").replace("<frozen plan file>", "/tmp/frozen.json").replace("<home>", "/x"))
        else:
            resolved.append(tok)
    return core.build_argv(resolved, {**D, **args})


n_parsed = 0
for key, samples in SAMPLES.items():
    name, _, variant = key.partition(":")
    impl = TOOLS[name]["impl"]
    spec = impl["variants"][variant]["argv"] if variant else impl["argv"]
    for s in samples:
        argv = argv_for(spec, s)
        r = parse(argv)
        check(not isinstance(r, str), f"{key}: {argv} -> {r}")
        n_parsed += 1
for key, args in BULK_STEPS.items():
    name, _, variant = key.partition(":")
    impl = TOOLS[name]["impl"]
    src = impl["variants"][variant] if variant else impl
    for which in ("preview_argv", "apply_argv"):
        spec = src[which]
        if not spec and which == "preview_argv":
            continue
        argv = argv_for(spec, args)
        r = parse(argv)
        check(not isinstance(r, str), f"{key} {which}: {argv} -> {r}")
        n_parsed += 1
print(f"[3] {n_parsed} argv vectors parse with the real parser")

# every argv-bearing tool has at least one sample above
for name, t in TOOLS.items():
    impl = t["impl"]
    if impl["kind"] in ("inproc", "dispatch"):
        continue
    has = any(k.split(":")[0] == name for k in list(SAMPLES) + list(BULK_STEPS))
    check(has, f"{name}: no argv sample in verify_tools.py")

# 5 ---------------------------------------------------------------------------------------------- examples validate
n_ex = 0
for name, t in TOOLS.items():
    for ex in t["examples"]:
        a = ex["arguments"]
        if isinstance(a, str):  # placeholder resolved by fill_examples.py
            continue
        a2 = {k: (["0123456789ab"] if (isinstance(v, list) and v == ["$ID_DOVETAIL"]) else v) for k, v in a.items()}
        if a2.get("id") == "$ID_DOVETAIL":
            a2["id"] = "0123456789ab"
        errs = core.validate(t["inputSchema"], a2)
        check(not errs, f"{name} example {ex['title']!r}: {errs}")
        n_ex += 1
print(f"[5] {n_ex} example argument sets validate")

print("\nRESULT:", "OK" if not fails else f"{len(fails)} FAILURES")
sys.exit(1 if fails else 0)
