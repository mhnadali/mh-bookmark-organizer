"""Firefox / Gecko support against files REAL Firefox 157 wrote (tests/fixtures/firefox/), and profile discovery against the
layouts real Firefox, Zen and Floorp create.  Nothing here needs a browser: tests/test_real_firefox.py drives one when available."""
from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import common  # noqa: F401  (puts scripts/ on sys.path, sets MHBM_LANG)
from mhbm import browsers, formats, gecko
from mhbm.mcp import registry

FIX = Path(__file__).resolve().parent / "fixtures" / "firefox"
EXPECTED = json.loads((FIX / "expected-firefox157.json").read_text(encoding="utf-8"))

# profiles.ini / installs.ini exactly as Firefox 157.0.1 wrote them on a fresh Linux home (random names kept)
PROFILES_INI = """[General]
StartWithLastProfile=1
Version=2

[Profile0]
Name=default-release
IsRelative=1
Path=wtr793rw.default-release

[Profile1]
Name=default
IsRelative=1
Path=byprd30h.default
Default=1

[InstallC107BF34762FDEC5]
Default=wtr793rw.default-release
Locked=1
"""
INSTALLS_INI = """[C107BF34762FDEC5]
Default=wtr793rw.default-release
Locked=1
"""


def fixture_db(dest: Path, marker: str = "") -> Path:
    """Unpack the real places.sqlite; ``marker`` adds one extra bookmark so tests can tell profiles apart."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(FIX / "places-firefox157.sqlite.gz") as z:
        dest.write_bytes(z.read())
    if marker:
        c = sqlite3.connect(dest)
        try:
            c.execute("INSERT INTO moz_places (url, url_hash, guid, rev_host) VALUES (?, 0, ?, '')", (f"https://{marker}.example.org/", marker.ljust(12, "x")[:12]))
            fk = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("INSERT INTO moz_bookmarks (type, fk, parent, position, title, dateAdded, lastModified, guid) "
                      "VALUES (1, ?, 5, 99, ?, 1600000000000000, 1600000000000000, ?)", (fk, marker, marker.ljust(12, "y")[:12]))
            c.commit()
        finally:
            c.close()
    return dest


def flat(parsed) -> list:
    return sorted((b.folder, b.url, b.title, sorted(b.tags), b.added) for b in parsed)   # tag ORDER differs between sources, the set must not


def want() -> list:
    return sorted((b["folder"], b["url"], b["title"], sorted(b["tags"]), b["added"]) for b in EXPECTED["bookmarks"])


class RealFirefoxFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_places_sqlite_matches_what_gecko_reports(self):
        db = fixture_db(self.tmp / "places.sqlite")
        got = browsers.load("firefox", profile=str(db))
        self.assertEqual(flat(got), want())
        self.assertEqual(sorted(got.folders), EXPECTED["folders"])

    def test_jsonlz4_backup_matches_what_gecko_reports(self):
        got = browsers.load("firefox", profile=str(FIX / "bookmarks-firefox157.jsonlz4"))
        self.assertEqual(flat(got), want())
        self.assertEqual(sorted(got.folders), EXPECTED["folders"])

    def test_sqlite_and_backup_name_folders_alike(self):
        """Re-importing the same profile from its backup must not create a second 'unfiled/...' tree (real backups call the root
        `unfiledBookmarksFolder`) nor import `place:` queries that the sqlite reader skips."""
        a = browsers.load("firefox", profile=str(fixture_db(self.tmp / "places.sqlite")))
        b = browsers.load("firefox", profile=str(FIX / "bookmarks-firefox157.jsonlz4"))
        self.assertEqual(flat(a), flat(b))
        self.assertFalse([x for x in b if x.folder.startswith("unfiled") or x.url.startswith("place:")])

    def test_the_real_lz4_stream_decodes_to_the_declared_size(self):
        raw = (FIX / "bookmarks-firefox157.jsonlz4").read_bytes()
        out = browsers.read_mozlz4(FIX / "bookmarks-firefox157.jsonlz4")
        self.assertEqual(len(out), int.from_bytes(raw[8:12], "little"))
        self.assertEqual(json.loads(out)["root"], "placesRoot")

    def test_roots_tags_separators_keywords_and_odd_schemes(self):
        got = {b.title: b for b in browsers.load("firefox", profile=str(fixture_db(self.tmp / "places.sqlite")))}
        self.assertEqual(got["Mobile one"].folder, "Mobile bookmarks")
        self.assertEqual(got["Toolbar one"].folder, "Bookmarks bar")
        self.assertEqual(got["Unfiled one"].folder, "Other bookmarks")
        self.assertEqual(got["Menu item A"].tags, ["alpha", "beta"])
        self.assertEqual(got["API tagged"].tags, ["api-tag", "zeta", "تگب"])
        self.assertIn("Bookmarklet", got)                      # javascript: bookmarks are kept
        self.assertNotIn("A smart bookmark (place: query)", got)
        self.assertEqual(got["Bottom of a 12-level tree"].folder.count("/"), 13)
        self.assertEqual(got["ADD_DATE 2200"].added, 7258118400)
        self.assertEqual(got["ADD_DATE in milliseconds"].added, 1600000000)   # Firefox stored ms x 10**6

    def test_wal_sidecar_is_read(self):
        """An uncheckpointed commit lives only in places.sqlite-wal (a running or killed Firefox): the copy must carry it."""
        db = fixture_db(self.tmp / "live" / "places.sqlite")
        c = sqlite3.connect(db)
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA wal_autocheckpoint=0")
            c.execute("INSERT INTO moz_places (url, url_hash, guid, rev_host) VALUES ('https://only-in-wal.example.org/', 0, 'walwalwal001', '')")
            c.execute("INSERT INTO moz_bookmarks (type, fk, parent, position, title, dateAdded, lastModified, guid) "
                      "VALUES (1, last_insert_rowid(), 5, 99, 'only in WAL', 1600000000000000, 1600000000000000, 'walwalwal002')")
            c.commit()
            self.assertTrue(db.with_name("places.sqlite-wal").stat().st_size > 0)
            self.assertNotIn("only in WAL", [b.title for b in sqlite_without_wal(db, self.tmp)])
            self.assertIn("only in WAL", [b.title for b in browsers.load("firefox", profile=str(db))])
        finally:
            c.close()

    def test_copy_retries_when_the_browser_checkpoints_mid_copy_and_refuses_when_it_never_settles(self):
        db = fixture_db(self.tmp / "places.sqlite")
        real = browsers._fingerprint
        calls = {"n": 0}

        def flaky(path):
            calls["n"] += 1
            fp = real(path)
            return (fp[0] + (calls["n"] == 2), *fp[1:])     # the check right after the first copy sees the file move

        with mock.patch.object(browsers, "_fingerprint", flaky), mock.patch.object(time, "sleep"):
            self.assertEqual(flat(browsers.load("firefox", profile=str(db))), want())
            self.assertEqual(calls["n"], 4)                      # copy 1 rejected, copy 2 accepted
        always = iter(range(10**6))
        with mock.patch.object(browsers, "_fingerprint", lambda p: (next(always), 0, b"")), mock.patch.object(time, "sleep"):
            with self.assertRaisesRegex(formats.FormatError, "kept changing"):
                browsers.load("firefox", profile=str(db))

    def test_checkpoint_between_the_db_copy_and_the_wal_copy_is_survived(self):
        """The real race: the browser checkpoints and truncates its WAL right after we copied the db.  db(old) + wal(new) is a
        malformed database; the copy must notice, retry and hand back a consistent pair."""
        db = self.tmp / "live" / "places.sqlite"
        db.parent.mkdir()
        live = sqlite3.connect(db)
        self.addCleanup(live.close)
        for pragma in ("page_size=1024", "journal_mode=WAL", "wal_autocheckpoint=0"):
            live.execute("PRAGMA " + pragma)
        live.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        live.execute("CREATE INDEX tv ON t(v)")

        def add(lo, hi):
            live.executemany("INSERT INTO t(v) VALUES (?)", [(f"row-{i:05d}-" + "x" * 80,) for i in range(lo, hi)])
            live.commit()

        add(0, 1500)
        live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        add(1500, 3000)
        real, hits = shutil.copy2, []

        def racing(src, dst, *a, **k):
            out = real(src, dst, *a, **k)
            if str(src).endswith("places.sqlite") and not hits:
                hits.append(1)
                live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                add(3000, 3600)
            return out

        with mock.patch.object(shutil, "copy2", racing), mock.patch.object(time, "sleep"):
            dest = browsers._copy(db)
        self.addCleanup(shutil.rmtree, dest.parent, True)
        c = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
        try:
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(c.execute("SELECT count(*) FROM t").fetchone()[0], 3600)
        finally:
            c.close()

    def test_temp_dir_with_uri_special_characters(self):
        db = fixture_db(self.tmp / "places.sqlite")
        for odd in ("with#hash", "with%41pct"):
            base = self.tmp / odd
            base.mkdir()
            with mock.patch.object(tempfile, "tempdir", str(base)):
                self.assertEqual(len(browsers.load("firefox", profile=str(db))), len(EXPECTED["bookmarks"]))

    def test_unreadable_databases_give_a_format_error(self):
        junk = self.tmp / "junk" / "places.sqlite"
        junk.parent.mkdir()
        junk.write_bytes(b"this is not a database, just text")
        trunc = self.tmp / "trunc" / "places.sqlite"
        trunc.parent.mkdir()
        trunc.write_bytes(fixture_db(self.tmp / "full.sqlite").read_bytes()[:70000])
        for bad in (junk, trunc):
            with self.assertRaisesRegex(formats.FormatError, "not a readable Firefox places.sqlite"):
                browsers.load("firefox", profile=str(bad))

    def test_profile_folder_and_renamed_files_are_accepted(self):
        prof = self.tmp / "abcd.default-release"
        fixture_db(prof / "places.sqlite")
        self.assertEqual(flat(browsers.load("firefox", profile=str(prof))), want())
        shutil.copy(prof / "places.sqlite", self.tmp / "places-copy.bak")        # sniffed by its SQLite header
        self.assertEqual(flat(browsers.load("/ignored/by/profile", profile=str(self.tmp / "places-copy.bak"))), want())
        (prof / "places.sqlite").unlink()
        (prof / "bookmarkbackups").mkdir()
        shutil.copy(FIX / "bookmarks-firefox157.jsonlz4", prof / "bookmarkbackups" / "bookmarks-2026-10-15_30_x.jsonlz4")
        self.assertEqual(flat(browsers.load("firefox", profile=str(prof))), want())   # no db: newest backup
        with self.assertRaises(formats.FormatError):
            browsers.load("firefox", profile=str(self.tmp / "empty-folder-does-not-exist"))

    def test_firefox_json_backup_by_extension(self):
        raw = json.dumps({"guid": "root________", "root": "placesRoot", "typeCode": 2, "children": [
            {"root": "unfiledBookmarksFolder", "typeCode": 2, "title": "unfiled", "children": [
                {"typeCode": 1, "uri": "https://j.example.org/", "title": "J", "dateAdded": 1600000000000000}]}]})
        p = self.tmp / "bookmarks-2026-01-01.json"
        p.write_text(raw, encoding="utf-8")
        got = browsers.load("firefox", profile=str(p))
        self.assertEqual([(b.url, b.folder, b.added) for b in got], [("https://j.example.org/", "Other bookmarks", 1600000000)])


def sqlite_without_wal(db: Path, scratch: Path) -> formats.ParsedList:
    """What an exporter that copies only places.sqlite (ignoring the -wal) would see."""
    lone = scratch / "lone" / "places.sqlite"
    lone.parent.mkdir(exist_ok=True)
    shutil.copy2(db, lone)
    return browsers.parse_places(lone)


class Lz4Hardening(unittest.TestCase):
    def test_match_copies_overlapping_and_not(self):
        end = bytes([0])
        self.assertEqual(browsers.lz4_block_decompress(bytes([0x4F]) + b"abcd" + bytes([4, 0, 1]) + end, 24), b"abcd" * 6)     # offset < length, extended length
        self.assertEqual(browsers.lz4_block_decompress(bytes([0x36]) + b"abc" + bytes([3, 0]) + end, 13), b"abcabcabcabca")     # remainder of the pattern
        self.assertEqual(browsers.lz4_block_decompress(bytes([0xA2]) + b"0123456789" + bytes([10, 0]) + end, 16), b"0123456789012345")  # plain copy

    def test_length_bomb_is_refused_before_anything_is_copied(self):
        """A match length is a run of 0xFF bytes: 200 KB of them claim ~50 MB (5 MB of them ~1.3 GB) while the header says 100 bytes."""
        bomb = bytes([0x1F]) + b"a" + bytes([1, 0]) + b"\xff" * 200_000 + bytes([0])
        started = time.monotonic()
        with self.assertRaisesRegex(formats.FormatError, "output too large"):
            browsers.lz4_block_decompress(bomb, 100)
        self.assertLess(time.monotonic() - started, 2.0)


class HtmlExportForFirefox(unittest.TestCase):
    def test_other_bookmarks_is_marked_as_firefoxs_unfiled_root(self):
        items = [formats.Bookmark(url="https://t.example.org/", title="T", folder="Bookmarks bar"),
                 formats.Bookmark(url="https://o.example.org/", title="O", folder="Other bookmarks/Sub")]
        text = formats.write_netscape(items, {"Bookmarks bar", "Other bookmarks", "Other bookmarks/Sub"})
        self.assertIn('PERSONAL_TOOLBAR_FOLDER="true">Bookmarks bar</H3>', text)
        self.assertIn('UNFILED_BOOKMARKS_FOLDER="true">Other bookmarks</H3>', text)
        self.assertEqual(sorted(b.folder for b in formats.parse_netscape(text)), ["Bookmarks bar", "Other bookmarks/Sub"])   # our own reader is unaffected


class GeckoDiscovery(unittest.TestCase):
    """Layouts as real browsers create them; names of profile folders are kept verbatim (spaces, parentheses, random ids)."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        self.env = {"XDG_CONFIG_HOME": "", "APPDATA": str(self.home / "AppData/Roaming"), "LOCALAPPDATA": str(self.home / "AppData/Local")}

    def found(self, name="firefox", system="Linux", **env) -> list:
        with mock.patch.object(Path, "home", return_value=self.home), mock.patch.object(gecko.platform, "system", return_value=system), \
                mock.patch.dict("os.environ", {**self.env, **env}):
            return [str(p.relative_to(self.home)).replace("\\", "/") for p in gecko.files(name)]

    def write(self, root: Path, profiles_ini: str = PROFILES_INI, installs_ini: str = INSTALLS_INI, dirs=("wtr793rw.default-release",)):
        root.mkdir(parents=True, exist_ok=True)
        if profiles_ini:
            (root / "profiles.ini").write_text(profiles_ini, encoding="utf-8")
        if installs_ini:
            (root / "installs.ini").write_text(installs_ini, encoding="utf-8")
        for d in dirs:
            fixture_db(root / d / "places.sqlite", marker=d.split(".")[0])

    def test_fresh_linux_install_uses_the_xdg_folder(self):
        self.write(self.home / ".config/mozilla/firefox")
        self.assertEqual(self.found(), [".config/mozilla/firefox/wtr793rw.default-release/places.sqlite"])

    def test_custom_xdg_config_home(self):
        self.write(self.home / "cfg/mozilla/firefox")
        self.assertEqual(self.found(XDG_CONFIG_HOME=str(self.home / "cfg")), ["cfg/mozilla/firefox/wtr793rw.default-release/places.sqlite"])
        self.assertEqual(self.found(XDG_CONFIG_HOME="relative/is/ignored"), [])

    def test_legacy_folder_is_preferred_and_snap_flatpak_are_found(self):
        self.write(self.home / ".mozilla/firefox")
        self.write(self.home / ".config/mozilla/firefox")
        self.write(self.home / "snap/firefox/common/.mozilla/firefox")
        self.write(self.home / ".var/app/org.mozilla.firefox/.mozilla/firefox")
        self.write(self.home / ".var/app/org.mozilla.firefox/config/mozilla/firefox")
        got = self.found()
        self.assertEqual(len(got), 5)
        self.assertTrue(got[0].startswith(".mozilla/firefox/"))

    def test_install_default_beats_default_1_and_alphabetical_order(self):
        """On a modern install `Default=1` marks the unused legacy profile; the [Install...] section is the truth."""
        ini = PROFILES_INI.replace("wtr793rw.default-release", "zzzz9999.default-release").replace("byprd30h.default", "aaaa0000.default")
        inst = INSTALLS_INI.replace("wtr793rw.default-release", "zzzz9999.default-release")
        self.write(self.home / ".mozilla/firefox", ini, inst, dirs=("zzzz9999.default-release", "aaaa0000.default"))
        got = self.found()
        self.assertEqual([g.split("/")[2] for g in got], ["zzzz9999.default-release", "aaaa0000.default"])
        with mock.patch.object(Path, "home", return_value=self.home), mock.patch.object(gecko.platform, "system", return_value="Linux"):
            self.assertIn("zzzz9999", [b.title for b in browsers.load("firefox")])

    def test_default_1_without_install_section_and_unlisted_profiles(self):
        ini = "[General]\nVersion=2\n\n[Profile0]\nName=work\nIsRelative=1\nPath=bbbb.work\n\n[Profile1]\nName=default\nIsRelative=1\nPath=cccc.default\nDefault=1\n"
        self.write(self.home / ".mozilla/firefox", ini, "", dirs=("bbbb.work", "cccc.default", "uOlTrTJv.Profile 1"))
        self.assertEqual([g.split("/")[2] for g in self.found()], ["cccc.default", "bbbb.work", "uOlTrTJv.Profile 1"])

    def test_absolute_profile_path_outside_the_root(self):
        elsewhere = self.home / "data" / "my firefox profile"
        fixture_db(elsewhere / "places.sqlite")
        ini = f"[Profile0]\nName=custom\nIsRelative=0\nPath={elsewhere}\nDefault=1\n"
        self.write(self.home / ".mozilla/firefox", ini, "", dirs=())
        self.assertEqual(self.found(), ["data/my firefox profile/places.sqlite"])

    def test_broken_ini_files_do_not_hide_profiles(self):
        self.write(self.home / ".mozilla/firefox", "\x00\xff garbage [[[ \n", "no sections here", dirs=("q1.default-release",))
        (self.home / ".mozilla/firefox/profiles.ini").write_bytes(b"\xff\xfe\x00bad utf-8 \x80\n[x")
        self.assertEqual(self.found(), [".mozilla/firefox/q1.default-release/places.sqlite"])

    def test_macos_and_windows_layouts(self):
        ini = PROFILES_INI.replace("Path=wtr793rw", "Path=Profiles/wtr793rw").replace("Default=wtr793rw", "Default=Profiles/wtr793rw")
        inst = INSTALLS_INI.replace("Default=wtr793rw", "Default=Profiles/wtr793rw")
        mac = self.home / "Library/Application Support/Firefox"
        self.write(mac, ini, inst, dirs=("Profiles/wtr793rw.default-release", "Profiles/byprd30h.default"))
        self.assertEqual(self.found(system="Darwin")[0], "Library/Application Support/Firefox/Profiles/wtr793rw.default-release/places.sqlite")
        win = self.home / "AppData/Roaming/Mozilla/Firefox"
        self.write(win, ini, inst, dirs=("Profiles/wtr793rw.default-release", "Profiles/byprd30h.default"))
        self.assertEqual(self.found(system="Windows")[0], "AppData/Roaming/Mozilla/Firefox/Profiles/wtr793rw.default-release/places.sqlite")

    def test_forks(self):
        zen = "[Profile0]\nName=Default (release)\nIsRelative=1\nPath=evl81bnf.Default (release)\n\n[InstallD3A8D3B14A701C42]\nDefault=evl81bnf.Default (release)\nLocked=1\n\n" \
              "[Profile1]\nName=Default Profile\nIsRelative=1\nPath=0e639wnc.Default Profile\nDefault=1\n"
        self.write(self.home / ".config/zen", zen, "", dirs=("evl81bnf.Default (release)", "0e639wnc.Default Profile"))
        self.assertEqual(self.found("zen")[0], ".config/zen/evl81bnf.Default (release)/places.sqlite")
        self.write(self.home / ".config/floorp", PROFILES_INI, INSTALLS_INI)
        self.assertEqual(self.found("floorp"), [".config/floorp/wtr793rw.default-release/places.sqlite"])
        self.assertEqual(self.found("librewolf"), [])
        self.assertEqual(registry.EXPORT_FORMATS, [f for f in formats.WRITE_FORMATS if f != "obsidian"])   # obsidian writes a directory
        self.assertEqual(registry.IMPORT_FORMATS, list(formats.READ_FORMATS))
        self.assertEqual(sorted(browsers.GECKO), ["firefox", "floorp", "librewolf", "waterfox", "zen"])
        self.assertEqual(tuple(registry.BROWSERS), tuple(browsers.ALL))      # MCP offers exactly the names the CLI knows

    def test_newest_backup_of_each_profile_follows_the_databases(self):
        root = self.home / ".mozilla/firefox"
        self.write(root)
        for name in ("bookmarks-2026-01-01_3_a.jsonlz4", "bookmarks-2026-03-09_3_b.jsonlz4"):
            (root / "wtr793rw.default-release/bookmarkbackups").mkdir(exist_ok=True)
            (root / "wtr793rw.default-release/bookmarkbackups" / name).write_bytes(b"x")
        self.assertEqual(self.found(), [".mozilla/firefox/wtr793rw.default-release/places.sqlite",
                                        ".mozilla/firefox/wtr793rw.default-release/bookmarkbackups/bookmarks-2026-03-09_3_b.jsonlz4"])


if __name__ == "__main__":
    unittest.main()
