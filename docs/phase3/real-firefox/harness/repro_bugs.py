#!/usr/bin/env python3
"""Reproduce each Firefox/Gecko finding against a tree of the mhbm package.  No browser needed: the inputs are files REAL Firefox 157
wrote (tests/fixtures/firefox/) and profile layouts shaped like the ones real Firefox/Zen/Floorp created (see REPORT.md).

usage: python3 -I repro_bugs.py <skill-dir-containing-scripts/> <fixtures-dir>
prints one line per finding: 'ok' = behaves, 'BUG' = reproduced."""
from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

tree, fixtures = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
sys.dont_write_bytecode = True
sys.path.insert(0, str(tree / "scripts"))
from mhbm import browsers, formats  # noqa: E402

work = Path(tempfile.mkdtemp(prefix="repro-"))
db_bytes = gzip.open(fixtures / "firefox/places-firefox157.sqlite.gz").read()
BACKUP = fixtures / "firefox/bookmarks-firefox157.jsonlz4"
out = []


def report(fid, title, bug, detail=""):
    out.append(f"{fid}  {'BUG' if bug else 'ok '}  {title}" + (f"   [{detail}]" if detail else ""))


def put_db(dest: Path, marker: str = ""):
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(db_bytes)
    if marker:
        c = sqlite3.connect(dest)
        c.execute("INSERT INTO moz_places (url, url_hash, guid, rev_host) VALUES (?, 0, ?, '')", (f"https://{marker}.example.org/", marker.ljust(12, "x")[:12]))
        c.execute("INSERT INTO moz_bookmarks (type, fk, parent, position, title, dateAdded, lastModified, guid) VALUES (1, last_insert_rowid(), 5, 99, ?, 1, 1, ?)", (marker, marker.ljust(12, "y")[:12]))
        c.commit()
        c.close()


def with_home(home: Path, system="Linux", **env):
    import platform
    return [mock.patch.object(Path, "home", return_value=home), mock.patch.object(platform, "system", return_value=system),
            mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": "", "APPDATA": str(home / "AppData/Roaming"), **env})]


def run_ctx(ctxs, fn):
    for c in ctxs:
        c.start()
    try:
        return fn()
    finally:
        for c in ctxs:
            c.stop()


# FF-01 fresh Linux Firefox (>= 147) keeps its profile under ~/.config/mozilla/firefox
h = work / "h1"
put_db(h / ".config/mozilla/firefox/wtr793rw.default-release/places.sqlite")
(h / ".config/mozilla/firefox/profiles.ini").write_text("[Profile0]\nName=default-release\nIsRelative=1\nPath=wtr793rw.default-release\n")
found = run_ctx(with_home(h), lambda: browsers.find_profiles("firefox"))
report("FF-01", "profile under ~/.config/mozilla/firefox is found (Firefox >= 147 default on Linux)", not found, f"found {len(found)}")

# FF-02 two profiles: the install default must win over alphabetical order and over the legacy Default=1 marker
h = work / "h2"
r = h / ".mozilla/firefox"
put_db(r / "zzzz9999.default-release/places.sqlite", "realprofile")
put_db(r / "aaaa0000.default/places.sqlite", "legacyprofile")
(r / "profiles.ini").write_text("[Profile0]\nName=default-release\nIsRelative=1\nPath=zzzz9999.default-release\n\n[Profile1]\nName=default\nIsRelative=1\nPath=aaaa0000.default\nDefault=1\n\n[InstallC107BF34762FDEC5]\nDefault=zzzz9999.default-release\nLocked=1\n")
(r / "installs.ini").write_text("[C107BF34762FDEC5]\nDefault=zzzz9999.default-release\nLocked=1\n")
titles = run_ctx(with_home(h), lambda: [b.title for b in browsers.load("firefox")])
report("FF-02", "import-browser firefox reads the profile the installation really uses", "realprofile" not in titles, "read the OTHER profile" if "realprofile" not in titles else "")

# FF-03 / FF-04 backup vs sqlite consistency
a = browsers.load("firefox", profile=str(put_db(work / "a/places.sqlite") or work / "a/places.sqlite"))
b = browsers.load("firefox", profile=str(BACKUP))
report("FF-03", "jsonlz4 backup names the Other bookmarks root like the sqlite reader", any(x.folder.startswith("unfiled") for x in b),
       f"{sum(1 for x in b if x.folder.startswith('unfiled'))} bookmarks under 'unfiled/'")
report("FF-04", "jsonlz4 backup skips place: queries like the sqlite reader", any(x.url.startswith("place:") for x in b),
       f"{sum(1 for x in b if x.url.startswith('place:'))} place: URLs imported")

# FF-05 profile folder
try:
    browsers.load("firefox", profile=str(work / "a"))
    bug5 = False
except Exception as e:
    bug5 = True
    d5 = f"{type(e).__name__}: {str(e).replace(str(work), '')[:50]}"
report("FF-05", "--profile accepts a profile folder", bug5, d5 if bug5 else "")

# FF-06 corrupt db -> friendly error
junk = work / "junk/places.sqlite"
junk.parent.mkdir()
junk.write_bytes(b"this is not a database")
try:
    browsers.load("firefox", profile=str(junk))
    bug6, d6 = False, ""
except formats.FormatError:
    bug6, d6 = False, ""
except Exception as e:
    bug6, d6 = True, f"{type(e).__name__} traceback"
report("FF-06", "unreadable places.sqlite gives a clean FormatError", bug6, d6)

# FF-07 temp dir with URI metacharacters
bad7 = []
for odd in ("with#hash", "with%41pct"):
    base = work / odd
    base.mkdir()
    with mock.patch.object(tempfile, "tempdir", str(base)):
        try:
            browsers.load("firefox", profile=str(work / "a/places.sqlite"))
        except Exception as e:
            bad7.append(f"{odd}: {type(e).__name__}")
report("FF-07", "works when the temp dir path contains # or %", bool(bad7), ", ".join(bad7))

# FF-08 db copied before / WAL copied after a checkpoint
d8 = work / "race"
d8.mkdir()
live = sqlite3.connect(d8 / "places.sqlite")
live.execute("PRAGMA page_size=1024")
live.execute("PRAGMA journal_mode=WAL")
live.execute("PRAGMA wal_autocheckpoint=0")
live.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
live.execute("CREATE INDEX tv ON t(v)")


def add(lo, hi):
    live.executemany("INSERT INTO t(v) VALUES (?)", [(f"row-{i:05d}-" + "x" * 80,) for i in range(lo, hi)])
    live.commit()


add(0, 1500)
live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
add(1500, 3000)
real, hit = shutil.copy2, []


def racing(src, dst, *a, **k):
    res = real(src, dst, *a, **k)
    if str(src).endswith("places.sqlite") and not hit:
        hit.append(1)
        live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        add(3000, 3600)
    return res


with mock.patch.object(shutil, "copy2", racing), mock.patch.object(time, "sleep"):
    try:
        dest = browsers._copy(d8 / "places.sqlite")
        c = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
        try:
            integ = c.execute("pragma integrity_check").fetchall()
            n = c.execute("select count(*) from t").fetchone()[0]
        except sqlite3.DatabaseError as e:
            integ, n = [(str(e),)], None
        c.close()
        bug8 = integ != [("ok",)] or n != 3600
        d8s = f"rows={n}, integrity_check={integ[0][0][:40]!r}"
    except formats.FormatError as e:
        bug8, d8s = False, "refused"
live.close()
report("FF-08", "copy of a live db+WAL is consistent across a checkpoint", bug8, d8s)

# FF-09 LZ4 length bomb
bomb = bytes([0x1F]) + b"a" + bytes([1, 0]) + b"\xff" * 60_000 + bytes([0])
t = time.monotonic()
try:
    browsers.lz4_block_decompress(bomb, 100)
except formats.FormatError:
    pass
el = time.monotonic() - t
report("FF-09", "LZ4 match-length bomb refused before copying (60 KB of 0xFF)", el > 0.3, f"took {el:.2f}s; 5 MB of 0xFF would claim ~1.3 GB")

# FF-10 milliseconds ADD_DATE that Firefox multiplied by 10**6
report("FF-10", "to_unix undoes ms-as-seconds x 10**6 (1.6e18)", formats.to_unix(1600000000000000000) != 1600000000, f"got {formats.to_unix(1600000000000000000)}")

# FF-11 forks
h = work / "h11"
put_db(h / ".config/zen/evl81bnf.Default (release)/places.sqlite")
put_db(h / ".config/floorp/t7pbp1ba.default-default/places.sqlite")
det = run_ctx(with_home(h), browsers.detect)
report("FF-11", "Zen and Floorp profiles are detected", not ({"zen", "floorp"} <= set(det)), f"detected {sorted(det)}")

# FF-12 Firefox JSON backup passed with a .json name
(work / "bk.json").write_text(json.dumps({"guid": "root________", "root": "placesRoot", "typeCode": 2, "children": [
    {"root": "toolbarFolder", "typeCode": 2, "title": "toolbar", "children": [{"typeCode": 1, "uri": "https://j.example.org/", "title": "J"}]}]}))
try:
    got = browsers.load("firefox", profile=str(work / "bk.json"))
    bug12, d12 = len(got) != 1, ""
except Exception as e:
    bug12, d12 = True, f"{type(e).__name__}"
report("FF-12", "a Firefox .json backup given to import-browser is read as Firefox JSON", bug12, d12)

# FF-13 exporter marks Other bookmarks for Firefox
txt = formats.write_netscape([formats.Bookmark(url="https://o.example.org/", folder="Other bookmarks")], {"Other bookmarks"})
report("FF-13", "HTML export marks 'Other bookmarks' UNFILED_BOOKMARKS_FOLDER (Firefox import)", "UNFILED_BOOKMARKS_FOLDER" not in txt)

print("\n".join(out))
shutil.rmtree(work, ignore_errors=True)
