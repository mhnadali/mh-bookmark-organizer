"""Scenario 1: initialize / version negotiation / ids / malformed input / batch / unknown methods."""
import json
import subprocess
import sys
sys.path.insert(0, "/tmp/claude-0/-home-user-adding-skills/e94ea44a-2022-55bc-9ae0-034b4c065efe/scratchpad/phase3/mcp-audit/harness")
from mcpdrive import *  # noqa

out = []


def section(title, srv):
    out.append(srv.dump(title))


# 0. does the shipped command even start?
home = fresh_home()
for label, cmd, env in (
    ("A: python3 -I -m mhbm ... mcp  (PYTHONPATH set, as the task text suggests)", ["python3", "-I", "-m", "mhbm", "--home", home, "mcp"], {"PYTHONPATH": PKG_ORIG}),
    ("B: python3 -s -m mhbm ... mcp  (unmodified repo)", ["python3", "-B", "-s", "-m", "mhbm", "--home", home, "mcp"], {"PYTHONPATH": PKG_ORIG}),
    ("C: same, scratch copy with `from .. import mcp_server`", ["python3", "-B", "-s", "-m", "mhbm", "--home", home, "mcp"], {"PYTHONPATH": PKG_FIXED}),
):
    r = subprocess.run(cmd, input=b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n', capture_output=True, timeout=30,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", **env))
    out.append(f"=== {label}\n$ {' '.join(cmd[:4])} ...\nexit={r.returncode}\nstdout={r.stdout.decode()!r}\nstderr(last 3 lines)={r.stderr.decode().strip().splitlines()[-3:]!r}")

# 1. initialize with many protocolVersion values
for ver in ("2025-06-18", "2025-03-26", "2024-11-05", "2099-01-01", "1.0", "", None, 20250618, [1], "garbage<script>"):
    s = Server(fresh_home())
    params = {"protocolVersion": ver, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}
    r = s.rpc("initialize", params, id=1)
    out.append(f"=== initialize protocolVersion={ver!r}\n  -> {json.dumps(r)[:600]}")
    s.close()
# initialize with no params / params null / params list / no protocolVersion
for label, msg in (("no params", {"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
                   ("params=null", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": None}),
                   ("params=[]", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": []}),
                   ("params=\"x\"", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": "x"}),
                   ("params={}", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})):
    s = Server(fresh_home())
    s.send(msg)
    r = s.recv_json(10)
    out.append(f"=== initialize {label}\n  -> {json.dumps(r)[:400]}")
    out.append("   stderr: " + s.stderr().strip()[-300:].replace("\n", " | ") if s.stderr().strip() else "   (no stderr)")
    out.append(f"   server alive after? {s.p.poll() is None}")
    s.close()

# 2. full handshake transcript, then tools/list before initialize
s = Server(fresh_home())
r = s.rpc("tools/list", id=1)
out.append("=== tools/list BEFORE initialize -> " + (json.dumps(r)[:200] if r else repr(r)))
r = s.init()
out.append("=== initialize response (2025-06-18)\n" + json.dumps(r, indent=1))
s.notify("notifications/initialized")
r = s.rpc("tools/list", id=2)
out.append("=== tools/list (first tool, full) \n" + json.dumps(r["result"]["tools"][1], indent=1))
out.append("   tool names: " + ", ".join(t["name"] for t in r["result"]["tools"]))
out.append("   keys per tool: " + str(sorted({k for t in r["result"]["tools"] for k in t})))
out.append("   result keys: " + str(sorted(r["result"])))
for m in ("resources/list", "resources/templates/list", "prompts/list", "logging/setLevel", "completion/complete", "tools/call", "nope/nope", "resources/read", "prompts/get", "notifications/cancelled"):
    r = s.rpc(m, {"name": "x"} if m in ("tools/call", "prompts/get") else None, id=f"m-{m}")
    out.append(f"=== {m} -> {json.dumps(r)[:300]}")
section("handshake session", s)
s.close()

# 3. request ids of different types
s = Server(fresh_home())
s.init()
for label, idv in (("int 7", 7), ("int 0", 0), ("string 'abc'", "abc"), ("empty string", ""), ("float 1.5", 1.5), ("bool true", True), ("null", None), ("object", {"a": 1}), ("list", [1]), ("huge int", 10**30), ("negative", -1)):
    msg = {"jsonrpc": "2.0", "id": idv, "method": "ping"}
    s.send(msg)
    r = s.recv_json(3)
    out.append(f"=== ping with id {label} -> {json.dumps(r) if r else repr(r)}")
s.send({"jsonrpc": "2.0", "method": "ping"})
r = s.recv_json(2)
out.append(f"=== ping with NO id key (notification) -> {json.dumps(r) if r else repr(r)}")
s.send({"jsonrpc": "2.0", "id": 5, "result": {}})
r = s.recv_json(2)
out.append(f"=== client RESPONSE message (id=5, result) -> {json.dumps(r) if r else repr(r)}")
s.send({"id": 6, "method": "ping"})
r = s.recv_json(2)
out.append(f"=== ping WITHOUT jsonrpc key -> {json.dumps(r) if r else repr(r)}")
s.send({"jsonrpc": "1.0", "id": 7, "method": "ping"})
r = s.recv_json(2)
out.append(f"=== ping with jsonrpc 1.0 -> {json.dumps(r) if r else repr(r)}")
s.send({"jsonrpc": "2.0", "id": 8, "method": 42})
r = s.recv_json(2)
out.append(f"=== method is a number -> {json.dumps(r) if r else repr(r)}")
s.send({"jsonrpc": "2.0", "id": 9})
r = s.recv_json(2)
out.append(f"=== no method key -> {json.dumps(r) if r else repr(r)}")
r = s.rpc("ping", id=99)
out.append(f"=== still alive? ping id 99 -> {json.dumps(r) if r else repr(r)}; alive={s.p.poll() is None}")
section("id types", s)
s.close()

# 4. malformed JSON, non-object JSON, batch, partial UTF-8, blank lines
s = Server(fresh_home())
s.init()
cases = [("malformed '{'", '{'), ("garbage", "not json"), ("JSON null", "null"), ("JSON number", "42"), ("JSON string", '"hi"'), ("empty array", "[]"),
         ("batch of 2 requests", json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])),
         ("batch of notifications only", json.dumps([{"jsonrpc": "2.0", "method": "notifications/initialized"}])),
         ("array of non-objects", "[1,2]"), ("trailing garbage after object", '{"jsonrpc":"2.0","id":3,"method":"ping"} xx'),
         ("NaN literal", '{"jsonrpc":"2.0","id":4,"method":"ping","params":{"x":NaN}}'),
         ("duplicate keys", '{"jsonrpc":"2.0","id":5,"id":6,"method":"ping"}'),
         ("deeply nested (100000 [)", "[" * 100000)]
for label, line in cases:
    s.send_line(line)
    got = []
    while True:
        r = s.recv(1.5)
        if not r:
            break
        got.append(r[:300])
    out.append(f"=== {label}: {len(got)} reply line(s)\n    " + "\n    ".join(got) if got else f"=== {label}: NO REPLY")
    out.append(f"    alive={s.p.poll() is None}")
    if s.p.poll() is not None:
        out.append("    server died; stderr tail: " + s.stderr().strip()[-400:].replace("\n", " | "))
        break
section("malformed", s)
s.close()

# 5. invalid UTF-8 and CRLF, BOM, blank line
s = Server(fresh_home())
s.init()
s.send_raw(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":"\xff\xfe"}}\n')
r = s.recv_json(3)
out.append(f"=== invalid UTF-8 byte in a line -> {json.dumps(r) if r else repr(r)}; alive={s.p.poll() is None}")
if s.p.poll() is not None:
    out.append("    stderr tail: " + s.stderr().strip()[-500:].replace("\n", " | "))
s.close()
s = Server(fresh_home())
s.send_raw(b'\xef\xbb\xbf{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
r = s.recv_json(3)
out.append(f"=== UTF-8 BOM at start of first line -> {json.dumps(r) if r else repr(r)}")
s.send_raw(b'{"jsonrpc":"2.0","id":2,"method":"ping"}\r\n')
r = s.recv_json(3)
out.append(f"=== CRLF line ending -> {json.dumps(r) if r else repr(r)}")
s.send_raw(b'\n\n   \n{"jsonrpc":"2.0","id":3,"method":"ping"}\n')
r = s.recv_json(3)
out.append(f"=== blank lines then ping -> {json.dumps(r) if r else repr(r)}")
# a request split across two writes (no newline in between)
s.send_raw(b'{"jsonrpc":"2.0","id":4,')
time.sleep(0.3)
s.send_raw(b'"method":"ping"}\n')
r = s.recv_json(3)
out.append(f"=== request split across two writes -> {json.dumps(r) if r else repr(r)}")
# non-ASCII in reply: escaped?
s.send({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "bm_search", "arguments": {"query": "شطرنج"}}})
r = s.recv(30)
out.append(f"=== persian query reply raw head: {r[:200] if r else r!r}")
s.close()

# 6. stdin closing mid-stream / EOF behaviour
s = Server(fresh_home())
s.init()
s.send_raw(b'{"jsonrpc":"2.0","id":1,"method":"ping"}')   # no trailing newline, then EOF
s.p.stdin.close()
r = s.recv_json(5)
out.append(f"=== last request WITHOUT trailing newline then EOF -> {json.dumps(r) if r else repr(r)}")
try:
    rc = s.p.wait(timeout=5)
except Exception as e:
    rc = repr(e)
out.append(f"    exit code after EOF: {rc}")
section("eof", s)

open(SCRATCH + "/transcripts/s01_protocol.txt", "w").write("\n".join(out) + "\n")
print("\n".join(out))
