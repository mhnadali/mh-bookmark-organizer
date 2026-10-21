"""Runs every example in tools.json through the reference server (real CLI, synthetic library) and stores the results in tools.json.

The network examples (bm_check_links, bm_archive) make 3-5 requests to github.com / pypi.org through the sandbox proxy.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WORK = ROOT / "work"
SC = ROOT / "example_homes"
sys.path.insert(0, str(HERE))
import refserver as rs  # noqa: E402

TOOLS_JSON = ROOT / "tools.json"
doc = json.loads(TOOLS_JSON.read_text(encoding="utf-8"))


def excerpt(o, items=3, s=140):
    if isinstance(o, dict):
        return {k: excerpt(v, items, s) for k, v in o.items()}
    if isinstance(o, list):
        out = [excerpt(v, items, s) for v in o[:items]]
        if len(o) > items:
            out.append(f"... +{len(o) - items} more")
        return out
    if isinstance(o, str) and len(o) > s:
        return o[: s - 1] + "…"
    return o


def home_for(lib, name):
    h = SC / name
    shutil.rmtree(h, ignore_errors=True)
    SC.mkdir(exist_ok=True)
    if lib == "empty":
        h.mkdir()
    else:
        shutil.copytree(WORK / {"unf": "lib_unf", "net": "lib_net"}[lib], h)
    return h


class Ctx:
    def __init__(self, tool, ex, idx):
        self.home = home_for(ex.get("library", "unf"), f"{tool['name']}-{idx}")
        extra = {"HOME": str(WORK / "fakehome")} if ex.get("env") == "fakehome" else {}
        groups = ("read", "edit", "propose", "apply", "net", "files", "admin")
        self.exchange = self.home / "exchange"
        self.s = rs.Server(self.home, allow_dirs=[self.exchange], groups=groups, extra_env=extra)
        self.n = 0

    def call(self, name, args):
        self.n += 1
        r = self.s.handle({"jsonrpc": "2.0", "id": self.n, "method": "tools/call", "params": {"name": name, "arguments": args}})
        return r["result"] if "result" in r else {"protocol_error": r["error"]}

    def data(self, name, args):
        r = self.call(name, args)
        assert not r.get("isError"), (name, args, r)
        return r["structuredContent"]

    def first_id(self, query="dovetail"):
        return self.data("bm_search", {"query": query, "limit": 1})["results"][0]["id"]

    def prep(self, name):
        if name == "mark_one":
            self.data("bm_mark", {"ids": [self.first_id()], "state": "unread"})
        elif name == "tag_one":
            self.data("bm_tag", {"id": self.first_id(), "add": ["example"]})
        elif name == "dead3":
            con = sqlite3.connect(self.home / "library.db")
            con.execute("UPDATE bookmarks SET status='dead', http_code=404, wayback='https://web.archive.org/web/2024/https://example.invalid/' WHERE id IN (SELECT id FROM bookmarks ORDER BY id LIMIT 3)")
            con.commit()
            con.close()
        elif name == "tags_oldtag":
            con = sqlite3.connect(self.home / "library.db")
            con.execute("UPDATE bookmarks SET tags='[\"oldtag\",\"keep\"]' WHERE id IN (SELECT id FROM bookmarks ORDER BY id LIMIT 6)")
            con.commit()
            con.close()
        elif name == "snapshot_then_delete":
            pre = self.data("bm_dedupe", {})
            self.data("bm_apply", {"preview_id": pre["preview_id"], "confirm": pre["confirm_text"]})

    def resolve(self, args, tool_name):
        if args == "$TRIAGE_DECIDE":
            cl = [c for c in self.data("bm_triage", {"limit": 5, "min_size": 2})["clusters"]][0]
            return {"decisions": [{"cluster": cl["id"], "action": "file", "exclude": [cl["members"][0]["id"]]}]}
        if args == "$UNDO_LATEST":
            return {"op": self.data("bm_history", {"limit": 1})["latest_undoable_op"]}
        if args == "$MERGE_PREVIEW":
            ids = [r["id"] for r in self.data("bm_search", {"query": "dovetail", "limit": 2})["results"]]
            return {"groups": [{"keep": ids[0], "remove": [ids[1]]}]}
        if args == "$INLINE_PLAN":
            ids = [r["id"] for r in self.data("bm_search", {"is": ["unfiled"], "limit": 2})["results"]]
            return {"plan": {ids[0]: {"folder": "Other bookmarks/Woodworking ", "tags": ["wood"]}, ids[1]: {"folder": "Reading/Later", "title": "Read me first"}}}
        if args == "$APPLY_DEDUPE":
            pre = self.data("bm_dedupe", {"level": "exact"})
            return {"preview_id": pre["preview_id"], "confirm": pre["confirm_text"]}
        if args == "$REVIEW_PREVIEW":
            pre = self.data("bm_organize", {})
            return {"preview_id": pre["preview_id"], "out": str(self.exchange / "review.html")}
        if args == "$RESTORE_PREVIEW":
            snap = self.data("bm_history", {"kind": "snapshots", "limit": 1})["snapshots"][0]["name"]
            return {"snapshot": snap}
        out = copy.deepcopy(args)
        for k, v in list(out.items()):
            if v == "$ID_DOVETAIL":
                out[k] = self.first_id()
            elif isinstance(v, list):
                out[k] = [self.first_id() if x == "$ID_DOVETAIL" else (str(self.exchange / x.split("/")[-1]) if isinstance(x, str) and x.startswith("$DATA/") else x) for x in v]
        return out


MISMATCH = []
summary = []
for tool in doc["tools"]:
    for idx, ex in enumerate(tool["examples"]):
        ctx = Ctx(tool, ex, idx)
        ctx.exchange.mkdir(exist_ok=True)
        for f in ("messy-1.html",):
            shutil.copy(WORK / "data" / f, ctx.exchange / f)
        if ex.get("prep"):
            ctx.prep(ex["prep"])
        args = ctx.resolve(ex["arguments"], tool["name"])
        t0 = time.time()
        res = ctx.call(tool["name"], args)
        took = time.time() - t0
        entry = {"title": ex["title"], "library": ex.get("library", "unf") + (" (synthetic HOME with 2 Chrome profiles + 1 Chromium)" if ex.get("env") == "fakehome" else ""),
                 "arguments": args, "seconds": round(took, 2)}
        if "protocol_error" in res:
            entry["protocol_error"] = res["protocol_error"]
        else:
            entry["is_error"] = res["isError"]
            entry["result_bytes"] = len(res["content"][0]["text"].encode())
            entry["result"] = excerpt(res["structuredContent"]) if not res["isError"] else res["content"][0]["text"]
            if not res["isError"]:
                import mcp_core as core
                errs = core.validate(tool["outputSchema"], res["structuredContent"])
                if errs:
                    MISMATCH.append((tool["name"], errs, sorted(res["structuredContent"])))
        if ex.get("two_step") and not res.get("isError") and res["structuredContent"].get("preview"):
            entry["proposal_id_note"] = "preview_id values differ on every run"
            ap = res["structuredContent"]["apply_with"]["arguments"]
            t1 = time.time()
            res2 = ctx.call("bm_apply", ap)
            entry["apply_arguments"] = excerpt(ap)
            entry["apply_result"] = excerpt(res2["structuredContent"]) if not res2["isError"] else res2["content"][0]["text"]
            entry["apply_seconds"] = round(time.time() - t1, 2)
            if not res2["isError"] and res2["structuredContent"].get("op"):
                und = ctx.call("bm_undo", {"op": res2["structuredContent"]["op"]})
                entry["undo_result"] = excerpt(und["structuredContent"])
        entry["verified"] = "ran against the unmodified CLI via refserver.py on a synthetic library"
        ex.clear()
        ex.update(entry)
        summary.append((tool["name"], idx, entry.get("is_error"), entry.get("result_bytes"), entry["seconds"]))
        print(f"{tool['name']:22s} {ex['title'][:46]:46s} err={entry.get('is_error')} bytes={entry.get('result_bytes')} {entry['seconds']}s")

TOOLS_JSON.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
print("examples written:", len(summary))
for m in MISMATCH:
    print("OUTPUT SCHEMA MISMATCH", m)
sys.exit(1 if MISMATCH else 0)
