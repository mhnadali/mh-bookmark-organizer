"""Scenario 4: what each tool (and --read-only) actually writes.  Directory tree is hashed before/after each call."""
import json
import shutil
import sqlite3
import sys
sys.path.insert(0, "/tmp/claude-0/-home-user-adding-skills/e94ea44a-2022-55bc-9ae0-034b4c065efe/scratchpad/phase3/mcp-audit/harness")
from mcpdrive import *  # noqa

out = []
FIX = "/home/user/adding-skills/.claude/skills/mh-bookmark-organizer/tests/fixtures"


def clean_seed(dest):
    """A seeded library with no -wal/-shm and no derived caches, so any file that appears afterwards was written by the tool."""
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(SEED_LIB, dest)
    c = sqlite3.connect(dest + "/library.db")
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    for f in ("library.db-wal", "library.db-shm", "model.json"):
        try:
            os.unlink(dest + "/" + f)
        except OSError:
            pass
    return dest


def run_tool(home, name, args, srv_args=(), env=None, timeout=120):
    before = tree_state(home) if os.path.isdir(home) else None
    s = Server(home, args=srv_args, env_extra=env)
    s.init()
    r = s.rpc("tools/call", {"name": name, "arguments": args}, id=5, timeout=timeout)
    s.close()
    after = tree_state(home) if os.path.isdir(home) else None
    ok = isinstance(r, dict) and "result" in r
    flag = ("isError=%s" % r["result"].get("isError")) if ok else ("error" if isinstance(r, dict) else "no-reply")
    if before is None:
        return flag, {"<home created>": (None, "new")} if after is not None else {}
    return flag, diff_state(before, after)


def show(d):
    if not d:
        return "NOTHING written"
    return "; ".join(f"{k}: {'created' if v[0] is None else 'removed' if v[1] is None else 'modified'}" for k, v in sorted(d.items()))


# ---- (a) read-only server, nonexistent home
home = SCRATCH + "/tmp/ro-nonexistent/lib"
shutil.rmtree(SCRATCH + "/tmp/ro-nonexistent", ignore_errors=True)
s = Server(home, args=("--read-only",))
s.init()
r = s.rpc("tools/call", {"name": "bm_stats", "arguments": {}}, id=5)
s.close()
out.append(f"=== (a) --read-only server, NONEXISTENT home, bm_stats -> {'ok' if 'result' in (r or {}) else r}; home dir created: {os.path.isdir(home)}; files: {sorted(os.listdir(home)) if os.path.isdir(home) else None}")

# ---- (b) read-only tools on a clean (checkpointed) library
for tool, args in (("bm_stats", {}), ("bm_search", {"query": "chisel"}), ("bm_history", {}), ("bm_triage", {"limit": 5}), ("bm_feedback", {})):
    home = clean_seed(SCRATCH + "/tmp/ro-clean/lib")
    flag, d = run_tool(home, tool, args, srv_args=("--read-only",))
    out.append(f"=== (b) --read-only, {tool} on a clean WAL library: {flag}; {show(d)}")

# ---- (c) old schema library opened through a read-only server (migration on open)
for fixture in ("old-library-v1", "old-library-v2-early", "old-library-v2-late"):
    home = SCRATCH + f"/tmp/ro-old-{fixture}/lib"
    shutil.rmtree(SCRATCH + f"/tmp/ro-old-{fixture}", ignore_errors=True)
    os.makedirs(home)
    c = sqlite3.connect(home + "/library.db")
    c.executescript(open(f"{FIX}/{fixture}.sql", encoding="utf-8").read())
    uv0 = c.execute("PRAGMA user_version").fetchone()[0]
    jm0 = c.execute("PRAGMA journal_mode").fetchone()[0]
    c.commit()
    c.close()
    flag, d = run_tool(home, "bm_stats", {}, srv_args=("--read-only",))
    c = sqlite3.connect(home + "/library.db")
    uv1 = c.execute("PRAGMA user_version").fetchone()[0]
    c.close()
    out.append(f"=== (c) --read-only bm_stats on {fixture}: {flag}; user_version {uv0} -> {uv1}; journal_mode before={jm0}; {show(d)}")

# ---- (d) every tool in dry-run / default form: what does it write?
TOOLS = [("bm_stats", {}), ("bm_search", {"query": "chisel"}), ("bm_dedupe", {}), ("bm_organize", {}), ("bm_restructure", {}), ("bm_triage", {}),
         ("bm_feedback", {}), ("bm_history", {}), ("bm_undo", {}), ("bm_import_file", {"path": "in.html"}), ("bm_export", {"format": "json", "out": "o.json"})]
shutil.rmtree(SCRATCH + "/work/allowed", ignore_errors=True)
os.makedirs(SCRATCH + "/work/allowed")
shutil.copy(SCRATCH + "/work/messy.html", SCRATCH + "/work/allowed/in.html")
for tool, args in TOOLS:
    home = clean_seed(SCRATCH + "/tmp/wr/lib")
    flag, d = run_tool(home, tool, args, srv_args=("--allow-dir", SCRATCH + "/work/allowed"))
    out.append(f"=== (d) full server, {tool} {json.dumps(args)} (no apply): {flag}; written inside home: {show(d)}")
# apply forms
for tool, args in (("bm_dedupe", {"apply": True}), ("bm_organize", {"apply": True}), ("bm_restructure", {"apply": True})):
    home = clean_seed(SCRATCH + "/tmp/wr/lib")
    flag, d = run_tool(home, tool, args)
    out.append(f"=== (d) full server, {tool} {json.dumps(args)}: {flag}; written inside home: {show(d)}")

# ---- (e) import twice -> second is a no-op but is an op recorded?  history after.
home = clean_seed(SCRATCH + "/tmp/wr2/lib")
s = Server(home, args=("--allow-dir", SCRATCH + "/work/allowed"), cwd=SCRATCH + "/work/allowed")
s.init()
for i in (1, 2):
    r = s.rpc("tools/call", {"name": "bm_import_file", "arguments": {"path": "in.html"}}, id=10 + i)
    out.append(f"=== (e) import #{i}: " + r["result"]["content"][0]["text"].replace("\n", " ")[:200])
r = s.rpc("tools/call", {"name": "bm_history", "arguments": {}}, id=20)
txt = json.loads(r["result"]["content"][0]["text"])
out.append("   history ops: " + json.dumps([(o["op"], o["command"], o["changes"]) for o in txt["operations"]]))
out.append("   snapshots dir: " + str(sorted(os.listdir(home + "/snapshots")) if os.path.isdir(home + "/snapshots") else None))
s.close()

# ---- (f) --read-only hides write tools; direct call refused
s = Server(clean_seed(SCRATCH + "/tmp/ro-f/lib"), args=("--read-only",))
s.init()
r = s.rpc("tools/list", id=2)
out.append("=== (f) --read-only tools/list: " + ", ".join(t["name"] for t in r["result"]["tools"]))
r = s.rpc("tools/call", {"name": "bm_undo", "arguments": {}}, id=3)
out.append("   --read-only tools/call bm_undo -> " + json.dumps(r)[:200])
s.close()

open(SCRATCH + "/transcripts/s04_readonly.txt", "w").write("\n".join(out) + "\n")
print("\n".join(out))
