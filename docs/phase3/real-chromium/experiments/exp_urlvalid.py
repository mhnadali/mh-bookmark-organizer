#!/usr/bin/env python3
"""Which url strings does Chromium keep when it LOADS a Bookmarks file (vs silently dropping the node)?
Writes one url node per candidate into a valid file (checksum over all nodes), loads it in Chromium, lists the survivors.
usage: exp_urlvalid.py <skill_dir> <skeleton> <workdir> [--bin path]  -> <workdir>/validity.json"""
import json, os, shutil, subprocess, sys
from pathlib import Path
skill, skeleton, workdir = sys.argv[1:4]
binargs = sys.argv[4:6] if len(sys.argv) > 5 and sys.argv[4] == "--bin" else []
sys.path.insert(0, os.path.join(skill, "scripts"))
from mhbm import formats
HERE = Path(__file__).resolve().parent
W = Path(workdir); shutil.rmtree(W, ignore_errors=True); W.mkdir(parents=True); shutil.copytree(skeleton, W / "udd")
for f in ("Bookmarks", "Bookmarks.bak"):
    (W / "udd/Default" / f).unlink(missing_ok=True)
cands = ["", " ", "example.com", "www.example.com/x", "//example.com/x", "/relative/path", "?q=1", "#frag", "http://", "http:///x", "http://:80/", "http://.", "http://a..b/", "http://-a.com/",
 "http://a_b.com/", "http://[::1", "http://[::1]/", "http://[zz]/", "http://999.999.999.999/", "http://1.2.3/", "http://1.2.3.4.5/", "http://0x7f.1/", "http://example.com:99999/", "http://example.com:/x",
 "http://example.com:abc/", "http://user@/", "http://user:pw@example.com", "http://exa mple.com/", "http://exa<mple.com/", "http://ex%41mple.com/", "http://exa%mple.com/", "http://例え.jp/", "http://ex​ample.com/",
 "HTTP://EXAMPLE.COM", "http:example.com", "http:/example.com", "http:\\\\example.com", "https:\\example.com", "ht tp://x.com", "1http://x.com", "-http://x.com", "://x.com", "http//x.com",
 "https://example.com", "ftp://", "ftp://h/", "ws://", "wss://h", "file:", "file:relative", "file:///", "file:///x", "file://host/x", "about:", "about:blank", "about:foo", "data:", "data:,x", "javascript:", "javascript:1",
 "mailto:", "mailto:a@b", "tel:", "chrome://", "chrome://settings", "chrome-extension://x/y", "view-source:", "blob:", "blob:http://x/y", "filesystem:http://x/t/f", "magnet:", "irc://", "ssh://h", "git@github.com:a/b.git",
 "localhost", "localhost:8080", "localhost:8080/x", "http://localhost", "192.168.0.1", "http://192.168.0.1", "a:b", "a:", ":", "x", "über.de", "https://über.de", "https://exa\tmple.com/", "https://exa\nmple.com/",
 "https://example.com/\u0000", "\u0000https://example.com/", "https://example.com/" + "a" * 70000, "https://" + "a" * 300 + ".com/", "https://" + ".".join(["a" * 63] * 5) + "/"]
if os.environ.get('MHBM_CANDIDATES'):
    cands = json.load(open(os.environ['MHBM_CANDIDATES']))   # e.g. a held-out list
nodes = []
for i, u in enumerate(cands):
    nodes.append({"id": str(10 + i), "name": f"c{i}", "type": "url", "url": u, "date_added": "13300000000000000", "date_last_used": "0", "guid": f"00000000-0000-4000-8000-{i + 1:012x}"})
roots = {k: {"id": str(i + 1), "name": n, "type": "folder", "guid": formats.CHROME_ROOT_GUIDS[k], "date_added": "13300000000000000", "date_last_used": "0", "date_modified": "0", "children": []} for i, (k, n) in enumerate(formats.CHROME_ROOTS.items())}
roots["other"]["children"] = nodes
(W / "udd/Default/Bookmarks").write_text(json.dumps({"checksum": formats.chrome_checksum(roots), "roots": roots, "version": 1}, indent=3, ensure_ascii=False))
js = r"""
const { launch } = require(process.argv[2] + '/lib');
(async () => { const { ctx, sw } = await launch(process.argv[3], {bin: process.argv[5] || undefined});
  const r = await sw.evaluate(async () => { const t = (await chrome.bookmarks.getTree())[0].children.find(n => n.folderType === 'other'); return t.children.map(c => ({ id: c.id, title: c.title, url: c.url })); });
  require('fs').writeFileSync(process.argv[4], JSON.stringify(r)); await ctx.close(); })();
"""
(W / "load.js").write_text(js)
subprocess.run(["node", str(W / "load.js"), str(HERE), str(W / "udd"), str(W / "kept.json"), *(binargs[1:] or [])], check=True, timeout=120)
kept = {n["title"]: n for n in json.loads((W / "kept.json").read_text())}
out = []
for i, u in enumerate(cands):
    k = kept.get(f"c{i}")
    out.append({"url": u, "kept": bool(k), "canon": k["url"] if k else None, "id_kept": bool(k) and k["id"] == str(10 + i)})
(W / "validity.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
ids_ok = sum(1 for o in out if o["id_kept"])
print(f"{sum(o['kept'] for o in out)} of {len(out)} candidates survive; ids of survivors preserved: {ids_ok}")
for o in out:
    u = o["url"] if len(o["url"]) < 60 else o["url"][:30] + f"...<{len(o['url'])} chars>"
    print(f"   {'KEPT   ' if o['kept'] else 'DROPPED'} {u!r:70} -> {(o['canon'] or '')[:60]!r}")
