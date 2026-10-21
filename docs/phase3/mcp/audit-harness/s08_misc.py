import json, sys
sys.path.insert(0, "/tmp/claude-0/-home-user-adding-skills/e94ea44a-2022-55bc-9ae0-034b4c065efe/scratchpad/phase3/mcp-audit/harness")
from mcpdrive import *
out = []
s = Server(fresh_home())
# 2026-07-28 style probe, before any initialize
r = s.rpc("server/discover", {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}}}, id=1)
out.append("server/discover probe (2026-07-28 client, legacy server) -> " + json.dumps(r))
# a modern-style tools/list without initialize
r = s.rpc("tools/list", {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}}}, id=2)
out.append("modern tools/list w/o initialize -> keys " + str(sorted(r["result"])) + " (answered under legacy semantics; no resultType/ttlMs)")
s.init()
r = s.rpc("tools/list", {"cursor": "bogus-cursor"}, id=3)
out.append("tools/list with bogus cursor -> " + ("ok, " + str(len(r["result"]["tools"])) + " tools, nextCursor=" + str(r["result"].get("nextCursor")) if "result" in r else json.dumps(r)))
r = s.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "x", "version": "1"}}, id=4)
out.append("second initialize on the same connection -> " + json.dumps(r)[:160])
s.notify("notifications/roots/list_changed"); s.notify("notifications/unknown")
r = s.rpc("ping", id=5)
out.append("unknown notifications are silently ignored; ping -> " + json.dumps(r))
r = s.rpc("tools/call", {"name": "bm_stats", "arguments": {}, "_meta": {"progressToken": 7}}, id=6)
out.append("tools/call with progressToken ignored, reply has keys " + str(sorted(r["result"])))
s.close()
open(SCRATCH + "/transcripts/s08_misc.txt", "w").write("\n".join(out) + "\n"); print("\n".join(out))
