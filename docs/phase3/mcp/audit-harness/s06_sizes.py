"""Scenario 6: output sizes on a 20k-bookmark library, very long input lines, memory, encodings."""
import json
import shutil
import sys
sys.path.insert(0, "/tmp/claude-0/-home-user-adding-skills/e94ea44a-2022-55bc-9ae0-034b4c065efe/scratchpad/phase3/mcp-audit/harness")
from mcpdrive import *  # noqa

out = []
BIG = SCRATCH + "/lib-big"


def hwm(pid):
    try:
        for line in open(f"/proc/{pid}/status"):
            if line.startswith("VmHWM"):
                return int(line.split()[1]) // 1024
    except OSError:
        return None


def bighome():
    d = tempfile.mkdtemp(prefix="big-", dir=SCRATCH + "/tmp")
    shutil.copytree(BIG, d + "/lib")
    return d + "/lib"


s = Server(bighome())
s.init()
out.append(f"server RSS peak after init: {hwm(s.p.pid)} MiB")
for name, args in (("bm_stats", {}), ("bm_search", {"query": "garden", "limit": 100}), ("bm_search", {"query": "garden", "limit": 100000}),
                   ("bm_search", {"query": "", "limit": -1}), ("bm_search", {"query": "garden", "limit": 1000000}), ("bm_history", {}),
                   ("bm_dedupe", {}), ("bm_feedback", {}), ("bm_triage", {"limit": 100}), ("bm_triage", {"limit": 100000})):
    t = time.time()
    s.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    raw = s.recv(300)
    dt = time.time() - t
    n = len(raw) if raw else 0
    try:
        r = json.loads(raw)
        txt = r["result"]["content"][0]["text"]
        inner = len(txt)
        err = r["result"].get("isError")
    except Exception as e:
        inner, err = None, f"unparseable {e!r}"
    out.append(f"=== 20k library: {name} {json.dumps(args)} -> reply line {n:>10,} bytes (text {inner}), isError={err}, {dt:.2f}s, server peak RSS {hwm(s.p.pid)} MiB")
s.close()

# ---- long lines
for size_mb in (8, 64):
    s = Server(fresh_home())
    s.init()
    pad = "x" * (size_mb * 1024 * 1024)
    t = time.time()
    s.send_raw(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"pad":"' + pad.encode() + b'"}}\n')
    r = s.recv_json(120)
    out.append(f"=== {size_mb} MiB request line (ping with padding) -> {r} in {time.time() - t:.2f}s; server peak RSS {hwm(s.p.pid)} MiB")
    s.close()
s = Server(fresh_home())
s.init()
s.send_raw(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"pad":"' + b"y" * (16 * 1024 * 1024))   # 16 MiB with no newline yet
time.sleep(1.0)
out.append(f"=== 16 MiB partial line (no newline): server alive={s.p.poll() is None}; RSS {hwm(s.p.pid)} MiB; reply so far={s.recv(0.5)!r}")
s.send_raw(b'"}}\n')
out.append(f"   after completing the line: {s.recv_json(10)}")
s.close()

# ---- tool call that returns a big result, line size of the response (stdout write blocking if client does not read)
# ---- encodings
for label, env in (("PYTHONUTF8=1 (default here)", {}), ("PYTHONIOENCODING=cp1252 (simulates a Windows legacy code page)", {"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}),
                   ("PYTHONIOENCODING=ascii:strict", {"PYTHONIOENCODING": "ascii:strict", "PYTHONUTF8": "0"})):
    s = Server(fresh_home(), env_extra=env)
    s.init()
    s.send_raw('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"bm_search","arguments":{"query":"شطرنج"}}}\n'.encode("utf-8"))
    r = s.recv_json(20)
    try:
        hits = len(json.loads(r["result"]["content"][0]["text"])["results"]) if r else None
    except Exception as e:
        hits = f"? {e!r}"
    out.append(f"=== {label}: Persian query 'شطرنج' (UTF-8 bytes on stdin) -> hits={hits}; alive={s.p.poll() is None}; stderr tail={s.stderr().strip()[-150:]!r}")
    s.send_raw(b'{"jsonrpc":"2.0","id":2,"method":"ping","params":{"x":"\xc3"}}\n')
    r2 = s.recv_json(5)
    out.append(f"   then a line with an undecodable byte -> {r2}; alive={s.p.poll() is None}; stderr tail={s.stderr().strip()[-150:]!r}")
    s.close()
# multibyte char split across two writes
s = Server(fresh_home())
s.init()
msg = '{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":"شطرنج"}}\n'.encode("utf-8")
cut = msg.index("ش".encode("utf-8")) + 1
s.send_raw(msg[:cut]); time.sleep(0.3); s.send_raw(msg[cut:])
out.append(f"=== UTF-8 char split across two writes -> {s.recv_json(5)}")
s.close()
# output is ASCII-escaped
s = Server(fresh_home())
s.init()
s.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "bm_search", "arguments": {"query": "شطرنج"}}})
raw = s.recv(20)
out.append(f"=== response encoding: contains raw non-ASCII bytes? {any(ord(c) > 127 for c in raw)}; contains \\u escapes? {'\\u06' in raw}; text field is indent=2 pretty JSON? {'\\n  ' in raw}")
s.close()

open(SCRATCH + "/transcripts/s06_sizes.txt", "w").write("\n".join(out) + "\n")
print("\n".join(out))
