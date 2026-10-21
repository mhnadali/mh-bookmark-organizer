"""Scenario 2: tools/call argument validation, injection, type confusion.  One fresh server per case (a crash must not hide later cases)."""
import json
import sqlite3
import sys
sys.path.insert(0, "/tmp/claude-0/-home-user-adding-skills/e94ea44a-2022-55bc-9ae0-034b4c065efe/scratchpad/phase3/mcp-audit/harness")
from mcpdrive import *  # noqa

out = []


def count(home):
    c = sqlite3.connect(home + "/library.db")
    try:
        return c.execute("select count(*) from bookmarks").fetchone()[0]
    finally:
        c.close()


DEAD_PROXY = {k: "http://127.0.0.1:9" for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")}
DEAD_PROXY.update(NO_PROXY="", no_proxy="")


def case(label, name, args, *, home=None, srv_args=(), raw_args=None, timeout=120, show=230, extra=None, env=None):
    home = home or fresh_home()
    before = count(home)
    s = Server(home, args=srv_args, env_extra=env)
    s.init()
    params = {"name": name, "arguments": args} if raw_args is None else {"name": name, "arguments": raw_args}
    if extra:
        params.update(extra)
    t = time.time()
    r = s.rpc("tools/call", params, id=100, timeout=timeout)
    dt = time.time() - t
    if r is None:
        try:
            s.p.wait(timeout=5)
        except Exception:
            pass
    alive = s.p.poll() is None
    if isinstance(r, dict) and "result" in r:
        res = r["result"]
        txt = res["content"][0]["text"] if res.get("content") else ""
        summary = f"result isError={res.get('isError')} keys={sorted(res)} text[{len(txt)}]={txt[:show]!r}"
    elif isinstance(r, dict) and "error" in r:
        summary = f"JSON-RPC error {json.dumps(r['error'])[:show]}"
    else:
        summary = f"NO REPLY ({r!r})"
    after = count(home) if os.path.exists(home + "/library.db") else None
    line = f"=== {label}\n  call: {name} {json.dumps(args if raw_args is None else raw_args)[:150]}\n  -> {summary}\n  time={dt:.2f}s server_alive={alive} count {before}->{after}"
    if not alive:
        last = [l for l in s.stderr().strip().splitlines() if l.strip()][-1:]
        line += f"\n  CRASH exit={s.p.poll()} stderr last: {last}"
    out.append(line)
    s.close()
    return r, home


# ---- baseline + schema enforcement
case("baseline bm_stats {}", "bm_stats", {})
case("extra property (schema says additionalProperties:false)", "bm_stats", {"bogus": 1})
case("search missing required 'query'", "bm_search", {})
case("search query is int 123", "bm_search", {"query": 123})
case("search query is null", "bm_search", {"query": None})
case("search query is list", "bm_search", {"query": ["a", "b"]})
case("search query is object", "bm_search", {"query": {"a": 1}})
case("search query empty string", "bm_search", {"query": ""})
case("search limit 0 (schema minimum 1)", "bm_search", {"query": "chisel", "limit": 0})
case("search limit -5", "bm_search", {"query": "chisel", "limit": -5})
case("search limit 100000 (schema maximum 100)", "bm_search", {"query": "chisel", "limit": 100000})
case("search limit '5' string", "bm_search", {"query": "chisel", "limit": "5"})
case("search limit 2.0 float", "bm_search", {"query": "chisel", "limit": 2.0})
case("search limit 2.5 float", "bm_search", {"query": "chisel", "limit": 2.5})
case("search limit true", "bm_search", {"query": "chisel", "limit": True})
case("search limit null", "bm_search", {"query": "chisel", "limit": None})
case("search limit 1e400 (inf)", "bm_search", {"query": "chisel"}, raw_args=json.loads('{"query":"chisel","limit":1e400}'))
case("search limit 10**30", "bm_search", {"query": "chisel", "limit": 10**30})
# ---- option-like values
case("search query '--help'", "bm_search", {"query": "--help"})
case("search query '-x'", "bm_search", {"query": "-x"})
case("search query '--limit 3 chisel'", "bm_search", {"query": "--limit 3 chisel"})
case("search query '-chisel' (exclude-only)", "bm_search", {"query": "-chisel"})
case("search FTS syntax error: unterminated quote", "bm_search", {"query": "\"chisel"})
case("search FTS: bare operator AND", "bm_search", {"query": "AND"})
case("search FTS: parens/NEAR/star", "bm_search", {"query": "NEAR( * ("})
case("search FTS: column filter title:x:y", "bm_search", {"query": "title:chisel:^"})
case("search lone surrogate \\ud800", "bm_search", {"query": "\ud800"}, raw_args=json.loads('{"query":"\\ud800"}'))
case("search NUL byte in query", "bm_search", {"query": "chi\u0000sel"})
case("search 1 MB query", "bm_search", {"query": "a" * 1_000_000})
# ---- arguments container wrong type
case("search arguments is list", "bm_search", None, raw_args=[1])
case("search arguments is string", "bm_search", None, raw_args="x")
case("search arguments is int 5", "bm_search", None, raw_args=5)
case("search arguments is null", "bm_search", None, raw_args=None)
case("import_file arguments is list", "bm_import_file", None, raw_args=["x"])
# ---- bm_import_browser option-like / path
case("import_browser name '--help'", "bm_import_browser", {"name": "--help"})
case("import_browser name '-x'", "bm_import_browser", {"name": "-x"})
case("import_browser name '--profile=/etc/hostname'", "bm_import_browser", {"name": "--profile=/etc/hostname"})
case("import_browser name '-h'", "bm_import_browser", {"name": "-h"})
case("import_browser name unknown 'netscape'", "bm_import_browser", {"name": "netscape"})
case("import_browser name int 5", "bm_import_browser", {"name": 5})
case("import_browser name empty", "bm_import_browser", {"name": ""})
case("import_browser name 'CHROME' (case)", "bm_import_browser", {"name": "CHROME"})
# ---- enum + boolean coercion
case("dedupe level 'bogus' (enum)", "bm_dedupe", {"level": "bogus"})
case("dedupe level int 1", "bm_dedupe", {"level": 1})
case("dedupe level null", "bm_dedupe", {"level": None})
for label, v in (("apply true", True), ("apply 'false' string", "false"), ("apply 'no' string", "no"), ("apply 0", 0), ("apply 1", 1), ("apply null", None),
                 ("apply [] ", []), ("apply [0]", [0]), ("apply {} ", {}), ("apply '0'", "0"), ("apply ''", "")):
    case(f"dedupe {label}  (does it WRITE?)", "bm_dedupe", {"apply": v})
for label, v in (("apply 'false' string", "false"), ("apply true", True)):
    case(f"restructure {label}", "bm_restructure", {"apply": v}, show=120)
    case(f"organize {label}", "bm_organize", {"apply": v}, show=120)
case("import_file skip_existing 'false' string (no path -> missing)", "bm_import_file", {"skip_existing": "false"})
# ---- tool name oddities
case("unknown tool", "bm_nope", {})
case("tool name wrong case", "BM_STATS", {})
case("tool name with spaces/padding", " bm_stats", {})
case("tool name null", None, {})
# ---- limits on other tools
if os.environ.get("SLOW"):
  case("check_links limit -1 (dead proxy so nothing leaves the box)", "bm_check_links", {"limit": -1}, show=200, env=DEAD_PROXY)
  case("check_links limit 'x'", "bm_check_links", {"limit": "x"}, show=200, env=DEAD_PROXY)
  case("archive limit 0 (0 means all in CLI!) (dead proxy)", "bm_archive", {"limit": 0}, show=200, env=DEAD_PROXY)
case("triage limit 0", "bm_triage", {"limit": 0}, show=120)
case("triage limit 100000", "bm_triage", {"limit": 100000}, show=120)

open(SCRATCH + "/transcripts/s02_tools" + ("_slow" if os.environ.get("SLOW") else "") + ".txt", "w").write("\n".join(out) + "\n")
print("\n".join(out))
