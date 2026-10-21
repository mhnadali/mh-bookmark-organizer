"""Browser profile discovery and safe write-back, on synthetic machines only.

Every test builds a throw-away "home directory" and pretends to be Linux, macOS, Windows or WSL by patching
``platform.system`` / ``Path.home`` / the environment. Nothing here reads a real browser profile.
"""
import contextlib
import json
import os
import plistlib
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import common  # noqa: F401
from mhbm import browsers, formats, profilesafe, sync
from mhbm.model import Bookmark
from test_cli import CliCase

ENV_VARS = ("HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "XDG_CONFIG_HOME", "CHROME_CONFIG_HOME", "SUDO_USER",
            "WSL_DISTRO_NAME", "WSL_INTEROP")
BAR = {"type": "folder", "name": "Bookmarks bar", "children": [{"type": "url", "name": "A", "url": "https://a.example/", "date_added": "13200000000000000"}]}


def bookmarks_json(urls=("https://a.example/",), **extra) -> str:
    kids = [{"id": str(i + 10), "type": "url", "name": u, "url": u, "date_added": "13200000000000000"} for i, u in enumerate(urls)]
    doc = {"roots": {"bookmark_bar": {"id": "1", "type": "folder", "name": "Bookmarks bar", "children": kids},
                     "other": {"id": "2", "type": "folder", "name": "Other bookmarks", "children": []},
                     "synced": {"id": "3", "type": "folder", "name": "Mobile bookmarks", "children": []}}, "version": 1, **extra}
    doc["checksum"] = formats.chrome_checksum(doc["roots"])
    return json.dumps(doc)


class Machine:
    """A pretend computer: ``with Machine("Linux") as m:`` then write files under ``m.home``."""

    def __init__(self, system="Linux", **env):
        self.system = system
        self.env = {k: str(v) for k, v in env.items()}
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve() / "home"
        self.home.mkdir()

    def put(self, rel, text=None):
        p = self.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(bookmarks_json() if text is None else text, encoding="utf-8")
        return p

    def __enter__(self):
        self.stack = contextlib.ExitStack()
        self.stack.callback(self.tmp.cleanup)
        self.stack.enter_context(mock.patch.dict(os.environ, {}))
        for k in ENV_VARS:
            os.environ.pop(k, None)
        os.environ.update({"HOME": str(self.home), "USERPROFILE": str(self.home), **self.env})
        self.stack.enter_context(mock.patch.object(browsers.platform, "system", lambda: self.system))
        self.stack.enter_context(mock.patch.object(Path, "home", classmethod(lambda cls: self.home)))
        self.stack.enter_context(mock.patch.object(browsers, "_is_wsl", lambda: False))
        return self

    def __exit__(self, *exc):
        self.stack.close()

    def found(self, name):
        return [str(Path(p).relative_to(self.home).as_posix()) for p in browsers.find_profiles(name)]


class Discovery(unittest.TestCase):
    def test_linux_default_locations_and_xdg_override(self):
        with Machine() as m:
            m.put(".config/google-chrome/Default/Bookmarks")
            self.assertEqual(m.found("chrome"), [".config/google-chrome/Default/Bookmarks"])
        with Machine(XDG_CONFIG_HOME="{home}/xdg") as m:
            os.environ["XDG_CONFIG_HOME"] = str(m.home / "xdg")
            m.put("xdg/google-chrome/Default/Bookmarks")
            m.put(".config/google-chrome/Default/Bookmarks")                          # the XDG folder wins
            self.assertEqual(m.found("chrome"), ["xdg/google-chrome/Default/Bookmarks"])

    def test_chrome_config_home_is_for_chrome_only(self):
        with Machine() as m:
            os.environ["CHROME_CONFIG_HOME"] = str(m.home / "cc")
            m.put("cc/google-chrome/Default/Bookmarks")
            m.put(".config/BraveSoftware/Brave-Browser/Default/Bookmarks")               # Brave ignores CHROME_CONFIG_HOME
            self.assertEqual(m.found("chrome"), ["cc/google-chrome/Default/Bookmarks"])
            self.assertEqual(m.found("brave"), [".config/BraveSoftware/Brave-Browser/Default/Bookmarks"])

    def test_linux_channels_snap_and_flatpak(self):
        want = {
            "chrome": [".config/google-chrome-beta", ".config/google-chrome-unstable", ".var/app/com.google.Chrome/config/google-chrome"],
            "chromium": ["snap/chromium/common/chromium", ".var/app/org.chromium.Chromium/config/chromium"],
            "brave": [".config/BraveSoftware/Brave-Browser-Beta", ".config/BraveSoftware/Brave-Browser-Dev",
                      ".config/BraveSoftware/Brave-Browser-Nightly", "snap/brave/current/.config/BraveSoftware/Brave-Browser",
                      ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser"],
            "edge": [".config/microsoft-edge-beta", ".config/microsoft-edge-dev", ".var/app/com.microsoft.Edge/config/microsoft-edge"],
            "vivaldi": [".config/vivaldi-snapshot", ".var/app/com.vivaldi.Vivaldi/config/vivaldi"],
            "yandex": [".config/yandex-browser", ".config/yandex-browser-beta"],
        }
        for name, roots in want.items():
            with self.subTest(browser=name), Machine() as m:
                for r in roots:
                    m.put(f"{r}/Default/Bookmarks")
                self.assertEqual(sorted(m.found(name)), sorted(f"{r}/Default/Bookmarks" for r in roots))

    def test_opera_profile_root_is_the_profile(self):
        with Machine() as m:
            m.put(".config/opera/Bookmarks")
            m.put(".config/opera-beta/Bookmarks")
            self.assertEqual(m.found("opera"), [".config/opera/Bookmarks", ".config/opera-beta/Bookmarks"])

    def test_macos_layout(self):
        want = {"chrome": ["Google/Chrome", "Google/Chrome Beta", "Google/Chrome Dev", "Google/Chrome Canary"],
                "edge": ["Microsoft Edge", "Microsoft Edge Beta", "Microsoft Edge Canary"],
                "brave": ["BraveSoftware/Brave-Browser", "BraveSoftware/Brave-Browser-Nightly"],
                "opera-gx": ["com.operasoftware.OperaGX"], "yandex": ["Yandex/YandexBrowser"], "vivaldi": ["Vivaldi"]}
        for name, roots in want.items():
            with self.subTest(browser=name), Machine("Darwin") as m:
                for r in roots:
                    m.put(f"Library/Application Support/{r}/Default/Bookmarks" if name != "opera-gx" else f"Library/Application Support/{r}/Bookmarks")
                self.assertEqual(len(m.found(name)), len(roots))

    def test_windows_layout_env_redirects_and_fallbacks(self):
        with Machine("Windows") as m:                                                  # no LOCALAPPDATA / APPDATA: fall back to the home folder
            m.put("AppData/Local/Google/Chrome SxS/User Data/Default/Bookmarks")
            m.put("AppData/Local/Microsoft/Edge Beta/User Data/Default/Bookmarks")
            m.put("AppData/Roaming/Opera Software/Opera GX Stable/Bookmarks")
            m.put("AppData/Roaming/Opera Software/Opera Stable/Bookmarks")
            self.assertEqual(len(m.found("chrome")), 1)
            self.assertEqual(len(m.found("edge")), 1)
            self.assertEqual(m.found("opera"), ["AppData/Roaming/Opera Software/Opera Stable/Bookmarks"])
            self.assertEqual(m.found("opera-gx"), ["AppData/Roaming/Opera Software/Opera GX Stable/Bookmarks"])
        with Machine("Windows") as m:                                                  # redirected folders (roaming profiles, OneDrive known-folder move)
            os.environ["LOCALAPPDATA"] = str(m.home / "D" / "Local")
            m.put("D/Local/Google/Chrome/User Data/Profile 3/Bookmarks")
            m.put("AppData/Local/Google/Chrome/User Data/Default/Bookmarks")           # the unredirected default is not consulted
            self.assertEqual(m.found("chrome"), ["D/Local/Google/Chrome/User Data/Profile 3/Bookmarks"])

    def test_wsl_sees_the_windows_side(self):
        with Machine() as m:
            mnt = m.home / "mnt"
            m.put("mnt/c/Users/alice/AppData/Local/Google/Chrome/User Data/Default/Bookmarks")
            m.put("mnt/c/Users/Public/AppData/Local/Google/Chrome/User Data/Default/Bookmarks")       # not a person
            m.put("mnt/c/Users/bob/Documents/x.txt", "x")                                              # no AppData: not a profile folder
            with mock.patch.object(browsers, "WSL_MOUNT_ROOT", mnt), mock.patch.object(browsers, "_is_wsl", lambda: True):
                self.assertEqual(m.found("chrome"), ["mnt/c/Users/alice/AppData/Local/Google/Chrome/User Data/Default/Bookmarks"])
            self.assertEqual(m.found("chrome"), [])                                                    # outside WSL it is not looked at

    def test_sudo_user_home_is_searched_too(self):
        with Machine() as m:
            other = m.home.parent / "alice"
            (other / ".config/google-chrome/Default").mkdir(parents=True)
            (other / ".config/google-chrome/Default/Bookmarks").write_text(bookmarks_json())
            with mock.patch.object(browsers, "_sudo_home", lambda: other):
                self.assertEqual(len(browsers.find_profiles("chrome")), 1)

    def test_default_profile_first_natural_order_and_guest_system_skipped(self):
        with Machine() as m:
            for d in ("Guest Profile", "Profile 10", "Profile 2", "Profile 1", "System Profile"):
                m.put(f".config/google-chrome/{d}/Bookmarks")
            self.assertEqual([Path(p).parent.name for p in browsers.find_profiles("chrome")], ["Profile 1", "Profile 2", "Profile 10"])
            m.put(".config/google-chrome/Local State", json.dumps({"profile": {"last_used": "Profile 2", "info_cache": {"Profile 10": {"is_ephemeral": True}}}}))
            ordered = [(Path(p).parent.name) for p in browsers.find_profiles("chrome")]
            self.assertEqual(ordered, ["Profile 2", "Profile 1"])                                     # last used first; the ephemeral one is gone
            m.put(".config/google-chrome/Default/Bookmarks")
            os.remove(m.home / ".config/google-chrome/Local State")
            self.assertEqual(Path(browsers.find_profiles("chrome")[0]).parent.name, "Default")

    def test_local_state_names_signed_in_flag_and_no_email_leaks(self):
        state = {"profile": {"last_used": "Profile 1", "info_cache": {
            "Default": {"name": "Work", "user_name": "alice@example.com", "gaia_id": "123"},
            "Profile 1": {"name": "alice@example.com", "gaia_name": "Alice"},
            "Profile 2": {"name": "Personal"}}}}
        with Machine() as m:
            m.put(".config/google-chrome/Local State", json.dumps(state))
            for d in ("Default", "Profile 1", "Profile 2"):
                m.put(f".config/google-chrome/{d}/Bookmarks")
            by_dir = {p.dir.name: p for p in browsers.discover("chrome")}
            self.assertEqual((by_dir["Default"].name, by_dir["Default"].signed_in), ("Work", True))
            self.assertEqual((by_dir["Profile 2"].name, by_dir["Profile 2"].signed_in), ("Personal", False))
            self.assertTrue(by_dir["Profile 1"].default)
            self.assertNotIn("@", json.dumps(browsers.describe()))                                    # an e-mail used as a profile name is scrubbed
            self.assertNotIn("alice@example.com", repr(browsers.discover("chrome")))

    def test_scrub(self):
        self.assertEqual(browsers.scrub("Work (alice@example.com)"), "Work ((account))")
        self.assertEqual(browsers.scrub("  Person 1 "), "Person 1")
        self.assertEqual(browsers.scrub("ali@x.org and bo@y.io"), "(account) and (account)")
        self.assertEqual(browsers.scrub(""), "")

    def test_account_bookmarks_alone_are_listed_and_merged_on_load(self):
        with Machine() as m:
            m.put(".config/google-chrome/Default/AccountBookmarks", bookmarks_json(("https://account.example/",)))
            self.assertEqual(m.found("chrome"), [".config/google-chrome/Default/AccountBookmarks"])
            self.assertEqual([b.url for b in browsers.load("chrome")], ["https://account.example/"])
            m.put(".config/google-chrome/Default/Bookmarks", bookmarks_json(("https://local.example/", "https://both.example/")))
            m.put(".config/google-chrome/Default/AccountBookmarks", bookmarks_json(("https://account.example/", "https://both.example/")))
            got = browsers.load("chrome")
            self.assertEqual(sorted(b.url for b in got), ["https://account.example/", "https://both.example/", "https://local.example/"])
            self.assertEqual(m.found("chrome"), [".config/google-chrome/Default/Bookmarks"])         # one entry per profile

    def test_encrypted_only_profile_says_why_it_cannot_be_read(self):
        with Machine() as m:
            m.put(".config/google-chrome/Default/EncryptedBookmarks2", "v10binary")
            self.assertEqual(m.found("chrome"), [])
            (prof,) = browsers.discover("chrome")
            self.assertIn("encrypted", prof.notes[0])
            with self.assertRaises(formats.FormatError) as cm:
                browsers.load("chrome")
            self.assertIn("EncryptedBookmarks2", str(cm.exception))

    def test_bookmarks_bak_is_a_fallback_only(self):
        with Machine() as m:
            m.put(".config/google-chrome/Default/Bookmarks.bak", bookmarks_json(("https://old.example/",)))
            self.assertEqual(m.found("chrome"), [".config/google-chrome/Default/Bookmarks.bak"])
            m.put(".config/google-chrome/Default/Bookmarks", bookmarks_json(("https://new.example/",)))
            self.assertEqual([b.url for b in browsers.load("chrome")], ["https://new.example/"])

    @unittest.skipUnless(hasattr(os, "symlink") and os.name != "nt", "needs symlinks")
    def test_symlinked_user_data_dir_is_listed_once(self):
        with Machine() as m:
            real = m.home / "data/chrome"
            m.put("data/chrome/Default/Bookmarks")
            (m.home / ".config").mkdir()
            (m.home / ".config/google-chrome").symlink_to(real)
            (m.home / ".config/google-chrome-beta").symlink_to(real)
            self.assertEqual(len(browsers.find_profiles("chrome")), 1)

    def test_unicode_profile_and_folder_names(self):
        with Machine() as m:
            m.put(".config/google-chrome/Profile ۱ محمد/Bookmarks", bookmarks_json(("https://fa.example/",)))
            self.assertEqual([b.url for b in browsers.load("chrome")], ["https://fa.example/"])
            self.assertEqual([b.url for b in browsers.load("chrome", "Profile ۱ محمد")], ["https://fa.example/"])

    def test_safari_without_full_disk_access_is_reported_not_raised(self):
        with Machine("Darwin") as m:
            (m.home / "Library/Safari").mkdir(parents=True)
            real_stat = os.stat

            def deny(path, *a, **k):
                if str(path).endswith("Bookmarks.plist"):
                    raise PermissionError(1, "Operation not permitted")
                return real_stat(path, *a, **k)
            with mock.patch.object(os, "stat", deny):
                self.assertEqual(browsers.detect(), {})                                               # no traceback
                (prof,) = browsers.discover("safari")
                self.assertIn("Full Disk Access", prof.notes[0])
                with self.assertRaises(formats.FormatError) as cm:
                    browsers.load("safari")
                self.assertIn("Full Disk Access", str(cm.exception))

    def test_firefox_family_roots(self):
        cases = [("Linux", "firefox", ".mozilla/firefox"), ("Linux", "firefox", ".var/app/org.mozilla.firefox/.mozilla/firefox"),
                 ("Linux", "librewolf", ".librewolf"), ("Linux", "zen", ".zen"), ("Linux", "waterfox", ".waterfox"), ("Linux", "floorp", ".floorp"),
                 ("Darwin", "firefox", "Library/Application Support/Firefox/Profiles"), ("Darwin", "zen", "Library/Application Support/zen/Profiles"),
                 ("Windows", "firefox", "AppData/Roaming/Mozilla/Firefox/Profiles"), ("Windows", "librewolf", "AppData/Roaming/LibreWolf/Profiles")]
        for system, name, root in cases:
            with self.subTest(system=system, browser=name, root=root), Machine(system) as m:
                db = m.home / root / "abcd.default-release/places.sqlite"
                db.parent.mkdir(parents=True)
                db.write_bytes(b"")
                self.assertEqual(len(browsers.find_profiles(name)), 1)

    def test_unsupported_browser_gets_an_explanation(self):
        with self.assertRaises(formats.FormatError) as cm:
            browsers.load("arc")
        self.assertIn("StorableSidebar", str(cm.exception))

    def test_detect_keeps_its_shape(self):
        with Machine() as m:
            m.put(".config/google-chrome/Default/Bookmarks")
            found = browsers.detect()
            self.assertEqual(list(found), ["chrome"])
            self.assertTrue(all(isinstance(p, str) for p in found["chrome"]))
            self.assertEqual(browsers.CHROMIUM[:6], ("chrome", "chromium", "brave", "edge", "vivaldi", "opera"))
            self.assertIn("firefox", browsers.ALL)
            self.assertIn("safari", browsers.ALL)


def places_db(path: Path, urls=("https://a.example/",), extra_sql="") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.executescript("CREATE TABLE moz_places(id INTEGER PRIMARY KEY, url TEXT);"
                    "CREATE TABLE moz_bookmarks(id INTEGER PRIMARY KEY, type INT, fk INT, parent INT, title TEXT, dateAdded INT, guid TEXT);"
                    "INSERT INTO moz_bookmarks VALUES (1,2,NULL,0,'','0','root________'),(2,2,NULL,1,'unfiled','0','unfiled_____');" + extra_sql)
    for i, u in enumerate(urls):
        c.execute("INSERT INTO moz_places VALUES (?,?)", (i + 1, u))
        c.execute("INSERT INTO moz_bookmarks VALUES (?,1,?,2,'t',1600000000000000,?)", (i + 10, i + 1, f"g{i}"))
    c.commit()
    c.close()
    return path


class FirefoxProfiles(unittest.TestCase):
    INI = ("[Profile2]\nName=default\nIsRelative=1\nPath=wxyz.default\n\n"
           "[Profile1]\nName=default-release\nIsRelative=1\nPath=abcd1234.default-release\nDefault=1\n\n"
           "[Profile0]\nName=dev-edition-default\nIsRelative=1\nPath=0zzz.dev-edition-default\n\n"
           "[Install4F96D1932A9F858E]\nDefault=abcd1234.default-release\nLocked=1\n\n[General]\nStartWithLastProfile=1\nVersion=2\n")

    def three_profiles(self, m, ini=None):
        for p in ("0zzz.dev-edition-default", "abcd1234.default-release", "wxyz.default"):
            places_db(m.home / ".mozilla/firefox" / p / "places.sqlite", (f"https://{p.split('.')[0]}.example/",))
        m.put(".mozilla/firefox/profiles.ini", self.INI if ini is None else ini)

    def test_profiles_ini_decides_the_default_not_the_alphabet(self):
        with Machine() as m:
            self.three_profiles(m)
            profiles = browsers.discover("firefox")
            self.assertEqual([p.dir.name for p in profiles][0], "abcd1234.default-release")
            self.assertEqual([p.name for p in profiles][0], "default-release")
            self.assertTrue(profiles[0].default)
            self.assertEqual([b.url for b in browsers.load("firefox")], ["https://abcd1234.example/"])

    def test_install_section_beats_profile_default_flag(self):
        ini = ("[Profile0]\nName=a\nIsRelative=1\nPath=0zzz.dev-edition-default\nDefault=1\n\n"
               "[Profile1]\nName=b\nIsRelative=1\nPath=abcd1234.default-release\n\n[Install1]\nDefault=abcd1234.default-release\n")
        with Machine() as m:
            self.three_profiles(m, ini)
            self.assertEqual(browsers.discover("firefox")[0].dir.name, "0zzz.dev-edition-default")   # both flagged; file order breaks the tie
            self.assertTrue({p.dir.name for p in browsers.discover("firefox") if p.default} >= {"abcd1234.default-release"})

    def test_profile_outside_the_firefox_folder_and_orphans(self):
        with Machine() as m:
            ext = m.home / "elsewhere/mine"
            places_db(ext / "places.sqlite", ("https://ext.example/",))
            places_db(m.home / ".mozilla/firefox/orphan.old/places.sqlite", ("https://orphan.example/",))
            m.put(".mozilla/firefox/profiles.ini", f"\ufeff[Profile0]\nName=ext\nIsRelative=0\nPath={ext}\nDefault=1\n")
            urls = [browsers.load("firefox", str(p.dir)).__iter__().__next__().url for p in browsers.discover("firefox")]
            self.assertEqual(urls, ["https://ext.example/", "https://orphan.example/"])

    @unittest.skipIf(os.name == "nt", "a Windows path is only unreachable from a POSIX system")
    def test_windows_paths_in_an_ini_read_from_posix_are_ignored(self):
        with Machine() as m:
            m.put(".mozilla/firefox/profiles.ini", "[Profile0]\nName=x\nIsRelative=0\nPath=C:\\Users\\a\\x\nDefault=1\n")
            self.assertEqual(browsers.discover("firefox"), [])

    def test_backslash_relative_paths_and_broken_ini(self):
        with Machine("Windows") as m:
            places_db(m.home / "AppData/Roaming/Mozilla/Firefox/Profiles/xy.default/places.sqlite")
            m.put("AppData/Roaming/Mozilla/Firefox/profiles.ini", "[Profile0]\nName=d\nIsRelative=1\nPath=Profiles\\xy.default\nDefault=1\n")
            (prof,) = browsers.discover("firefox")
            self.assertEqual((prof.dir.name, prof.name), ("xy.default", "d"))
            m.put("AppData/Roaming/Mozilla/Firefox/profiles.ini", "this is not an ini file\n\x00\x01")
            self.assertEqual(len(browsers.discover("firefox")), 1)                                  # falls back to what is on disk

    def test_newest_automatic_backup_is_a_fallback(self):
        with Machine() as m:
            d = m.home / ".mozilla/firefox/abc.default"
            (d / "bookmarkbackups").mkdir(parents=True)
            for n in ("bookmarks-2026-01-01_5_x.jsonlz4", "bookmarks-2026-02-01_7_x.jsonlz4"):
                (d / "bookmarkbackups" / n).write_bytes(b"x")
            (prof,) = browsers.discover("firefox")
            self.assertEqual(prof.fallbacks[0].name, "bookmarks-2026-02-01_7_x.jsonlz4")
            self.assertEqual(prof.files, ())


class Selecting(CliCase):
    def machine_with_profiles(self, m):
        m.put(".config/google-chrome/Local State", json.dumps({"profile": {"info_cache": {"Profile 1": {"name": "Work"}}}}))
        m.put(".config/google-chrome/Default/Bookmarks", bookmarks_json(("https://default.example/",)))
        m.put(".config/google-chrome/Profile 1/Bookmarks", bookmarks_json(("https://work.example/",)))

    def test_profile_option_takes_a_directory_name_a_display_name_a_folder_or_a_file(self):
        with Machine() as m:
            self.machine_with_profiles(m)
            for selector in ("Profile 1", "profile 1", "Work", "work", str(m.home / ".config/google-chrome/Profile 1"),
                             str(m.home / ".config/google-chrome/Profile 1/Bookmarks")):
                with self.subTest(selector=selector):
                    self.assertEqual([b.url for b in browsers.load("chrome", selector)], ["https://work.example/"])
            self.assertEqual([b.url for b in browsers.load("chrome", str(m.home / ".config/google-chrome"))], ["https://default.example/"])
            with self.assertRaises(formats.FormatError) as cm:
                browsers.load("chrome", "No Such Profile")
            self.assertIn("browsers", str(cm.exception))

    def test_import_browser_names_the_profile_and_counts_the_others(self):
        with Machine() as m:
            self.machine_with_profiles(m)
            r = self.js("import-browser", "chrome")
            self.assertEqual((r["imported"], r["other_profiles"]), (1, 1))
            self.assertTrue(r["sources"][0].endswith("Bookmarks"))
            r = self.js("import-browser", "chrome", "--profile", "Work")
            self.assertEqual((r["imported"], r["other_profiles"]), (1, 0))
            r = self.js("import-browser", "chrome", "--all-profiles", "--allow-duplicates")
            self.assertEqual(sorted(r["profiles"]), ["Default", "Profile 1"])
            self.assertEqual(self.lib().count(), 3)

    def test_browsers_command_lists_names_and_flags_but_no_email(self):
        with Machine() as m:
            m.put(".config/google-chrome/Local State", json.dumps({"profile": {"info_cache": {
                "Default": {"name": "Work", "user_name": "alice@example.com", "gaia_id": "1"}}}}))
            m.put(".config/google-chrome/Default/Bookmarks")
            m.put(".config/google-chrome/Profile 2/EncryptedBookmarks2", "binary")
            text = self.run_cli("browsers")
            self.assertIn('"Work"', text)
            self.assertIn("signed in", text)
            self.assertIn("encrypted", text)
            self.assertNotIn("@", text)
            data = self.js("browsers")
            self.assertEqual(data["browsers"]["chrome"], [str(m.home / ".config/google-chrome/Default/Bookmarks")])
            self.assertNotIn("@", json.dumps(data))
            self.assertEqual({p["profile"] for p in data["profiles"]}, {"Default", "Profile 2"})


class Reading(unittest.TestCase):
    def test_places_database_in_a_folder_whose_name_is_not_uri_safe(self):
        with tempfile.TemporaryDirectory() as d:
            for odd in ("a b", "a#b", "a?b", "a%41b", "persian-محمد"):
                with self.subTest(folder=odd):
                    db = places_db(Path(d, odd, "places.sqlite"))
                    self.assertEqual([b.url for b in browsers.parse_places(db)], ["https://a.example/"])

    def test_live_wal_database_includes_uncheckpointed_rows(self):
        if os.name == "nt":
            self.skipTest("Windows locks a database a writer holds open")
        with tempfile.TemporaryDirectory() as d:
            db = places_db(Path(d, "places.sqlite"))
            w = sqlite3.connect(db, isolation_level=None)
            self.addCleanup(w.close)
            w.execute("PRAGMA journal_mode=WAL")
            w.execute("PRAGMA wal_autocheckpoint=0")
            w.execute("INSERT INTO moz_places VALUES (500,'https://hot.example/')")
            w.execute("INSERT INTO moz_bookmarks VALUES (500,1,500,2,'hot',1600000000000000,'ghot')")
            self.assertTrue(Path(str(db) + "-wal").exists())
            self.assertEqual({b.url for b in browsers.load("firefox", profile=str(db))}, {"https://a.example/", "https://hot.example/"})

    def test_torn_copy_is_retried_but_a_wrong_file_is_not(self):
        with tempfile.TemporaryDirectory() as d:
            db = places_db(Path(d, "places.sqlite"))
            real, calls = browsers.parse_places, []

            def flaky(p):
                calls.append(p)
                if len(calls) < 3:
                    raise sqlite3.DatabaseError("database disk image is malformed")
                return real(p)
            with mock.patch.object(browsers, "parse_places", flaky), mock.patch.object(time, "sleep", lambda s: None):
                self.assertEqual(len(browsers.load("firefox", profile=str(db))), 1)
                self.assertEqual(len(calls), 3)
            calls.clear()
            with mock.patch.object(browsers, "parse_places", side_effect=sqlite3.DatabaseError("database disk image is malformed")) as boom, \
                    mock.patch.object(time, "sleep", lambda s: None):
                with self.assertRaises(formats.FormatError) as cm:
                    browsers.load("firefox", profile=str(db))
                self.assertEqual(boom.call_count, 3)
                self.assertIn("close the browser", str(cm.exception))
            junk = Path(d, "junk", "places.sqlite")
            junk.parent.mkdir()
            junk.write_bytes(b"not a database at all" * 50)
            with self.assertRaises(formats.FormatError):
                browsers.load("firefox", profile=str(junk))

    def test_unreadable_file_becomes_a_format_error_with_a_hint(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "Bookmarks")
            p.write_text(bookmarks_json())
            with mock.patch("shutil.copy2", side_effect=PermissionError(13, "Permission denied")):
                with self.assertRaises(formats.FormatError) as cm:
                    browsers.load("x", profile=str(p))
                self.assertIn("close the browser", str(cm.exception))
            lib_safari = Path(d, "Library/Safari/Bookmarks.plist")
            lib_safari.parent.mkdir(parents=True)
            lib_safari.write_bytes(plistlib.dumps({}))
            with mock.patch("shutil.copy2", side_effect=PermissionError(1, "Operation not permitted")):
                with self.assertRaises(formats.FormatError) as cm:
                    browsers.load("safari", profile=str(lib_safari))
                self.assertIn("Full Disk Access", str(cm.exception))

    def test_garbage_in_a_bookmarks_file_is_a_format_error(self):
        with tempfile.TemporaryDirectory() as d:
            for name, blob in (("Bookmarks", b"\xff\xfe binary \x00"), ("Bookmarks.plist", b"nope"), ("empty", b"")):
                with self.subTest(file=name):
                    p = Path(d, name)
                    p.write_bytes(blob)
                    with self.assertRaises(formats.FormatError):
                        browsers.load("x", profile=str(p))

    def test_safari_reading_list_dates_are_utc_in_every_time_zone(self):
        leaf = {"WebBookmarkType": "WebBookmarkTypeLeaf", "URLString": "https://rl.example/", "URIDictionary": {"title": "RL"},
                "ReadingList": {"DateAdded": datetime(2024, 1, 1, 12, 0, 0)}}                         # plistlib: naive, UTC
        data = plistlib.loads(plistlib.dumps({"WebBookmarkType": "WebBookmarkTypeList", "Title": "", "Children": [leaf]}))
        want = int(datetime(2024, 1, 1, 12, tzinfo=timezone.utc).timestamp())
        zones = ["UTC", "America/Los_Angeles", "Asia/Tehran"] if hasattr(time, "tzset") else [None]
        old = os.environ.get("TZ")
        try:
            for tz in zones:
                if tz:
                    os.environ["TZ"] = tz
                    time.tzset()
                self.assertEqual(browsers.parse_safari(data)[0].added, want, tz)
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            if hasattr(time, "tzset"):
                time.tzset()
        aware = {"WebBookmarkType": "WebBookmarkTypeLeaf", "URLString": "https://x/", "ReadingList": {"DateAdded": datetime(2024, 1, 1, 12, tzinfo=timezone.utc)}}
        self.assertEqual(browsers.parse_safari(aware)[0].added, want)


def fake_pid(alive: bool):
    return mock.patch.object(profilesafe, "_pid_alive", lambda pid: alive)


class Locks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "Default").mkdir()
        (self.root / "Local State").write_text("{}")
        self.target = self.root / "Default" / "Bookmarks"
        self.target.write_text(bookmarks_json())

    def link(self, value, where=None):
        try:
            os.symlink(value, (where or self.root) / "SingletonLock")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available")

    def test_free_when_there_is_no_lock(self):
        self.assertEqual(profilesafe.chromium_lock(self.target).state, "free")

    def test_singleton_lock_of_a_live_process_blocks(self):
        import socket
        self.link(f"{socket.gethostname()}-4242")
        with fake_pid(True):
            state = profilesafe.chromium_lock(self.target)
        self.assertEqual(state.state, "running")
        self.assertTrue(state.blocks)
        self.assertIn("4242", state.detail)

    def test_singleton_lock_left_by_a_crash_is_stale_not_blocking(self):
        import socket
        self.link(f"{socket.gethostname()}-4242")
        with fake_pid(False):
            state = profilesafe.chromium_lock(self.target)
        self.assertEqual((state.state, state.blocks), ("stale", False))

    def test_lock_of_another_computer_blocks_whatever_the_pid(self):
        self.link("some-other-host-4242")
        with fake_pid(False):
            self.assertEqual(profilesafe.chromium_lock(self.target).state, "running")

    def test_hostnames_with_dashes_are_parsed_from_the_right(self):
        import socket
        self.link(f"{socket.gethostname()}-7")
        with fake_pid(True):
            self.assertIn("process 7", profilesafe.chromium_lock(self.target).detail)

    def test_sandboxed_profiles_cannot_be_checked_so_the_lock_counts(self):
        import socket
        sandbox = self.root / ".var" / "app" / "com.google.Chrome" / "config" / "google-chrome"
        (sandbox / "Default").mkdir(parents=True)
        (sandbox / "Local State").write_text("{}")
        target = sandbox / "Default" / "Bookmarks"
        target.write_text(bookmarks_json())
        self.link(f"{socket.gethostname()}-99999", sandbox)
        with fake_pid(False):
            self.assertEqual(profilesafe.chromium_lock(target).state, "running")

    def test_a_lock_that_is_not_a_symlink_or_is_malformed_blocks(self):
        (self.root / "SingletonLock").write_text("x")
        self.assertEqual(profilesafe.chromium_lock(self.target).state, "unknown")
        (self.root / "SingletonLock").unlink()
        self.link("garbage")
        self.assertEqual(profilesafe.chromium_lock(self.target).state, "unknown")

    def test_windows_lockfile_held_open_means_running(self):
        (self.root / "lockfile").write_text("")
        real_open = open

        def held(path, *a, **k):
            if str(path).endswith("lockfile"):
                raise PermissionError(13, "The process cannot access the file because it is being used by another process")
            return real_open(path, *a, **k)
        with mock.patch("builtins.open", held):
            self.assertEqual(profilesafe.chromium_lock(self.target).state, "running")
        self.assertEqual(profilesafe.chromium_lock(self.target).state, "stale")                     # present but not held
        (self.root / "lockfile").unlink()
        self.assertEqual(profilesafe.chromium_lock(self.target).state, "free")

    def test_unrelated_parent_folders_are_never_probed(self):
        stray = Path(self.tmp.name, "loose")
        (stray / "x").mkdir(parents=True)
        (stray / "lockfile").write_text("")                  # two levels above the target, but no `Local State` there: not a browser folder
        t = stray / "x" / "Bookmarks"
        t.write_text(bookmarks_json())
        real_open = open

        def held(path, *a, **k):
            if str(path).endswith("lockfile"):
                raise PermissionError(13, "busy")
            return real_open(path, *a, **k)
        with mock.patch("builtins.open", held):
            self.assertEqual(profilesafe.chromium_lock(t).state, "free")
            (stray / "Local State").write_text("{}")
            self.assertEqual(profilesafe.chromium_lock(t).state, "running")

    def test_opera_keeps_the_lock_beside_its_bookmarks(self):
        import socket
        opera = self.root / "opera"
        opera.mkdir()
        t = opera / "Bookmarks"
        t.write_text(bookmarks_json())
        self.link(f"{socket.gethostname()}-5", opera)
        with fake_pid(True):
            self.assertEqual(profilesafe.chromium_lock(t).state, "running")

    def test_pid_probe_never_signals_on_windows(self):
        with mock.patch.object(profilesafe.os, "name", "nt"), mock.patch.object(profilesafe.os, "kill") as kill:
            self.assertTrue(profilesafe._pid_alive(1234))
        kill.assert_not_called()


class Preflight(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name).resolve()
        self.target = self.dir / "Bookmarks"
        self.target.write_text(bookmarks_json())

    def test_clean_profile_has_no_errors(self):
        pf = profilesafe.preflight(self.target)
        self.assertEqual((pf.errors, pf.warnings, pf.ok), ([], [], True))

    def test_sync_metadata_blocks_unless_allowed(self):
        self.target.write_text(bookmarks_json(sync_metadata="AAAA"))
        pf = profilesafe.preflight(self.target)
        self.assertTrue(pf.synced)
        self.assertIn("Sync", pf.errors[0])
        self.assertTrue(profilesafe.preflight(self.target, allow_synced=True).ok)
        self.assertTrue(profilesafe.preflight(self.target, allow_synced=True).warnings)

    def test_running_browser_blocks_unless_ignored(self):
        with mock.patch.object(profilesafe, "chromium_lock", lambda t: profilesafe.LockState("running", "process 1 holds the lock")):
            pf = profilesafe.preflight(self.target)
            self.assertFalse(pf.ok)
            self.assertIn("Close the browser", pf.errors[0])
            self.assertTrue(profilesafe.preflight(self.target, ignore_lock=True).ok)

    def test_stale_lock_is_only_a_warning(self):
        with mock.patch.object(profilesafe, "chromium_lock", lambda t: profilesafe.LockState("stale", "left by a crash")):
            pf = profilesafe.preflight(self.target)
            self.assertEqual((pf.ok, pf.warnings), (True, ["left by a crash"]))

    def test_read_only_file_system_and_missing_permission(self):
        with mock.patch.object(profilesafe, "_read_only_mount", lambda p: True):
            self.assertIn("read-only", profilesafe.preflight(self.target).errors[0])
        with mock.patch.object(profilesafe.os, "access", lambda p, mode: False):
            self.assertIn("no permission", profilesafe.preflight(self.target).errors[0])

    def test_neighbouring_files_and_unusual_content_are_reported(self):
        (self.dir / "AccountBookmarks").write_text("{}")
        (self.dir / "EncryptedBookmarks2").write_text("x")
        self.target.write_text(bookmarks_json())
        data = json.loads(self.target.read_text())
        data["roots"]["trash"] = {"id": "9", "type": "folder", "name": "Trash", "children": []}
        data["roots"]["bookmark_bar"]["children"][0]["meta_info"] = {"Nickname": "n"}
        self.target.write_text(json.dumps(data))
        pf = profilesafe.preflight(self.target, rewrites_everything=True)
        text = "\n".join(pf.warnings)
        for needle in ("AccountBookmarks", "EncryptedBookmarks2", "trash", "meta_info", "regenerated"):
            self.assertIn(needle, text)
        self.assertTrue(pf.account_file)
        self.assertEqual([p.name for p in pf.twins], ["EncryptedBookmarks2"])

    def test_invalid_json_is_replaceable(self):
        self.target.write_text("{not json")
        pf = profilesafe.preflight(self.target)
        self.assertTrue(pf.ok)
        self.assertIn("not valid JSON", pf.warnings[0])


class AtomicWrites(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name).resolve()
        self.target = self.dir / "Bookmarks"
        self.target.write_text("ORIGINAL", encoding="utf-8")

    def test_backups_never_overwrite_each_other(self):
        with mock.patch.object(time, "strftime", lambda fmt, *a: "20260101-000000"):
            first = profilesafe.backup_file(self.target)
            self.target.write_text("SECOND")
            second = profilesafe.backup_file(self.target)
            self.target.write_text("THIRD")
            third = profilesafe.backup_file(self.target)
        self.assertEqual(len({first, second, third}), 3)
        self.assertEqual([p.read_text() for p in (first, second, third)], ["ORIGINAL", "SECOND", "THIRD"])
        self.assertEqual(profilesafe.list_backups(self.target), [first, second, third])

    def test_backup_ordering_survives_double_digit_counters(self):
        names = ["20260101-000000", "20260101-000000-2", "20260101-000000-10", "20260102-000000"]
        made = []
        for n in names:
            p = self.dir / f"Bookmarks{profilesafe.BACKUP_TAG}{n}"
            p.write_text(n)
            made.append(p)
        (self.dir / "Bookmarks.mhbm-backup-notastamp").write_text("x")
        self.assertEqual(profilesafe.list_backups(self.target), made)

    def test_failure_leaves_the_original_and_no_temp_files(self):
        with mock.patch.object(os, "replace", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                profilesafe.atomic_write_text(self.target, "NEW" * 1000)
        self.assertEqual(self.target.read_text(), "ORIGINAL")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["Bookmarks"])

    def test_a_locked_target_on_windows_is_explained(self):
        with mock.patch.object(os, "replace", side_effect=PermissionError(13, "Access is denied")):
            with self.assertRaises(profilesafe.ProfileError) as cm:
                profilesafe.atomic_write_text(self.target, "NEW")
        self.assertIn("holds it open", str(cm.exception))
        self.assertEqual(self.target.read_text(), "ORIGINAL")

    def test_data_is_flushed_to_disk_before_the_rename(self):
        order = []
        real_fsync, real_replace = os.fsync, os.replace
        with mock.patch.object(os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1]), \
                mock.patch.object(os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))[1]):
            profilesafe.atomic_write_text(self.target, "NEW")
        self.assertEqual(self.target.read_text(), "NEW")
        self.assertEqual(order[:2], ["fsync", "replace"])
        if os.name != "nt":
            self.assertEqual(order[-1], "fsync")                                                      # and the directory entry afterwards

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_mode_is_kept(self):
        os.chmod(self.target, 0o640)
        profilesafe.atomic_write_text(self.target, "NEW")
        self.assertEqual(os.stat(self.target).st_mode & 0o777, 0o640)
        fresh = self.dir / "fresh"
        profilesafe.atomic_write_text(fresh, "x")
        self.assertEqual(os.stat(fresh).st_mode & 0o777, 0o600)

    @unittest.skipUnless(hasattr(os, "chown"), "POSIX ownership")
    def test_owner_of_the_replaced_file_is_restored_when_run_as_someone_else(self):
        st = os.stat(self.target)
        with mock.patch.object(os, "chown") as chown:
            profilesafe.atomic_write_text(self.target, "NEW")
            backup = profilesafe.backup_file(self.target)
        self.assertIn(mock.call(mock.ANY, st.st_uid, st.st_gid), chown.call_args_list)
        self.assertEqual(chown.call_count, 2)                                                         # the new file and its backup
        self.assertTrue(backup.exists())

    @unittest.skipUnless(hasattr(os, "symlink") and os.name != "nt", "needs symlinks")
    def test_writes_through_a_symlink_and_keeps_it(self):
        real = self.dir / "dotfiles"
        real.mkdir()
        (real / "Bookmarks").write_text("ORIGINAL")
        link = self.dir / "profile-Bookmarks"
        link.symlink_to(real / "Bookmarks")
        profilesafe.atomic_write_text(link, "NEW")
        self.assertTrue(link.is_symlink())
        self.assertEqual((real / "Bookmarks").read_text(), "NEW")

    def test_win_extended_paths(self):
        long_dir = "C:\\Users\\" + "x" * 250
        self.assertEqual(profilesafe.win_extended("C:\\short\\Bookmarks"), "C:\\short\\Bookmarks")
        self.assertEqual(profilesafe.win_extended(long_dir + "\\Bookmarks"), "\\\\?\\" + long_dir + "\\Bookmarks")
        self.assertEqual(profilesafe.win_extended("\\\\server\\share\\" + "y" * 250), "\\\\?\\UNC\\server\\share\\" + "y" * 250)
        already = "\\\\?\\C:\\" + "z" * 250
        self.assertEqual(profilesafe.win_extended(already), already)
        self.assertEqual(profilesafe.win_extended("relative/" + "r" * 300), "relative/" + "r" * 300)

    def test_fingerprint_notices_changes(self):
        fp = profilesafe.fingerprint(self.target)
        profilesafe.assert_unchanged(self.target, fp)
        self.target.write_text("ORIGINAL plus a browser write")
        with self.assertRaises(profilesafe.ProfileError):
            profilesafe.assert_unchanged(self.target, fp)

    def test_write_profile_file_moves_the_encrypted_twins_aside_and_restore_brings_them_back(self):
        (self.dir / "EncryptedBookmarks2").write_text("TWIN")
        result = profilesafe.write_profile_file(self.target, "NEW")
        self.assertEqual(self.target.read_text(), "NEW")
        self.assertEqual(result["backup"].read_text(), "ORIGINAL")
        self.assertFalse((self.dir / "EncryptedBookmarks2").exists())
        (moved,) = result["moved_aside"]
        self.assertEqual(moved.read_text(), "TWIN")
        self.assertTrue(moved.name.endswith(result["backup"].name[len("Bookmarks"):]))              # same stamp: they travel together
        (self.dir / "EncryptedBookmarks2").write_text("BROWSER REWROTE IT")                          # the browser recreates it after the next run
        self.target.write_text(bookmarks_json(("https://new.example/",)))
        profilesafe.write_profile_file(self.target, "NEWER")
        restored = profilesafe.restore_backup(self.target, result["backup"].name and result["backup"])
        self.assertEqual(self.target.read_text(), "ORIGINAL")
        self.assertEqual((self.dir / "EncryptedBookmarks2").read_text(), "TWIN")
        self.assertEqual(restored["previous_saved_as"].read_text(), "NEWER")

    def test_restore_refuses_strangers_and_bad_content(self):
        self.target.write_text(bookmarks_json())
        good = profilesafe.backup_file(self.target)
        with self.assertRaises(profilesafe.ProfileError):
            profilesafe.restore_backup(self.target, self.dir / "elsewhere")
        stranger = self.dir / "other.json"
        stranger.write_text(bookmarks_json())
        with self.assertRaises(profilesafe.ProfileError):
            profilesafe.restore_backup(self.target, stranger)
        good.write_text("{broken")
        with self.assertRaises(profilesafe.ProfileError) as cm:
            profilesafe.restore_backup(self.target)
        self.assertIn("not a valid", str(cm.exception))
        for b in profilesafe.list_backups(self.target):
            b.unlink()
        with self.assertRaises(profilesafe.ProfileError) as cm:
            profilesafe.restore_backup(self.target)
        self.assertIn("no mhbm backup", str(cm.exception))


class InstallChrome(CliCase):
    NEW = "https://library.example/"

    def setUp(self):
        super().setUp()
        src = self.dir / "src.json"
        src.write_text(formats.write_chrome([Bookmark(url=self.NEW, title="L", folder="Bookmarks bar")]))
        self.run_cli("import-browser", "x", "--profile", str(src))
        self.prof = self.dir / "User Data" / "Default"
        self.prof.mkdir(parents=True)
        (self.prof.parent / "Local State").write_text("{}")
        self.target = self.prof / "Bookmarks"
        self.original = bookmarks_json(("https://irreplaceable.example/",))
        self.target.write_text(self.original)

    def install(self, *extra, expect=0):
        return self.run_cli("install-chrome", str(self.target), "--confirm-chrome-closed", *extra, expect=expect)

    def lock(self, alive=True):
        import socket
        try:
            os.symlink(f"{socket.gethostname()}-31337", self.prof.parent / "SingletonLock")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available")
        return fake_pid(alive)

    def backups(self):
        return sorted(self.prof.glob("Bookmarks.mhbm-backup-*"))

    def test_running_browser_stops_the_write_and_changes_nothing(self):
        with self.lock(True), self.assertRaises(SystemExit) as cm:
            self.install()
        self.assertIn("Close the browser", str(cm.exception))
        self.assertEqual((self.target.read_text(), self.backups()), (self.original, []))

    def test_a_stale_lock_does_not_stop_it_and_the_override_works(self):
        with self.lock(False):
            self.install()
        self.assertIn(self.NEW, self.target.read_text())
        self.target.write_text(self.original)
        with self.lock(True) as _, self.assertRaises(SystemExit):
            self.install()
        with fake_pid(True):
            self.install("--ignore-profile-lock")
        self.assertIn(self.NEW, self.target.read_text())

    def test_synced_profile_needs_an_explicit_flag(self):
        self.target.write_text(bookmarks_json(("https://irreplaceable.example/",), sync_metadata="AAAA"))
        before = self.target.read_text()
        with self.assertRaises(SystemExit) as cm:
            self.install()
        self.assertIn("Sync", str(cm.exception))
        self.assertEqual(self.target.read_text(), before)
        self.install("--allow-synced-profile")
        self.assertIn(self.NEW, self.target.read_text())

    def test_two_runs_in_the_same_second_keep_the_original(self):
        with mock.patch.object(time, "strftime", lambda fmt, *a: "20260101-000000"):
            self.install()
            self.install()
        self.assertEqual(len(self.backups()), 2)
        self.assertTrue(any("irreplaceable.example" in b.read_text() for b in self.backups()))

    def test_a_failed_write_leaves_the_original_file_and_no_debris(self):
        with mock.patch.object(os, "replace", side_effect=OSError(28, "No space left on device")), self.assertRaises(OSError):
            self.install()
        self.assertEqual(self.target.read_text(), self.original)
        self.assertEqual([p.name for p in self.prof.iterdir() if p.name.endswith(".mhbm-tmp")], [])

    def test_file_changed_by_the_browser_while_rendering_is_not_overwritten(self):
        real = formats.render

        def render_then_browser_writes(*a, **k):
            out = real(*a, **k)
            self.target.write_text(bookmarks_json(("https://typed-meanwhile.example/",)))
            return out
        with mock.patch.object(formats, "render", render_then_browser_writes):
            self.install(expect=2)
        self.assertIn("typed-meanwhile", self.target.read_text())

    def test_encrypted_twin_is_moved_aside_not_deleted(self):
        (self.prof / "EncryptedBookmarks2").write_text("TWIN")
        out = self.js("install-chrome", str(self.target), "--confirm-chrome-closed")
        self.assertFalse((self.prof / "EncryptedBookmarks2").exists())
        self.assertEqual(len(out["moved_aside"]), 1)
        self.assertEqual(Path(out["moved_aside"][0]).read_text(), "TWIN")
        self.assertTrue(any("EncryptedBookmarks2" in w for w in out["warnings"]))

    def test_restore_round_trip(self):
        self.install()
        self.assertNotIn("irreplaceable", self.target.read_text())
        self.run_cli("install-chrome", str(self.target), "--confirm-chrome-closed", "--restore")
        self.assertEqual(self.target.read_text(), self.original)
        with self.assertRaises(SystemExit):
            self.run_cli("install-chrome", str(self.target), "--restore")                              # still needs the browser closed
        self.assertEqual(len(self.backups()), 2)                                                      # the original, and the file the restore replaced

    def test_restore_of_a_deleted_file_and_of_an_explicit_backup(self):
        self.install()
        (backup,) = self.backups()
        self.target.unlink()
        self.run_cli("install-chrome", str(self.target), "--confirm-chrome-closed", "--restore", str(backup))
        self.assertEqual(self.target.read_text(), self.original)
        self.run_cli("install-chrome", str(self.prof / "Bookmarks"), "--confirm-chrome-closed", "--restore", str(self.dir / "nope"), expect=2)

    def test_target_must_still_be_a_bookmarks_file(self):
        with self.assertRaises(SystemExit):
            self.run_cli("install-chrome", str(self.prof / "Preferences"), "--confirm-chrome-closed")
        with self.assertRaises(SystemExit):
            self.run_cli("install-chrome", str(self.prof / "Bookmarks.bak"), "--confirm-chrome-closed")


class SyncSafety(CliCase):
    def setUp(self):
        super().setUp()
        self.prof = self.dir / "User Data" / "Default"
        self.prof.mkdir(parents=True)
        (self.prof.parent / "Local State").write_text("{}")
        self.target = self.prof / "Bookmarks"
        self.target.write_text(formats.write_chrome([Bookmark(url="https://shared.example/", title="S", folder="Bookmarks bar")]))
        self.run_cli("import-browser", "x", "--profile", str(self.target))
        self.run_cli("sync", str(self.target), "--apply")                                           # baseline
        lib = self.lib()
        lib.upsert([Bookmark(url="https://library-new.example/", title="N", folder="Bookmarks bar", id="ln")])
        lib.close()

    def apply(self, *extra, expect=0):
        return self.run_cli("sync", str(self.target), "--apply", "--confirm-chrome-closed", *extra, expect=expect)

    def test_running_browser_blocks_a_sync_that_would_write_to_it(self):
        before = self.target.read_text()
        with mock.patch.object(profilesafe, "chromium_lock", lambda t: profilesafe.LockState("running", "process 7 holds it")):
            with self.assertRaises(SystemExit):
                self.apply()
            self.assertEqual(self.target.read_text(), before)
            self.apply("--ignore-profile-lock")
        self.assertIn("library-new", self.target.read_text())

    def test_a_sync_that_only_changes_the_library_never_needs_the_browser_closed(self):
        data = json.loads(self.target.read_text())
        data["roots"]["bookmark_bar"]["children"].append({"id": "900", "name": "R", "type": "url", "url": "https://remote-new.example/",
                                                          "guid": "11111111-1111-4111-8111-111111111111"})
        data["checksum"] = formats.chrome_checksum(data["roots"])
        self.target.write_text(json.dumps(data))
        lib = self.lib()
        lib.delete([b.id for b in lib.all() if b.url == "https://library-new.example/"])
        lib.close()
        with mock.patch.object(profilesafe, "chromium_lock", lambda t: profilesafe.LockState("running", "busy")):
            self.run_cli("sync", str(self.target), "--apply")
        self.assertIn("remote-new.example", [b.url for b in self.lib().all()].__repr__())

    def test_synced_profile_is_refused_unless_allowed_and_keeps_its_metadata_when_allowed(self):
        data = json.loads(self.target.read_text())
        data["sync_metadata"] = "QUJD"
        data["checksum_sha256"] = "stale"
        self.target.write_text(json.dumps(data))
        with self.assertRaises(SystemExit):
            self.apply()
        self.apply("--allow-synced-profile")
        out = json.loads(self.target.read_text())
        self.assertEqual(out["sync_metadata"], "QUJD")
        self.assertEqual(out["checksum_sha256"], formats.chrome_checksum_sha256(out["roots"]))
        self.assertEqual(out["checksum"], formats.chrome_checksum(out["roots"]))
        self.assertEqual(formats.verify_chrome(out), [])

    def test_bookmarks_bak_can_be_read_but_not_patched(self):
        bak = self.prof / "Bookmarks.bak"
        bak.write_text(self.target.read_text())
        self.js("sync", str(bak))
        with self.assertRaises(SystemExit) as cm:
            self.run_cli("sync", str(bak), "--apply", "--confirm-chrome-closed")
        self.assertIn("never reads Bookmarks.bak", str(cm.exception))

    def test_a_change_between_reading_and_writing_aborts_before_the_library_changes(self):
        real = sync.read_remote

        def read_then_browser_writes(path):
            out = real(path)
            Path(path).write_text(Path(path).read_text() + " ")
            return out
        count = self.lib().count()
        with mock.patch.object(sync, "read_remote", read_then_browser_writes):
            self.apply(expect=2)
        self.assertEqual(self.lib().count(), count)

    @unittest.skipUnless(hasattr(os, "symlink") and os.name != "nt", "needs symlinks")
    def test_a_symlinked_bookmarks_file_stays_a_symlink(self):
        real = self.dir / "dotfiles" / "Bookmarks"
        real.parent.mkdir()
        real.write_text(self.target.read_text())
        self.target.unlink()
        self.target.symlink_to(real)
        self.apply()
        self.assertTrue(self.target.is_symlink())
        self.assertIn("library-new", real.read_text())

    def test_missing_root_is_an_error_message_not_a_traceback(self):
        data = json.loads(self.target.read_text())
        del data["roots"]["other"]
        data["roots"]["bookmark_bar"]["children"] = []
        self.target.write_text(json.dumps(data))
        self.apply(expect=2)

    def test_verify_chrome_reports_a_stale_sha256(self):
        data = json.loads(bookmarks_json())
        data["checksum_sha256"] = formats.chrome_checksum_sha256(data["roots"])
        self.assertEqual(formats.verify_chrome(data), [])
        data["checksum_sha256"] = "0" * 64
        self.assertTrue(any("checksum_sha256" in p for p in formats.verify_chrome(data)))


if __name__ == "__main__":
    unittest.main()
