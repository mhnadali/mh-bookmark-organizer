import json, os, shutil, subprocess, sys, tempfile
S = "/tmp/claude-0/-home-user-adding-skills/e94ea44a-2022-55bc-9ae0-034b4c065efe/scratchpad/phase3/mcp-audit"
env = dict(os.environ, PYTHONPATH=S + "/pkg_fixed", PYTHONDONTWRITEBYTECODE="1", MHBM_LANG="en",
           HTTP_PROXY="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9", http_proxy="http://127.0.0.1:9", https_proxy="http://127.0.0.1:9", NO_PROXY="")
def home():
    d = tempfile.mkdtemp(prefix="shape-", dir=S + "/tmp"); shutil.copytree(S + "/lib-seed", d + "/lib"); return d
def run(label, *args, h=None, timeout=120):
    h = h or home()
    r = subprocess.run([sys.executable, "-B", "-s", "-m", "mhbm", "--home", h + "/lib", "--json", "--quiet", *args], capture_output=True, text=True, env=env, timeout=timeout, cwd=h)
    try:
        d = json.loads(r.stdout)
        shape = {k: (type(v).__name__ + (f"[{len(v)}]" if isinstance(v, (list, dict)) else "")) for k, v in d.items()} if isinstance(d, dict) else type(d).__name__
    except Exception:
        shape = "NOT-JSON: " + r.stdout[:120].replace("\n", " ")
    print(f"## {label}: rc={r.returncode} keys={json.dumps(shape)[:600]}" + (f"\n   stderr: {r.stderr.strip()[-200:]}" if r.returncode else ""))
    return h, r
run("stats", "stats"); run("search", "search", "--limit", "2", "--", "chisel"); run("folders (list)", "folders"); run("tags (list)", "tags"); run("history", "history")
run("reading-list", "reading-list"); run("triage", "triage", "--show", "2"); run("feedback stats", "feedback"); run("feedback list", "feedback", "list")
run("rules list", "rules", "list"); run("rules suggest", "rules", "suggest"); run("rules test", "rules", "test", "--url", "https://github.com/x", "--title", "x")
run("browsers", "browsers"); run("backups", "backups"); run("doctor", "doctor"); run("learn", "learn", timeout=300)
run("organize --plan-out", "organize", "--plan-out", "p.json"); run("dedupe --plan-out", "dedupe", "--plan-out", "p.json"); run("restructure --plan-out", "restructure", "--plan-out", "p.json")
run("autotag --plan-out", "autotag", "--plan-out", "p.json"); run("titles --plan-out", "titles", "--plan-out", "p.json"); run("canonical --plan-out", "canonical", "--plan-out", "p.json")
run("prune (dry)", "prune"); run("clean-urls (dry)", "clean-urls"); run("check --limit 3 (dead proxy)", "check", "--limit", "3"); run("archive --limit 3 (dead proxy)", "archive", "--limit", "3")
h, r = run("tag", "tag", "--add", "x", "--", "9aae81d76fd8")
run("mark", "mark", "--state", "unread", "9aae81d76fd8"); run("undo (op 1)", "undo"); run("export", "export", "--format", "json", "--out", "o.json")
h = home(); run("apply-plan dry", "dedupe", "--plan-out", h + "/p.json", h=h); run("apply-plan", "apply-plan", h + "/p.json", h=h); run("apply-plan --apply", "apply-plan", h + "/p.json", "--apply", h=h)
run("config", "config"); run("synonyms list", "synonyms", "list"); run("verify-chrome (nonexistent)", "verify-chrome", "/nonexistent")
