#!/usr/bin/env python3
"""Compare two bookmark trees structurally (order-preserving).

Accepts either a Chromium ``Bookmarks`` file ({"roots": {...}}) or the JSON produced by ``chrome.bookmarks.getTree()``
(a list with one root node whose children are the permanent folders).  Prints a flat list of differences; exit status 1
if there are any.

usage: cmp_trees.py A.json B.json [--ignore-order] [--ignore-dates] [--ids] [--show N]
"""
import json
import sys

WEBKIT = 11644473600


def load(path):
    d = json.load(open(path, encoding="utf-8"))
    if isinstance(d, dict) and "tree" in d and isinstance(d["tree"], list):   # exp_a_create.js output
        d = d["tree"]
    return d


def flat(d):
    """-> list of (path, index, kind, title, url, date_ms_or_None) in document order."""
    out = []

    def walk(node, path, idx):
        kind = node.get("type") or ("folder" if "children" in node else "url")
        title = node.get("name", node.get("title", ""))
        url = node.get("url")
        da = node.get("date_added", node.get("dateAdded"))
        if da is not None and "date_added" in node:
            da = int(da) // 1000 - WEBKIT * 1000      # webkit µs -> unix ms
        out.append((tuple(path), idx, kind, title, url, da, str(node.get("id"))))
        for i, c in enumerate(node.get("children", [])):
            walk(c, path + [title], i)

    if isinstance(d, list):                       # getTree
        for i, top in enumerate(d[0].get("children", [])):
            walk(top, [], i)
    else:
        names = {"bookmark_bar": 0, "other": 1, "synced": 2}
        for k, r in sorted(d["roots"].items(), key=lambda kv: names.get(kv[0], 9)):
            if isinstance(r, dict):
                if k == "synced" and not r.get("children"):
                    continue          # Chromium hides an empty 'Mobile bookmarks' root from chrome.bookmarks.getTree()
                walk(r, [], names.get(k, 9) if k != "synced" else len([1 for kk in d["roots"] if kk in ("bookmark_bar", "other")]))
    return out


def _clip(x, n=70):
    """Shorten every long string inside a (nested) tuple so a 15 kB URL does not flood the terminal."""
    if isinstance(x, str):
        return x if len(x) <= n else f"{x[:n // 2]}...<{len(x)} chars>...{x[-n // 2:]}"
    if isinstance(x, tuple):
        return tuple(_clip(i, n) for i in x)
    return x


def main(argv):
    a, b = load(argv[1]), load(argv[2])
    ign_date = "--ignore-dates" in argv
    show = int(argv[argv.index("--show") + 1]) if "--show" in argv else 40
    fa, fb = flat(a), flat(b)
    # Sync roots and "Mobile bookmarks" may be missing from getTree when empty: compare only what both have.
    with_ids = "--ids" in argv
    strip = lambda f: [(p, i, k, t, u, None if ign_date else dt) + ((nid,) if with_ids else ()) for (p, i, k, t, u, dt, nid) in f]
    sa, sb = strip(fa), strip(fb)
    diffs = []
    if "--ignore-order" in argv:
        from collections import Counter
        ca, cb = Counter(x[0:1] + x[2:] for x in sa), Counter(x[0:1] + x[2:] for x in sb)  # drops the sibling index
        for k in (ca - cb): diffs.append(("only in A", k))
        for k in (cb - ca): diffs.append(("only in B", k))
    else:
        n = max(len(sa), len(sb))
        for i in range(n):
            x = sa[i] if i < len(sa) else None
            y = sb[i] if i < len(sb) else None
            if x != y:
                diffs.append((f"#{i}", x, y))
    print(f"A: {len(fa)} nodes, B: {len(fb)} nodes, differences: {len(diffs)}")
    for d in diffs[:show]:
        print("  ", _clip(d))
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
