#!/usr/bin/env python3
"""Start `claude -p` in a network-less namespace with a clean environment, read ONLY the first stream-json event
(`system/init`: loaded plugins, skills, agents, slash commands, MCP servers and tools), then kill the process.
No credentials are provided and no packet can leave the namespace, so no model request can succeed.

usage: probe_init.py HOME [--cwd DIR] [--plugin-dir DIR]... [--debug-file F] [--json] [-- extra claude args]"""
import argparse, json, os, signal, subprocess, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("home")
ap.add_argument("--cwd", default=None)
ap.add_argument("--plugin-dir", action="append", default=[])
ap.add_argument("--debug-file", default=None)
ap.add_argument("--json", action="store_true", help="print the whole init event")
ap.add_argument("--timeout", type=float, default=60)
ap.add_argument("extra", nargs="*")
a = ap.parse_args()
claude = os.environ.get("CLAUDE_BIN", "/opt/node22/bin/claude")
cmd = ["unshare", "-rn", "env", "-i", "PATH=/opt/node22/bin:/usr/local/bin:/usr/bin:/bin", f"HOME={a.home}", f"CLAUDE_CONFIG_DIR={a.home}/.claude",
       "TERM=dumb", "LANG=C.UTF-8", claude, "-p", "probe", "--output-format", "stream-json", "--verbose"]
for d in a.plugin_dir:
    cmd += ["--plugin-dir", d]
if a.debug_file:
    cmd += ["--debug-file", a.debug_file]
cmd += a.extra
p = subprocess.Popen(cmd, cwd=a.cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, text=True)
init = None
t0 = time.time()
try:
    for line in p.stdout:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            init = ev
            break
        if time.time() - t0 > a.timeout:
            break
finally:
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    p.wait()
if not init:
    sys.exit("no init event received")
if a.json:
    print(json.dumps(init, indent=1))
else:
    plug = [pl["name"] for pl in init.get("plugins", []) if not pl.get("path") == "builtin"]
    print("plugins       :", [(pl["name"], pl.get("source"), pl.get("version")) for pl in init.get("plugins", []) if pl.get("path") != "builtin"])
    print("skills        :", [s for s in init.get("skills", []) if ":" in s])
    print("slash_commands:", [s for s in init.get("slash_commands", []) if ":" in s])
    print("agents        :", [s for s in init.get("agents", []) if ":" in s])
    print("mcp_servers   :", init.get("mcp_servers"))
    print("mcp tools     :", [t for t in init.get("tools", []) if t.startswith("mcp__")])
