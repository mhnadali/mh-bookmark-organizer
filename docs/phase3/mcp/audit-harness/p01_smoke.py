import json, sys
sys.path.insert(0, "/tmp/claude-0/-home-user-adding-skills/e94ea44a-2022-55bc-9ae0-034b4c065efe/scratchpad/phase3/mcp-audit/harness")
from mcpdrive import *
PROTO = SCRATCH + "/pkg_proto"
s = Server(fresh_home(), pkg=PROTO)
r = s.init("2025-11-25"); print("initialize:", json.dumps(r)[:900])
r = s.rpc("tools/list", id=1); tools = r["result"]["tools"]; print("tools:", len(tools), "bytes:", len(json.dumps(r)))
print("tool[1]:", json.dumps(tools[1])[:900])
def call(name, args, id=5, **kw):
    t = time.time(); r = s.rpc("tools/call", {"name": name, "arguments": args}, id=id, timeout=120); dt = time.time() - t
    res = r.get("result") if isinstance(r, dict) else None
    if res is None: print(f"{name} {json.dumps(args)[:80]} -> ERROR {json.dumps(r)[:300]} ({dt:.2f}s)"); return r
    print(f"{name} {json.dumps(args)[:80]} -> isError={res.get('isError')} structured={'structuredContent' in res} text={res['content'][0]['text'][:200]!r} meta={res.get('_meta')} ({dt:.2f}s)")
    return r
call("bm_stats", {})
call("bm_search", {"query": "chisel", "limit": 2})
call("bm_search", {"query": 123})
call("bm_search", {"query": "x", "limit": 1000})
call("bm_dedupe", {"apply": "false"})
call("bm_nope", {})
call("bm_dedupe", {})
s.close(); print(s.stderr()[-600:])
