#!/usr/bin/env python3
"""Does this browser renumber ids when the checksum is wrong / an id is duplicated?  Prints one JSON line.
usage: probe_checksum.py <skill_dir> <base_bookmarks.json> <skeleton> <workdir> <browser-binary>"""
import json, os, shutil, subprocess, sys
from pathlib import Path
skill, base, skeleton, workdir, binary = sys.argv[1:6]
sys.path.insert(0, os.path.join(skill, "scripts"))
from mhbm import formats
HERE = Path(__file__).resolve().parent
d0 = json.loads(Path(base).read_text())
def run(name, mutate):
    d = json.loads(json.dumps(d0)); mutate(d)
    w = Path(workdir) / name; shutil.rmtree(w, ignore_errors=True); shutil.copytree(skeleton, w / "udd")
    for f in ("Bookmarks", "Bookmarks.bak"): (w / "udd/Default" / f).unlink(missing_ok=True)
    (w / "udd/Default/Bookmarks").write_text(json.dumps(d, indent=3, ensure_ascii=False))
    js = "const {launch}=require(process.argv[2]+'/lib');(async()=>{const {ctx,sw}=await launch(process.argv[3],{bin:process.argv[5]});const ids=await sw.evaluate(async()=>{const o=[];(function w(n){o.push(n.id);(n.children||[]).forEach(w)})((await chrome.bookmarks.getTree())[0]);return o});require('fs').writeFileSync(process.argv[4],JSON.stringify(ids));await ctx.close()})()"
    (w / "p.js").write_text(js)
    subprocess.run(["node", str(w / "p.js"), str(HERE), str(w / "udd"), str(w / "ids.json"), binary], check=True, timeout=90)
    got = json.loads((w / "ids.json").read_text())
    want = ["0"] + [n for n in _ids(d) if n]
    return sum(1 for a, b in zip(got, want) if a == b), len(want)
def _ids(d):
    out = []
    def w(n):
        out.append(str(n["id"]))
        for c in n.get("children", []): w(c)
    for k in ("bookmark_bar", "other", "synced"): w(d["roots"][k])
    return out
def wrong_sum(d): d["checksum"] = "0" * 32
def dup_id(d):
    ch = d["roots"]["bookmark_bar"]["children"]; ch[1]["id"] = ch[0]["id"]; d["checksum"] = formats.chrome_checksum(d["roots"])
ok = run("control", lambda d: None); w = run("wrong_checksum", wrong_sum); du = run("duplicate_id", dup_id)
print(json.dumps({"binary": binary.split("/")[-4:-1], "control_ids_kept": ok, "wrong_checksum_ids_kept": w, "duplicate_id_ids_kept": du,
                  "verdict": "checksum mismatch renumbers ALL ids" if w[0] < w[1] * 0.5 else "checksum mismatch ignored"}))
