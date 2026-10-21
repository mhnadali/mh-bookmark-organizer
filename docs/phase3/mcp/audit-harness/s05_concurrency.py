"""Scenario 5: blocking, cancellation, progress, concurrency, library lock contention, worker processes, stdout hygiene."""
import json
import sqlite3
import subprocess
import sys
import threading
sys.path.insert(0, "/tmp/claude-0/-home-user-adding-skills/e94ea44a-2022-55bc-9ae0-034b4c065efe/scratchpad/phase3/mcp-audit/harness")
from mcpdrive import *  # noqa

out = []
DEAD_PROXY = {k: "http://127.0.0.1:9" for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")}
DEAD_PROXY.update(NO_PROXY="", no_proxy="")


def collect(s, n, timeout=60):
    got = []
    t0 = time.time()
    while len(got) < n and time.time() - t0 < timeout:
        r = s.recv(1.0)
        if r is None:
            break
        if r:
            got.append((round(time.time() - s.t0, 2), r[:140]))
    return got


# ---- (a) a long tool blocks the loop: ping + tools/list queue behind bm_organize; cancellation arrives too late
s = Server(fresh_home())
s.init()
t_send = time.time() - s.t0
s.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "bm_organize", "arguments": {}, "_meta": {"progressToken": "tok-1"}}})
time.sleep(0.2)
s.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
s.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1, "reason": "user pressed stop"}})
s.send({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
got = collect(s, 3, 30)
out.append(f"=== (a) bm_organize (id 1, with _meta.progressToken) sent at t={t_send:.2f}s; ping(2), cancel(1), tools/list(3) sent 0.2s later")
for t, line in got:
    out.append(f"   t={t:6.2f}s  <- {line}")
out.append("   => ping answered only after organize finished; cancel had no effect; no notifications/progress was ever emitted: "
           + str(not any('notifications/progress' in l for _, l in got)))
s.close()

# ---- (b) slow network tool: how long is the server deaf?  (dead proxy => every fetch fails fast, so this is the best case)
s = Server(fresh_home(), env_extra=DEAD_PROXY)
s.init()
t0 = time.time()
s.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "bm_check_links", "arguments": {"limit": 60}}})
time.sleep(0.5)
s.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
got = collect(s, 2, 180)
out.append("=== (b) bm_check_links limit=60 against a dead proxy (best case), ping sent 0.5s later")
for t, line in got:
    out.append(f"   t={t:6.2f}s  <- {line}")
s.close()

# ---- (c) library busy: another process holds the write lock
home = fresh_home()
lock = sqlite3.connect(home + "/library.db", timeout=1, isolation_level=None)
lock.execute("BEGIN IMMEDIATE")
s = Server(home, env_extra={"MHBM_BUSY_TIMEOUT_MS": "700"})
s.init()
for name, args in (("bm_stats", {}), ("bm_search", {"query": "chisel"}), ("bm_dedupe", {"apply": True}), ("bm_undo", {}), ("bm_organize", {"apply": True})):
    t = time.time()
    r = s.rpc("tools/call", {"name": name, "arguments": args}, id=7, timeout=60)
    res = r.get("result", {}) if isinstance(r, dict) else {}
    txt = (res.get("content") or [{"text": json.dumps(r)}])[0]["text"]
    out.append(f"=== (c) writer holds BEGIN IMMEDIATE; MHBM_BUSY_TIMEOUT_MS=700; {name}{json.dumps(args)} -> isError={res.get('isError')} in {time.time() - t:.2f}s text={txt[:230]!r}")
r = s.rpc("ping", id=9)
out.append(f"   server alive afterwards: ping -> {r}")
s.close()
lock.execute("ROLLBACK")
lock.close()
# default busy timeout: how long does a write tool wait while the loop is blocked?
home = fresh_home()
lock = sqlite3.connect(home + "/library.db", timeout=1, isolation_level=None)
lock.execute("BEGIN IMMEDIATE")
s = Server(home)
s.init()
t = time.time()
s.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "bm_dedupe", "arguments": {"apply": True}}})
s.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
got = collect(s, 2, 90)
out.append("=== (c2) default MHBM_BUSY_TIMEOUT_MS (30000): bm_dedupe apply blocked on a held lock; ping queued behind it")
for tt, line in got:
    out.append(f"   t={tt:6.2f}s  <- {line}")
s.close()
lock.execute("ROLLBACK")
lock.close()

# ---- (d) workers: MHBM_WORKERS=2 forces the cross-validation process pool inside the MCP server
s = Server(fresh_home(), env_extra={"MHBM_WORKERS": "2"})
s.init()
s.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "bm_organize", "arguments": {}}})
kids = 0
t0 = time.time()
while time.time() - t0 < 20 and s.out_q.empty():
    ps = subprocess.run(["ps", "--ppid", str(s.p.pid), "-o", "pid,args"], capture_output=True, text=True).stdout
    n = max(0, len(ps.strip().splitlines()) - 1)
    kids = max(kids, n)
    time.sleep(0.1)
r = s.recv_json(30)
out.append(f"=== (d) MHBM_WORKERS=2 bm_organize: max child processes seen={kids}; reply ok={'result' in (r or {})}; stderr={s.stderr().strip()[:300]!r}")
s.close()

# ---- (e) killing the server mid-tool: are child processes orphaned?
s = Server(fresh_home(), env_extra={"MHBM_WORKERS": "2"})
s.init()
s.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "bm_organize", "arguments": {}}})
time.sleep(1.0)
pid = s.p.pid
kids_before = subprocess.run(["ps", "--ppid", str(pid), "-o", "pid="], capture_output=True, text=True).stdout.split()
s.p.stdin.close()      # client closes stdin while a tool is running (spec shutdown step 1)
t = time.time()
try:
    s.p.wait(timeout=30)
    waited = time.time() - t
except subprocess.TimeoutExpired:
    waited = None
out.append(f"=== (e) client closes stdin while bm_organize is running: server exited after {waited and round(waited, 1)}s (it finishes the running tool first); children at close={kids_before}")
s.close()

# ---- (f) two clients, one library: simultaneous mutating calls
home = fresh_home()
a, b = Server(home), Server(home)
a.init(); b.init()
a.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "bm_dedupe", "arguments": {"apply": True}}})
b.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "bm_dedupe", "arguments": {"apply": True}}})
ra, rb = a.recv_json(60), b.recv_json(60)
out.append("=== (f) two servers, same library, simultaneous bm_dedupe apply:\n   A -> " + json.dumps(ra)[:250] + "\n   B -> " + json.dumps(rb)[:250])
a.close(); b.close()

open(SCRATCH + "/transcripts/s05_concurrency.txt", "w").write("\n".join(out) + "\n")
print("\n".join(out))
