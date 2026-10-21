#!/usr/bin/env python3
"""Print the pin (file name, size, sha256, URL) for a Firefox build on conda-forge - a repack of Mozilla's official Linux build
(its distribution.ini says `mozilla-official`; the `.sig` files are inside).  Standard library only.

usage: python3 pin_firefox.py [VERSION]       (default: the newest in current_repodata)
Use the `sha256` it prints in CI and verify the download against it (tests/real_firefox/extract_conda.py does)."""
import bz2
import json
import sys
import urllib.request

BASE = "https://conda.anaconda.org/conda-forge/linux-64/"
with urllib.request.urlopen(BASE + "current_repodata.json.bz2", timeout=120) as r:
    data = json.loads(bz2.decompress(r.read()))
pk = {**data.get("packages", {}), **data.get("packages.conda", {})}
rows = sorted(((tuple(int(x) for x in m["version"].split(".") if x.isdigit()), m["build_number"], fn, m)
               for fn, m in pk.items() if m["name"] == "firefox" and (len(sys.argv) < 2 or m["version"] == sys.argv[1])), key=lambda t: t[:2])
if not rows:
    sys.exit("no such firefox build in current_repodata (older versions: use repodata.json)")
_, _, fn, m = rows[-1]
print(json.dumps({"file": fn, "version": m["version"], "size": m["size"], "sha256": m["sha256"], "url": BASE + fn}, indent=1))
