#!/usr/bin/env python3
"""Experiment B3: a library full of hostile titles, folder names and URLs -> install-chrome -> real Chromium -> what survives?
usage: exp_b3_hostile.py <skill_dir> <workdir> <profile_skeleton_dir>"""
import json, os, shutil, subprocess, sys
from pathlib import Path
skill, workdir, skeleton = sys.argv[1:4]
sys.path.insert(0, os.path.join(skill, "scripts"))
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
from mhbm import formats
from mhbm.model import Bookmark, assign_ids
from mhbm.store import Library
HERE = Path(__file__).resolve().parent
W = Path(workdir)
shutil.rmtree(W, ignore_errors=True)
(W / "home").mkdir(parents=True)
shutil.copytree(skeleton, W / "udd")
for f in ("Bookmarks", "Bookmarks.bak"):
    (W / "udd/Default" / f).unlink(missing_ok=True)

titles = ["line\nbreak", "tab\there", "cr\rhere", "nul\u0000char", "ls\u2028ps\u2029sep", "bom\ufeffhere", "  lead and trail  ", "", "x" * 200000,
          "😀" * 1000, "rtl \u202eabc\u202c end", "quote \" back \\ lt < gt >", "ctl\u0001\u001f\u007f\u0085"]
urls = ["", "example.com", "//example.com/x", "http://", "https://exa mple.com/", "http://[::1", "javascript:alert(1)", "data:text/plain;base64,QQ==",
        "https://example.com/" + "a" * 100000, "ftp://user@host/f", "file:///C:/x", "chrome-extension://abcdefghijklmnopabcdefghijklmnop/page.html",
        "view-source:https://example.com", "tel:+123", "mailto:a@b.c", "about:blank", "magnet:?xt=urn:btih:abc", "ws://x/", "blob:https://example.com/uuid",
        "https://example.com/\u0000nul", "HTTPS://EXAMPLE.COM", "https://example.com/ü ö"]
folders = ["Folder\nNewline", "Folder\tTab", " lead", "trail ", "A∕B", "Dot.", "Q\"uote", "Back\\slash", "<script>", "F" * 5000, "😀 emoji folder", "RTL \u202e folder"]
bms = []
for i, t in enumerate(titles):
    bms.append(Bookmark(url=f"https://t{i}.example/", title=t, folder="Titles", added=1700000000 + i))
for i, u in enumerate(urls):
    bms.append(Bookmark(url=u, title=f"url {i}", folder="Urls", added=1700000000 + i))
for i, f in enumerate(folders):
    bms.append(Bookmark(url=f"https://f{i}.example/", title=f"in folder {i}", folder="Folders/" + f.replace("/", "∕"), added=1700000000 + i))
lib = Library(str(W / "home"))
assign_ids(bms, lib.ids())
lib.upsert(bms)
lib.add_folders(["Titles", "Urls", "Folders"])
lib.close()
(W / "udd/Default/Bookmarks").write_text("{}")   # install-chrome needs an existing file to replace
env = dict(os.environ, PYTHONPATH=os.path.join(skill, "scripts"))
r = subprocess.run([sys.executable, "-m", "mhbm", "--home", str(W / "home"), "install-chrome", str(W / "udd/Default/Bookmarks"), "--confirm-chrome-closed"], env=env, capture_output=True, text=True)
print(r.stdout.strip().splitlines()[0][:150], r.stderr[:300])
written = json.loads((W / "udd/Default/Bookmarks").read_text(encoding="utf-8"))
print("verify_chrome on what we wrote:", formats.verify_chrome(written))
shutil.copy(W / "udd/Default/Bookmarks", W / "written.json")
# load in Chromium, flatten tree inside the service worker
js = r"""
const { launch, settle } = require(process.argv[2] + '/lib');
(async () => {
  const { ctx, sw } = await launch(process.argv[3], {});
  const flat = await sw.evaluate(async () => { const out = []; (function w(n, p) { out.push({ id: n.id, parent: p, title: n.title, url: n.url, folder: !n.url }); for (const c of (n.children || [])) w(c, n.id); })((await chrome.bookmarks.getTree())[0], null); return out; });
  require('fs').writeFileSync(process.argv[4], JSON.stringify(flat));
  await sw.evaluate(async () => { const top = (await chrome.bookmarks.getTree())[0].children; const o = top.find(n => n.folderType === 'other'); const n = await chrome.bookmarks.create({ parentId: o.id, title: 'touch', url: 'https://touch.example/' }); await chrome.bookmarks.remove(n.id); });
  await settle(3500); await ctx.close();
})().catch(e => { console.error(e); process.exit(1); });
"""
(W / "load.js").write_text(js)
r = subprocess.run(["node", str(W / "load.js"), str(HERE), str(W / "udd"), str(W / "flat.json")], capture_output=True, text=True, timeout=120)
print("chromium run rc", r.returncode, r.stderr[-300:])
flat = json.loads((W / "flat.json").read_text())
by_id = {n["id"]: n for n in flat}
def path(n):
    out = []
    while n["parent"] is not None:
        n = by_id[n["parent"]]
        out.append(n["title"])
    return list(reversed(out))[1:]
loaded = {}
for n in flat:
    if not n["folder"]:
        loaded[n["title"] if n["title"].startswith(("url ", "in folder ")) else n["title"]] = n
# expectations
res = []
def show(s, n=60):
    s = repr(s)
    return s if len(s) <= n else s[: n // 2] + f"...<{len(s)} chars>..." + s[-n // 4:]
print("\n== titles (library -> Chromium)")
for i, t in enumerate(titles):
    got = [n for n in flat if not n["folder"] and n["url"] == f"https://t{i}.example/"]
    if not got:
        print(f"   DROPPED  {show(t)}")
    else:
        g = got[0]["title"]
        print(f"   {'same   ' if g == t else 'CHANGED'}  {show(t)} -> {show(g)}")
print("\n== urls (library -> Chromium)")
for i, u in enumerate(urls):
    got = [n for n in flat if not n["folder"] and n["title"] == f"url {i}"]
    if not got:
        print(f"   DROPPED  {show(u)}")
    else:
        g = got[0]["url"]
        print(f"   {'same   ' if g == u else 'CHANGED'}  {show(u)} -> {show(g)}")
print("\n== folder names (library -> Chromium)")
for i, f in enumerate(folders):
    got = [n for n in flat if not n["folder"] and n["title"] == f"in folder {i}"]
    if not got:
        print(f"   DROPPED  {show(f)}")
    else:
        p = path(got[0])
        g = p[-1] if p else None
        print(f"   {'same   ' if g == f else 'CHANGED'}  {show(f)} -> {show(g)}   (path {[x[:20] for x in p]})")
rew = json.loads((W / "udd/Default/Bookmarks").read_text(encoding="utf-8"))
print("\nverify_chrome on the file Chromium rewrote:", formats.verify_chrome(rew))
print("files:", sorted(p.name for p in (W / "udd/Default").iterdir() if "ookmark" in p.name))
