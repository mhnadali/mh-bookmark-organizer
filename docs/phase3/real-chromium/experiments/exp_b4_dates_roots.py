#!/usr/bin/env python3
"""Experiment B4: dates (0, 1970, now, 2100, 3000, 10000+, negative) and all three roots, incl. 'Mobile bookmarks', through install-chrome -> Chromium.
usage: exp_b4_dates_roots.py <skill_dir> <workdir> <profile_skeleton_dir>"""
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
dates = {"zero": 0, "one": 1, "y1970": 86400, "y2001": 1000000000, "y2023": 1700000000, "y2100": 4102444800, "y3000": 32503680000, "y9999": 253402300799, "y10000": 253402300800, "huge": 10**15}
bms = [Bookmark(url=f"https://d-{k}.example/", title=f"date {k}", folder="Bookmarks bar/Dates", added=v) for k, v in dates.items()]
bms += [Bookmark(url="https://m1.example/", title="mobile loose", folder="Mobile bookmarks"),
        Bookmark(url="https://m2.example/", title="mobile nested", folder="Mobile bookmarks/Phone/Reading"),
        Bookmark(url="https://o1.example/", title="other loose", folder="Other bookmarks"),
        Bookmark(url="https://b1.example/", title="bar loose", folder="Bookmarks bar")]
lib = Library(str(W / "home")); assign_ids(bms, lib.ids()); lib.upsert(bms); lib.add_folders(["Bookmarks bar/Dates", "Mobile bookmarks/Phone/Reading"]); lib.close()
(W / "udd/Default/Bookmarks").write_text("{}")
env = dict(os.environ, PYTHONPATH=os.path.join(skill, "scripts"))
subprocess.run([sys.executable, "-m", "mhbm", "--home", str(W / "home"), "install-chrome", str(W / "udd/Default/Bookmarks"), "--confirm-chrome-closed"], env=env, capture_output=True, text=True, check=True)
written = json.loads((W / "udd/Default/Bookmarks").read_text(encoding="utf-8"))
shutil.copy(W / "udd/Default/Bookmarks", W / "written.json")
print("verify_chrome(written):", formats.verify_chrome(written))
js = r"""
const { launch, settle } = require(process.argv[2] + '/lib');
(async () => {
  const { ctx, sw } = await launch(process.argv[3], {});
  const flat = await sw.evaluate(async () => { const out = []; (function w(n, p) { out.push({ id: n.id, parent: p, title: n.title, url: n.url, dateAdded: n.dateAdded, folderType: n.folderType, n: (n.children || []).length }); for (const c of (n.children || [])) w(c, n.id); })((await chrome.bookmarks.getTree())[0], null); return out; });
  require('fs').writeFileSync(process.argv[4], JSON.stringify(flat));
  await sw.evaluate(async () => { const top = (await chrome.bookmarks.getTree())[0].children; const o = top.find(n => n.folderType === 'other'); const n = await chrome.bookmarks.create({ parentId: o.id, title: 'touch', url: 'https://touch.example/' }); await chrome.bookmarks.remove(n.id); });
  await settle(3500); await ctx.close();
})().catch(e => { console.error(e); process.exit(1); });
"""
(W / "load.js").write_text(js)
r = subprocess.run(["node", str(W / "load.js"), str(HERE), str(W / "udd"), str(W / "flat.json")], capture_output=True, text=True, timeout=120)
print("chromium rc", r.returncode, r.stderr[-200:])
flat = json.loads((W / "flat.json").read_text())
print("top-level folders in getTree:", [(n["title"], n["folderType"], n["n"]) for n in flat if n["parent"] == "0"])
rew = json.loads((W / "udd/Default/Bookmarks").read_text(encoding="utf-8"))
def urls(d):
    out = {}
    def w(n):
        if n.get("type") == "url": out[n["url"]] = n
        for c in n.get("children", []): w(c)
    for r_ in d["roots"].values(): w(r_)
    return out
uw, ur = urls(written), urls(rew)
print("\n== dates   (library unix seconds -> date_added in our file -> getTree dateAdded ms -> date_added after Chromium rewrite)")
byurl = {n["url"]: n for n in flat if n.get("url")}
for k, v in dates.items():
    u = f"https://d-{k}.example/"
    print(f"   {k:7s} ts={v!s:16s} file={uw[u]['date_added']:>22s}  getTree={byurl[u].get('dateAdded', 'undefined')!s:>20}  rewritten={ur[u]['date_added'] if u in ur else 'MISSING':>22}")
print("\n== roots")
for t in ("https://m1.example/", "https://m2.example/", "https://o1.example/", "https://b1.example/"):
    print("  ", t, "present after load:", t in byurl)
print("verify_chrome(rewritten):", formats.verify_chrome(rew))
