#!/usr/bin/env python3
"""Compare the SOURCE Netscape file (read with mhbm's own HTML parser) with what mhbm reads back from the profile real Gecko wrote from it.
usage: MHBM_SCRIPTS=... python3 -I compare_source_html.py <source.html> <profile-dir>"""
import collections
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.environ["MHBM_SCRIPTS"])
from mhbm import browsers, formats  # noqa: E402

src = formats.read_file(sys.argv[1])
got = browsers.load("firefox", profile=sys.argv[2] + "/places.sqlite")
print(f"source: {len(src)} bookmarks, {len(src.folders)} folders;  read back from Gecko's places.sqlite: {len(got)} bookmarks, {len(got.folders)} folders")
ROOT = {"Bookmarks bar", "Other bookmarks", "Mobile bookmarks", "Bookmarks menu"}


def leaf(folder):
    return folder.split("/")[-1]


def key(b, with_title=True):
    return (leaf(b.folder) if b.folder not in ROOT else "", b.url, b.title if with_title else "", b.added, tuple(sorted(b.tags)))


a, b = collections.Counter(key(x) for x in src), collections.Counter(key(x) for x in got)
print("bookmarks only in source:", sum((a - b).values()), " only in Gecko readback:", sum((b - a).values()))
for k in list((a - b))[:6]:
    print("   source only:", tuple(str(x)[:60] for x in k))
for k in list((b - a))[:6]:
    print("   readback only:", tuple(str(x)[:60] for x in k))
fa, fb = {leaf(f) for f in src.folders}, {leaf(f) for f in got.folders}
print("folder names only in source:", sorted(fa - fb)[:8], " only in readback:", sorted(fb - fa)[:8])
print("persian titles:", sum(1 for x in src if any("؀" <= c <= "ۿ" for c in x.title)), "->", sum(1 for x in got if any("؀" <= c <= "ۿ" for c in x.title)))
print("empty titles:", sum(1 for x in src if not x.title), "->", sum(1 for x in got if not x.title))
