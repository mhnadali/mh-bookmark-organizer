import json, sys
sys.path.insert(0, "/tmp/claude-0/-home-user-adding-skills/e94ea44a-2022-55bc-9ae0-034b4c065efe/scratchpad/phase3/mcp-audit/harness")
from mcpdrive import *
out = []
cases = [("JSON null", "null"), ("JSON number 42", "42"), ("JSON string", '"hi"'), ("JSON true", "true"), ("empty array []", "[]"),
         ("batch [ping,tools/list]", json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])),
         ("batch [notification only]", json.dumps([{"jsonrpc": "2.0", "method": "notifications/initialized"}])),
         ("array of non-objects [1,2]", "[1,2]"),
         ("trailing garbage after object", '{"jsonrpc":"2.0","id":3,"method":"ping"} xx'),
         ("NaN literal in params", '{"jsonrpc":"2.0","id":4,"method":"ping","params":{"x":NaN}}'),
         ("duplicate keys id", '{"jsonrpc":"2.0","id":5,"id":6,"method":"ping"}'),
         ("deeply nested 100000 '['", "[" * 100000),
         ("deeply nested 100000 object", '{"a":' * 100000),
         ("params is a string for tools/call", '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":"x"}'),
         ("params is a list for tools/call", '{"jsonrpc":"2.0","id":8,"method":"tools/call","params":[1]}'),
         ("tools/call params null", '{"jsonrpc":"2.0","id":9,"method":"tools/call","params":null}'),
         ("tools/call name is object", '{"jsonrpc":"2.0","id":10,"method":"tools/call","params":{"name":{"a":1}}}'),
         ("tools/call arguments is a list", '{"jsonrpc":"2.0","id":11,"method":"tools/call","params":{"name":"bm_stats","arguments":[1]}}'),
         ("tools/call arguments is a string", '{"jsonrpc":"2.0","id":12,"method":"tools/call","params":{"name":"bm_stats","arguments":"x"}}'),
         ("tools/call arguments is a number 5", '{"jsonrpc":"2.0","id":13,"method":"tools/call","params":{"name":"bm_stats","arguments":5}}'),
         ]
for label, line in cases:
    s = Server(fresh_home())
    s.init()
    s.send_line(line)
    got = []
    t = time.time()
    while time.time() - t < 6:
        r = s.recv(1.5)
        if not r:
            break
        got.append(r[:260])
    alive = s.p.poll() is None
    out.append(f"=== {label}: {len(got)} reply line(s); server alive={alive}" + ("" if alive else f" exit={s.p.poll()}"))
    for g in got:
        out.append("    " + g)
    if not alive:
        last = [l for l in s.stderr().strip().splitlines() if l.strip()][-1:]
        out.append(f"    stderr last line: {last}")
    s.close()
open(SCRATCH + "/transcripts/s01b_malformed.txt", "w").write("\n".join(out) + "\n")
print("\n".join(out))
