#!/usr/bin/env python3
"""Turn a profile written by REAL Gecko (build_profile.py --extras, clean exit) into small committed test fixtures.

usage: python3 -I make_fixture.py <profile-dir> <out-dir>
  writes  <out>/places-firefox<MAJOR>.sqlite.gz   VACUUM INTO copy of the real places.sqlite (still Firefox's own schema, 32 KiB pages)
          <out>/bookmarks-firefox<MAJOR>.jsonlz4   the real bookmarkbackups file, byte for byte
          <out>/expected-firefox<MAJOR>.json       what Gecko itself reported (PlacesUtils.promiseBookmarksTree) flattened as mhbm should read it
          <out>/PROVENANCE.txt
Everything in the fixtures is synthetic (example.org hosts, invented titles).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compare


def main() -> None:
    prof, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    truth = json.loads(Path(str(prof) + ".truth.json").read_text(encoding="utf-8"))
    major = truth["version"].split(".")[0]
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    shutil.copy(prof / "places.sqlite", tmp / "in.sqlite")
    if (prof / "places.sqlite-wal").exists():
        shutil.copy(prof / "places.sqlite-wal", tmp / "in.sqlite-wal")
    c = sqlite3.connect(tmp / "in.sqlite")
    c.execute(f"VACUUM INTO '{tmp / 'out.sqlite'}'")
    c.close()
    raw = (tmp / "out.sqlite").read_bytes()
    with open(out / f"places-firefox{major}.sqlite.gz", "wb") as f, gzip.GzipFile(fileobj=f, mode="wb", mtime=0, compresslevel=9) as z:
        z.write(raw)
    backups = sorted((prof / "bookmarkbackups").glob("bookmarks-*.jsonlz4"))
    shutil.copy(backups[-1], out / f"bookmarks-firefox{major}.jsonlz4")
    rows = compare.truth_items(truth, include_place_queries=False)
    expected = {"bookmarks": [{"folder": r[0], "url": r[1], "title": r[2], "tags": list(r[3]), "added": r[4]} for r in rows],
                "folders": sorted(compare.truth_folders(truth))}
    (out / f"expected-firefox{major}.json").write_text(json.dumps(expected, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    (out / "PROVENANCE.txt").write_text(
        f"Written by a real Mozilla Firefox {truth['version']} (buildID {truth['buildID']}), headless, in a network namespace with only loopback.\n"
        f"Source: a synthetic Netscape bookmarks file imported through browser.places.importBookmarksHTML, plus PlacesUtils API calls\n"
        f"(mobile root, API tags, a keyword with POST data, separators, a place: query).  See tests/real_firefox/.\n"
        f"places-firefox{major}.sqlite.gz: VACUUM INTO of the closed profile's places.sqlite (sha256 of the uncompressed file {hashlib.sha256(raw).hexdigest()}).\n"
        f"bookmarks-firefox{major}.jsonlz4: the file Firefox wrote to bookmarkbackups/, unmodified.\n"
        f"expected-firefox{major}.json: Gecko's own view of the tree, flattened the way mhbm names roots (menu->Bookmarks menu, toolbar->Bookmarks bar,\n"
        f"unfiled->Other bookmarks, mobile->Mobile bookmarks; place: queries excluded; tags normalised like model.clean_tags).\n", encoding="utf-8")
    shutil.rmtree(tmp)
    print("wrote", sorted(os.listdir(out)))


if __name__ == "__main__":
    main()
