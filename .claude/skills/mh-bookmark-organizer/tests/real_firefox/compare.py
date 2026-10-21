#!/usr/bin/env python3
"""Compare what mhbm reads from a Gecko-written profile with what Gecko itself says is in it (<profile>.truth.json).

usage: MHBM_SCRIPTS=<skill>/scripts python3 -I compare.py <profile-dir> [--sources sqlite,jsonlz4,json,html] [--mhbm-dir DIR]
Prints a compact diff per source; exit status 1 if any source differs from the truth.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT_NAMES = {"menu________": "Bookmarks menu", "toolbar_____": "Bookmarks bar", "unfiled_____": "Other bookmarks",
              "mobile______": "Mobile bookmarks"}


def esc(name: str) -> str:
    return name.replace("/", "∕") if name.strip() else ""


def _clean_tags(tags):
    """mhbm's own tag normalisation (lower-case, spaces -> '-', drop empties); imported lazily so the module imports without mhbm."""
    sys.dont_write_bytecode = True
    sys.path.insert(0, os.environ.get("MHBM_SCRIPTS", ""))  # -I ignores PYTHONPATH on purpose
    from mhbm import model
    return model.clean_tags(tags)


def truth_items(truth: dict, include_place_queries: bool) -> list:
    """Flatten Gecko's own tree to (folder, url, title, tags, added_s)."""
    out = []

    def walk(node, path):
        t = node.get("type")
        if t == "text/x-moz-place":
            url = node.get("uri", "")
            if url.startswith("place:") and not include_place_queries:
                return
            tags = sorted(_clean_tags((node.get("tags") or "").split(",")))
            added = int(node.get("dateAdded") or 0) // 1_000_000
            if added > 10**11:  # a milliseconds ADD_DATE that Firefox multiplied by 10**6 again: mhbm undoes it (to_unix, > 10**17)
                added //= 1000
            out.append(("/".join(path), url, node.get("title", ""), tuple(tags), added))
        elif t == "text/x-moz-place-container":
            guid = node.get("guid")
            name = ROOT_NAMES.get(guid) or node.get("title") or ""
            sub = path + [esc(name)] if name and guid != "root________" else path
            for c in node.get("children", []):
                walk(c, sub)

    walk(truth["tree"], [])
    return out


def truth_folders(truth: dict) -> set:
    out = set()

    def walk(node, path):
        if node.get("type") == "text/x-moz-place-container":
            guid = node.get("guid")
            name = ROOT_NAMES.get(guid) or node.get("title") or ""
            sub = path + [esc(name)] if name and guid != "root________" else path
            if sub and guid != "root________":
                out.add("/".join(sub))
            for c in node.get("children", []):
                walk(c, sub)

    walk(truth["tree"], [])
    return out


def mhbm_items(parsed) -> list:
    return [(b.folder, b.url, b.title, tuple(sorted(b.tags)), b.added) for b in parsed]


def diff(name, got, want, folders_got, folders_want, limit=8) -> bool:
    cg, cw = Counter(got), Counter(want)
    missing, extra = cw - cg, cg - cw
    print(f"--- {name}: read {len(got)} bookmarks, truth {len(want)}; missing {sum(missing.values())}, unexpected {sum(extra.values())}")
    for label, c in (("MISSING (in Gecko, not read)", missing), ("UNEXPECTED (read, not in Gecko)", extra)):
        for row in list(c)[:limit]:
            print(f"    {label}: {tuple(str(x)[:70] for x in row)}")
    fm, fx = sorted(folders_want - folders_got), sorted(folders_got - folders_want)
    if fm or fx:
        print(f"    folders missing {len(fm)}: {[x[:60] for x in fm[:limit]]}")
        print(f"    folders unexpected {len(fx)}: {[x[:60] for x in fx[:limit]]}")
    return not (missing or extra or fm or fx)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("profile")
    ap.add_argument("--sources", default="sqlite,jsonlz4,json,html")
    ap.add_argument("--place-queries", action="store_true", help="expect place: URLs (saved queries) to be imported too (default: skipped)")
    args = ap.parse_args()
    sys.dont_write_bytecode = True
    sys.path.insert(0, os.environ.get("MHBM_SCRIPTS", ""))  # -I ignores PYTHONPATH on purpose
    from mhbm import browsers, formats

    prof = Path(args.profile)
    truth = json.loads(Path(str(prof) + ".truth.json").read_text(encoding="utf-8"))
    ok = True
    for src in args.sources.split(","):
        try:
            if src == "sqlite":
                parsed = browsers.load("firefox", profile=str(prof / "places.sqlite"))
            elif src == "jsonlz4":
                backups = sorted((prof / "bookmarkbackups").glob("bookmarks-*.jsonlz4"))
                parsed = browsers.load("firefox", profile=str(backups[-1]))
            elif src == "json":
                parsed = formats.read_file(str(prof) + ".backup.json", "firefox")
            elif src == "html":
                parsed = formats.read_file(str(prof) + ".export.html", "html")
            else:
                continue
        except Exception as e:
            print(f"--- {src}: FAILED {type(e).__name__}: {e}")
            ok = False
            continue
        want = truth_items(truth, include_place_queries=args.place_queries)
        got = mhbm_items(parsed)
        if src == "html":  # Firefox's own HTML export names its roots differently and has its own entity rules: compare content only
            got = [("",) + r[1:] for r in got]
            want = [("",) + r[1:] for r in want]
            ok &= diff(src, got, want, set(), set())
        else:
            ok &= diff(src, got, want, set(parsed.folders), truth_folders(truth))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
