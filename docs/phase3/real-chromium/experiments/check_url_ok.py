#!/usr/bin/env python3
"""Score a ``chrome_url_ok`` prototype against what real Chromium kept/dropped.  usage: check_url_ok.py <validity.json> <module.py> [<fuzz_canon.json> ...]"""
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("m", sys.argv[2]); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
rows = json.load(open(sys.argv[1]))
bad_ok = [r["url"] for r in rows if m.chrome_url_ok(r["url"]) and not r["kept"]]      # predicted fine, browser drops  (the dangerous direction)
bad_no = [r["url"] for r in rows if not m.chrome_url_ok(r["url"]) and r["kept"]]      # predicted bad, browser keeps   (harmless: we skip a URL we could have written)
print(f"battery: {len(rows)} urls; browser kept {sum(r['kept'] for r in rows)}; predictor wrong-OK={len(bad_ok)} wrong-BAD={len(bad_no)}")
print("   predicted OK but dropped:", [u[:50] for u in bad_ok]); print("   predicted BAD but kept  :", [u[:50] for u in bad_no])
for f in sys.argv[3:]:
    fz = [r for r in json.load(open(f)) if "canon" in r]
    wrong = [r["raw"] for r in fz if not m.chrome_url_ok(r["raw"])]
    print(f"fuzz {f.split('/')[-1]}: {len(fz)} urls accepted by Chromium; predictor says BAD for {len(wrong)}", [w[:50] for w in wrong[:5]])
