"""Sizes and times of the MCP results on the 5,571-bookmark synthetic library (500 unfiled), numpy installed, cold model cache first."""
import json, os, shutil, sys, time
sys.dont_write_bytecode = True
from pathlib import Path
HERE = Path(__file__).resolve().parent; ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import refserver as rs
home = ROOT / "example_homes" / "big"
shutil.rmtree(home, ignore_errors=True); shutil.copytree(ROOT / "work" / "lib_big_unf", home)
(home / "model.json").unlink(missing_ok=True)
s = rs.Server(home)
n = [0]
def call(name, args):
    n[0] += 1
    t = time.time()
    r = s.handle({"jsonrpc": "2.0", "id": n[0], "method": "tools/call", "params": {"name": name, "arguments": args}})["result"]
    return r, time.time() - t
rows = []
for label, name, args in [
    ("stats", "bm_stats", {}), ("search 50 hits", "bm_search", {"query": "guide", "limit": 50}), ("browse folders (top)", "bm_browse", {"facet": "folders"}),
    ("browse folders (child, 200)", "bm_browse", {"facet": "folders", "parent": "Other bookmarks", "limit": 200}), ("browse domains 200", "bm_browse", {"facet": "domains", "limit": 200}),
    ("history both 50", "bm_history", {"kind": "both", "limit": 50}),
    ("triage 30 (COLD model)", "bm_triage", {"limit": 30}), ("triage 30 (warm)", "bm_triage", {"limit": 30}), ("ai_batch 100", "bm_ai_batch", {"limit": 100}),
    ("organize preview (warm)", "bm_organize", {}), ("dedupe preview", "bm_dedupe", {"level": "variant", "fuzzy": True}), ("clean_urls preview", "bm_clean_urls", {}),
    ("restructure preview", "bm_restructure", {}), ("autotag preview", "bm_autotag", {}), ("titles preview", "bm_titles", {}), ("learn", "bm_learn", {}), ("doctor", "bm_doctor", {})]:
    r, sec = call(name, args)
    d = r.get("structuredContent", {})
    lst = max((len(v) for v in d.values() if isinstance(v, list)), default=0)
    rows.append({"call": label, "tool": name, "arguments": args, "seconds": round(sec, 2), "text_bytes": len(r["content"][0]["text"].encode()),
                 "is_error": r["isError"], "truncated": bool(d.get("truncated")), "largest_list": lst})
    print(f"{label:30s} {sec:6.2f}s {rows[-1]['text_bytes']:7d} B err={r['isError']} trunc={rows[-1]['truncated']}")
json.dump(rows, open(ROOT / "measurements_big.json", "w"), indent=1)
