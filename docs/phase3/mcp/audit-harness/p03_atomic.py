"""Is an interrupted `apply-plan --apply` all-or-nothing?  Kill it (SIGINT / SIGTERM / SIGKILL) at random moments on a 20k library."""
import json, os, random, shutil, signal, sqlite3, subprocess, sys, tempfile, time
S = "/tmp/claude-0/-home-user-adding-skills/e94ea44a-2022-55bc-9ae0-034b4c065efe/scratchpad/phase3/mcp-audit"
env = dict(os.environ, PYTHONPATH=os.environ.get("AUDIT_PKG", S + "/pkg_fixed"), PYTHONDONTWRITEBYTECODE="1")
src = sqlite3.connect(S + "/lib-big/library.db"); ids = [r[0] for r in src.execute("select id from bookmarks limit 6000")]; src.close()
R = random.Random(5)
results = []
for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
    for trial in range(6):
        d = tempfile.mkdtemp(prefix="atomic-", dir=S + "/tmp"); shutil.copytree(S + "/lib-big", d + "/lib")
        plan = d + "/plan.json"; json.dump({i: {"delete": True, "reason": "test"} for i in ids}, open(plan, "w"))
        p = subprocess.Popen([sys.executable, "-B", "-s", "-m", "mhbm", "--home", d + "/lib", "--json", "--quiet", "apply-plan", "--apply", plan], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        time.sleep(R.uniform(0.35, 2.2))
        done_before = p.poll() is not None
        if not done_before:
            os.killpg(p.pid, sig)
        try: p.wait(10)
        except subprocess.TimeoutExpired: os.killpg(p.pid, signal.SIGKILL); p.wait()
        c = sqlite3.connect(d + "/lib/library.db")
        ok = c.execute("pragma integrity_check").fetchone()[0]
        n = c.execute("select count(*) from bookmarks").fetchone()[0]
        fts = c.execute("select count(*) from bm_fts").fetchone()[0]
        ops = c.execute("select count(*) from ops").fetchone()[0]
        c.close()
        results.append((signal.Signals(sig).name, "finished-before-kill" if done_before else "killed", ok, n, fts, ops))
for r in results: print(r)
bad = [r for r in results if r[2] != "ok" or r[3] not in (20000, 14000) or r[4] != r[3]]
print("all-or-nothing violations:", len(bad), "of", len(results))
