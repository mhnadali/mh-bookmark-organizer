#!/usr/bin/env python3
"""Find the folder nesting depth at which Chromium silently discards the whole Bookmarks file (JSON parser depth limit).
usage: exp_depth.py <skill_dir> <profile_skeleton_dir> <workdir> [lo hi]"""
import json, os, shutil, subprocess, sys
from pathlib import Path
skill, skeleton, workdir = sys.argv[1:4]
lo, hi = (int(sys.argv[4]), int(sys.argv[5])) if len(sys.argv) > 5 else (50, 300)
sys.path.insert(0, os.path.join(skill, "scripts"))
from mhbm import formats
HERE = Path(__file__).resolve().parent


def make(depth):
    roots = {k: {"id": str(i + 1), "name": n, "type": "folder", "guid": formats.CHROME_ROOT_GUIDS[k], "date_added": "13300000000000000",
                 "date_last_used": "0", "date_modified": "0", "children": []} for i, (k, n) in enumerate(formats.CHROME_ROOTS.items())}
    node, nid = roots["other"], 10
    for i in range(depth):
        nid += 1
        child = {"id": str(nid), "name": f"d{i}", "type": "folder", "date_added": "13300000000000000", "date_last_used": "0", "date_modified": "0",
                 "guid": f"00000000-0000-4000-8000-{nid:012x}", "children": []}
        node["children"].append(child)
        node = child
    node["children"].append({"id": "9999", "name": "bottom", "type": "url", "url": "https://bottom.example/", "date_added": "13300000000000000",
                             "date_last_used": "0", "guid": "00000000-0000-4000-8000-00000000ffff"})
    return {"checksum": formats.chrome_checksum(roots), "roots": roots, "version": 1}


def survives(depth):
    d = Path(workdir) / f"depth{depth}"
    shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(skeleton, d / "udd")
    for f in ("Bookmarks", "Bookmarks.bak"):
        (d / "udd/Default" / f).unlink(missing_ok=True)
    (d / "udd/Default/Bookmarks").write_text(json.dumps(make(depth), indent=3))
    try:
        out = subprocess.run(["node", str(HERE / "exp_count.js"), str(d / "udd")], capture_output=True, text=True, timeout=25, check=True).stdout
    except subprocess.TimeoutExpired:
        return True, -1      # file was accepted but chrome.bookmarks.getTree() never answered (observed at depth 97)
    n = json.loads(out.strip().splitlines()[-1])["nodes"]
    return n > 3, n


while lo + 1 < hi:
    mid = (lo + hi) // 2
    ok, n = survives(mid)
    print(f"depth {mid}: {'loaded' if ok else 'DISCARDED'} ({n} nodes)", flush=True)
    lo, hi = (mid, hi) if ok else (lo, mid)
print(f"largest surviving folder depth below a root: {lo}; first discarded: {hi}")
