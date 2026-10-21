"""Scenario 3: path confinement of bm_import_file / bm_export, the unconfined bm_import_browser path, symlinks, TOCTOU, clobbering."""
import json
import shutil
import sqlite3
import sys
import threading
sys.path.insert(0, "/tmp/claude-0/-home-user-adding-skills/e94ea44a-2022-55bc-9ae0-034b4c065efe/scratchpad/phase3/mcp-audit/harness")
from mcpdrive import *  # noqa

W = SCRATCH + "/work"
A = W + "/allowed"          # server cwd == default allow-dir
B = W + "/outside"          # NOT allowed
for d in (A, B):
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)

HTML = lambda marker: f"""<!DOCTYPE NETSCAPE-Bookmark-file-1>
<DL><p><DT><H3>Probe</H3><DL><p>
<DT><A HREF="https://{marker}.example/page">{marker} title</A>
</DL><p></DL><p>"""
open(A + "/in.html", "w").write(HTML("inside-marker"))
open(B + "/secret.html", "w").write(HTML("outside-marker"))
CHROME = {"roots": {"bookmark_bar": {"type": "folder", "name": "Bar", "id": "1", "children": [
    {"type": "url", "name": "outside chrome marker", "url": "https://outside-chrome-marker.example/x", "id": "2"}]},
    "other": {"type": "folder", "name": "Other", "id": "3", "children": []}, "synced": {"type": "folder", "name": "m", "id": "4", "children": []}}, "version": 1}
json.dump(CHROME, open(B + "/Bookmarks", "w"))
json.dump({"hello": "world", "token": "SECRET-IN-JSON"}, open(B + "/notchrome.json", "w"))
json.dump(["SECRET-IN-LIST"], open(B + "/list.json", "w"))
open(B + "/plain.txt", "w").write("TOP-SECRET-PLAIN-TEXT-CONTENT")
out = []


def has(home, like):
    c = sqlite3.connect(home + "/library.db")
    try:
        return c.execute("select count(*) from bookmarks where url like ?", (f"%{like}%",)).fetchone()[0]
    finally:
        c.close()


def call(s, name, args, id=1, timeout=60):
    r = s.rpc("tools/call", {"name": name, "arguments": args}, id=id, timeout=timeout)
    if r is None:
        try:
            s.p.wait(5)
        except Exception:
            pass
    if isinstance(r, dict) and "result" in r:
        t = r["result"]["content"][0]["text"] if r["result"].get("content") else ""
        return f"isError={r['result'].get('isError')} text={t[:260]!r}"
    if isinstance(r, dict):
        return "JSON-RPC " + json.dumps(r.get("error"))[:260]
    return f"NO REPLY ({r!r}); alive={s.p.poll() is None}; stderr last={[l for l in s.stderr().splitlines() if l.strip()][-1:]}"


def run(label, name, args, probe=None, srv_args=(), cwd=None, home=None, env=None):
    home = home or fresh_home(seeded=False)
    s = Server(home, args=srv_args, cwd=cwd or A, env_extra=env)
    s.init()
    res = call(s, name, args)
    extra = ""
    if probe and os.path.exists(home + "/library.db"):
        try:
            extra = f" | library rows matching {probe!r}: {has(home, probe)}"
        except sqlite3.Error as e:
            extra = f" | (library unreadable now: {e})"
    out.append(f"=== {label}\n  {name} {json.dumps(args)[:200]}\n  -> {res}{extra}")
    s.close()
    return home


# ---- import_file confinement
run("import inside allowed dir (relative)", "bm_import_file", {"path": "in.html"}, "inside-marker")
run("import inside allowed dir (absolute)", "bm_import_file", {"path": A + "/in.html"}, "inside-marker")
run("import outside (absolute)", "bm_import_file", {"path": B + "/secret.html"}, "outside-marker")
run("import outside via ..", "bm_import_file", {"path": "../outside/secret.html"}, "outside-marker")
run("import outside via /./ and //", "bm_import_file", {"path": A + "//./../outside//secret.html"}, "outside-marker")
os.symlink(B + "/secret.html", A + "/link-file.html")
run("import symlink-file inside -> outside", "bm_import_file", {"path": "link-file.html"}, "outside-marker")
os.symlink(B, A + "/link-dir")
run("import through symlinked directory", "bm_import_file", {"path": "link-dir/secret.html"}, "outside-marker")
run("import with NUL byte", "bm_import_file", {"path": "in.html\u0000.txt"}, "inside-marker")
run("import empty path", "bm_import_file", {"path": ""}, "inside-marker")
run("import the allowed dir itself (directory)", "bm_import_file", {"path": "."}, "inside-marker")
run("import path 5000 chars", "bm_import_file", {"path": "a" * 5000}, "inside-marker")
# ---- option-like path values
for p in ("-x", "--help", "-h", "--format=csv", "--skip-existing", "--allow-duplicates", "-"):
    run(f"import path={p!r}", "bm_import_file", {"path": p}, "inside-marker")
# ---- ~ expansion: the check expands ~, the CLI does not
h = SCRATCH + "/work"
run("import '~/allowed/in.html' with HOME=work dir (check expands ~; CLI does not)", "bm_import_file", {"path": "~/allowed/in.html"}, "inside-marker", env={"HOME": h})
run("import '~/outside/secret.html' with allow-dir=work/allowed (check expands ~ -> outside -> denied)", "bm_import_file", {"path": "~/outside/secret.html"}, "outside-marker", env={"HOME": h})
run("import '$HOME/...' not expanded", "bm_import_file", {"path": "$HOME/allowed/in.html"}, "inside-marker", env={"HOME": h})
# ---- allow-dir variants
run("--allow-dir pointing at a symlink to outside; path via symlink", "bm_import_file", {"path": A + "/link-dir/secret.html"}, "outside-marker", srv_args=("--allow-dir", A + "/link-dir"))
run("--allow-dir relative '.' ", "bm_import_file", {"path": "in.html"}, "inside-marker", srv_args=("--allow-dir", "."))
run("--allow-dir '/' (whole filesystem)", "bm_import_file", {"path": B + "/secret.html"}, "outside-marker", srv_args=("--allow-dir", "/"))
run("--allow-dir nonexistent dir", "bm_import_file", {"path": "in.html"}, "inside-marker", srv_args=("--allow-dir", "/nonexistent-dir-xyz"))
run("--allow-dir '' (empty string = cwd?)", "bm_import_file", {"path": B + "/secret.html"}, "outside-marker", srv_args=("--allow-dir", ""))
run("sibling-prefix dir 'allowed2' with allow=allowed (is_relative_to is component-wise)", "bm_import_file", {"path": W + "/allowed2/x.html"}, "x", srv_args=())
# ---- bm_import_browser takes a path
run("import_browser with ABSOLUTE PATH OUTSIDE allow-dir (valid Chromium JSON)", "bm_import_browser", {"name": B + "/Bookmarks"}, "outside-chrome-marker")
run("import_browser path to non-chromium JSON object", "bm_import_browser", {"name": B + "/notchrome.json"}, "x")
run("import_browser path to JSON list", "bm_import_browser", {"name": B + "/list.json"}, "x")
run("import_browser path to plain text", "bm_import_browser", {"name": B + "/plain.txt"}, "x")
run("import_browser path /etc/hostname", "bm_import_browser", {"name": "/etc/hostname"}, "x")
run("import_browser path = directory", "bm_import_browser", {"name": B}, "x")

# ---- export
hexp = fresh_home(seeded=True)
run("export inside (new file)", "bm_export", {"format": "json", "out": "o.json"}, home=hexp)
out.append(f"   o.json size={os.path.getsize(A + '/o.json') if os.path.exists(A + '/o.json') else None}")
open(A + "/precious.txt", "w").write("PRECIOUS")
run("export overwriting an existing file inside allowed dir", "bm_export", {"format": "csv", "out": "precious.txt"}, home=hexp)
out.append(f"   precious.txt now starts: {open(A + '/precious.txt').read()[:60]!r}")
run("export outside", "bm_export", {"format": "json", "out": B + "/o.json"}, home=hexp)
out.append(f"   outside/o.json exists={os.path.exists(B + '/o.json')}")
run("export to directory path", "bm_export", {"format": "json", "out": "."}, home=hexp)
os.symlink(B + "/victim.txt", A + "/link-out.json")
open(B + "/victim.txt", "w").write("VICTIM")
run("export to symlink inside -> outside file", "bm_export", {"format": "json", "out": "link-out.json"}, home=hexp)
out.append(f"   victim.txt still: {open(B + '/victim.txt').read()[:20]!r}")
run("export to '-' (stdout convention?)", "bm_export", {"format": "json", "out": "-"}, home=hexp)
out.append(f"   file named '-' exists in cwd: {os.path.exists(A + '/-')}")
run("export format 'obsidian' (not in the schema enum) to a dir", "bm_export", {"format": "obsidian", "out": "notes"}, home=hexp)
out.append(f"   notes/ file count: {len(os.listdir(A + '/notes')) if os.path.isdir(A + '/notes') else None}")
run("export format 'raindrop'", "bm_export", {"format": "raindrop", "out": "r.csv"}, home=hexp)
run("export out = '--help'", "bm_export", {"format": "json", "out": "--help"}, home=hexp)
run("export out = '--folder'", "bm_export", {"format": "json", "out": "--folder"}, home=hexp)
run("export out = '-5'", "bm_export", {"format": "json", "out": "-5"}, home=hexp)
out.append(f"   file '-5' exists in cwd: {os.path.exists(A + '/-5')}")
run("export out in non-existent subdirectory", "bm_export", {"format": "json", "out": "nodir/o.json"}, home=hexp)
run("export 100-char-deep new dirs", "bm_export", {"format": "json", "out": "a/b/c.json"}, home=hexp)

# ---- home inside the allow-dir (the default layout: ./.mhbm under cwd)
hin = A + "/.mhbm"
shutil.rmtree(hin, ignore_errors=True)
shutil.copytree(SEED_LIB, hin)
json.dump({"notify": {"on": ["webhook"], "webhook": {"url": "https://hooks.example/services/T000/B000/SECRET-WEBHOOK-TOKEN"}}, "taxonomy": None}, open(hin + "/config.json", "w"))
run("import the library's own config.json (home is inside allow-dir)", "bm_import_file", {"path": ".mhbm/config.json"}, "x", home=hin)
run("import the library's own library.db (binary sqlite)", "bm_import_file", {"path": ".mhbm/library.db"}, "x", home=hin)
run("export ONTO config.json (home inside allow-dir)", "bm_export", {"format": "json", "out": ".mhbm/config.json"}, home=hin)
out.append(f"   config.json now starts: {open(hin + '/config.json').read()[:50]!r}")
run("export ONTO library.db (home inside allow-dir)", "bm_export", {"format": "json", "out": ".mhbm/library.db"}, "x", home=hin)
out.append(f"   library.db head after export: {open(hin + '/library.db','rb').read(40)!r}")
s = Server(hin, cwd=A)
s.init()
out.append("   afterwards bm_stats -> " + call(s, "bm_stats", {}))
s.close()

# ---- TOCTOU: swap a regular file for a symlink while the call is in flight
race = A + "/race.html"
good = A + "/race-good.html"
shutil.copy(A + "/in.html", good)
stop = threading.Event()


def racer():
    n = 0
    while not stop.is_set():
        for src in (good, B + "/secret.html"):
            tmp = A + f"/.tmp-link-{n % 2}"
            try:
                os.unlink(tmp)
            except OSError:
                pass
            os.symlink(src, tmp)
            os.replace(tmp, race)
            n += 1


home = fresh_home(seeded=False)
s = Server(home, cwd=A)
s.init()
hits, attempts = 0, 0
t = threading.Thread(target=racer, daemon=True)
os.symlink(good, race)
t.start()
first_hit = None
for i in range(120):
    attempts += 1
    r = s.rpc("tools/call", {"name": "bm_import_file", "arguments": {"path": "race.html"}}, id=1000 + i, timeout=30)
    if has(home, "outside-marker"):
        hits += 1
        first_hit = first_hit or attempts
        break
stop.set()
t.join(2)
out.append(f"=== TOCTOU race: bm_import_file on a path whose target flips between an allowed file and a file OUTSIDE the allow-dir\n  attempts={attempts} outside-file imported={bool(hits)} (first success at attempt {first_hit})")
s.close()

open(SCRATCH + "/transcripts/s03_files.txt", "w").write("\n".join(out) + "\n")
print("\n".join(out))
