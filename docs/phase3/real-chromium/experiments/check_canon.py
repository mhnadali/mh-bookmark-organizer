#!/usr/bin/env python3
"""Compare key functions against real Chromium's canonical URLs.  usage: check_canon.py <skill_dir> <fuzz_canon.json> [--proposed path/to/browser_key.py]"""
import importlib.util, json, os, sys
skill, data = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.join(skill, "scripts"))
from mhbm import urlnorm
rows = [r for r in json.load(open(data)) if "canon" in r]
fns = {"current normalize_url": urlnorm.normalize_url}
if "--proposed" in sys.argv:
    p = sys.argv[sys.argv.index("--proposed") + 1]
    spec = importlib.util.spec_from_file_location("bk", p); mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    fns["proposed browser_key"] = lambda u: mod.browser_key(u, urlnorm.is_tracking, urlnorm._route_fragment)
for name, fn in fns.items():
    bad = [r for r in rows if fn(r["raw"]) != fn(r["canon"])]
    print(f"{name}: {len(bad)} of {len(rows)} raw URLs get a different key than Chromium's canonical form")
    for r in bad[:int(os.environ.get('SHOW', '12'))]:
        print("   raw   ", repr(r["raw"])[:110]); print("   canon ", repr(r["canon"])[:110]); print("   keys  ", repr(fn(r["raw"]))[:100], "|", repr(fn(r["canon"]))[:100])
