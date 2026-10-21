"""The guards that keep a Chromium ``Bookmarks`` file acceptable, without needing a browser.

Each rule here was measured on a real browser first (``tests/test_realbrowser.py`` repeats the measurement when one is
installed): what Chromium does with a wrong version, a missing root, a URL it cannot parse, URLs it re-encodes, ...
"""
import json
import os
import socket
import stat
import sys
import tempfile
import unittest
from pathlib import Path

import common  # noqa: F401
from mhbm import formats, sync
from mhbm.model import Bookmark
from mhbm.urlnorm import browser_key, normalize_url


def valid_doc(extra_url="https://a.example/"):
    bms = [Bookmark(url=extra_url, title="A", folder="Bookmarks bar/Dev", added=1700000000, id="1"),
           Bookmark(url="https://b.example/", title="B", folder="Other bookmarks", added=1700000001, id="2")]
    return json.loads(formats.write_chrome(bms, ["Bookmarks bar/Dev"]))


class BrowserKey(unittest.TestCase):
    PAIRS = [  # (what a library may hold, what Chromium stores for it)
        ("https://example.org/a b/ü?q=1#x", "https://example.org/a%20b/%C3%BC?q=1#x"),
        ("https://fa.example.org/تحلیل داده/صفحه", "https://fa.example.org/%D8%AA%D8%AD%D9%84%DB%8C%D9%84%20%D8%AF%D8%A7%D8%AF%D9%87/%D8%B5%D9%81%D8%AD%D9%87"),
        ("https://münchen.example/straße", "https://xn--mnchen-3ya.example/stra%C3%9Fe"),
        ("https://example.net/a|b^c`d{e}f", "https://example.net/a%7Cb%5Ec%60d%7Be%7Df"),
        ("https://example.net/a/../b/./c", "https://example.net/b/c"),
        ("HTTPS://Example.COM:443/Path", "https://example.com/Path"),
        ("https://example.com", "https://example.com/"),
        ("https://example.net/\\backslash", "https://example.net//backslash"),
        ("  https://example.net/tab\there  ", "https://example.net/tabhere"),
    ]

    def test_a_url_and_the_form_the_browser_stores_have_one_key(self):
        for raw, stored in self.PAIRS:
            self.assertEqual(browser_key(raw), browser_key(stored), raw)

    def test_distinct_pages_stay_distinct(self):
        for a, b in [("https://x.example/a%2Fb", "https://x.example/a/b"), ("https://x.example/a+b", "https://x.example/a%20b"),
                     ("https://x.example/A", "https://x.example/a"), ("https://x.example/?q=1", "https://x.example/?q=2"),
                     ("https://x.example/p?x=a%2Bb", "https://x.example/p?x=a+b")]:
            self.assertNotEqual(browser_key(a), browser_key(b), (a, b))

    def test_agrees_with_normalize_url_for_plain_urls(self):
        for u in ["https://www.example.com/a?utm_source=x&b=1", "http://example.com:80/x/", "https://example.com/#/route", "https://example.com/#anchor",
                  "mailto:a@b", "javascript:void(0)", "https://[::1]:8080/x"]:
            self.assertEqual(browser_key(u), normalize_url(u), u)


class UrlOk(unittest.TestCase):
    def test_what_chromium_drops(self):
        for u in ["", " ", "example.com", "www.example.com/x", "192.168.0.1", "localhost", "git@github.com:a/b.git", "//example.com/x", "/relative", "?q", "#f",
                  "http://", "http://:80/", "http://user@/", "http://[::1", "http://h:99999/", "http://h:abc/", "http://999.999.999.999/", "http://exa<mple.com/",
                  "ht tp://x.com", "chrome://", "ftp://", None, 42]:
            self.assertFalse(formats.chrome_url_ok(u), u)

    def test_what_it_keeps(self):
        for u in ["https://example.com", "HTTP://EXAMPLE.COM", "http:example.com", "http:///x", "https://über.de", "http://exa mple.com/", "http://[::1]/",
                  "http://1.2.3/", "http://localhost", "localhost:8080/x", "file:///x", "about:blank", "data:,x", "javascript:1", "mailto:a@b", "chrome://settings",
                  "ssh://h", "magnet:?xt=1", "https://example.com/" + "a" * 70000]:
            self.assertTrue(formats.chrome_url_ok(u), u[:60])


class VerifyChrome(unittest.TestCase):
    def test_a_good_document_is_clean(self):
        self.assertEqual(formats.verify_chrome(valid_doc()), [])

    def test_whole_file_killers_are_reported(self):
        d = valid_doc()
        d["version"] = 2
        self.assertTrue(any("version" in p for p in formats.verify_chrome(d)))
        d = valid_doc()
        d.pop("version")
        self.assertTrue(any("version" in p for p in formats.verify_chrome(d)))
        d = valid_doc()
        d["roots"].pop("synced")
        self.assertIn("missing root 'synced'", formats.verify_chrome(d))

    def test_node_level_problems_are_reported_without_crashing(self):
        for mutate, needle in [(lambda n: n.pop("id"), "missing 'id'"), (lambda n: n.update(id="0"), "positive integer"), (lambda n: n.update(id="abc"), "positive integer"),
                               (lambda n: n.update(id=5), "positive integer"), (lambda n: n.update(name=42), "not a string"),
                               (lambda n: n.update(url="example.com"), "silently drops"), (lambda n: n.update(url=""), "without url")]:
            d = valid_doc()
            mutate(d["roots"]["other"]["children"][0])
            self.assertTrue(any(needle in p for p in formats.verify_chrome(d)), needle)

    def test_depth_limit(self):
        node = {"id": "9", "name": "x", "type": "folder", "guid": "00000000-0000-4000-8000-000000000009", "children": []}
        d = valid_doc()
        cur = d["roots"]["other"]
        for i in range(formats.MAX_CHROME_DEPTH + 2):
            child = {"id": str(100 + i), "name": f"d{i}", "type": "folder", "guid": f"00000000-0000-4000-8000-{i + 1000:012x}", "children": []}
            cur["children"].append(child)
            cur = child
        self.assertTrue(any("nested" in p for p in formats.verify_chrome(d)))
        with self.assertRaises(formats.FormatError):
            formats.write_chrome([Bookmark(url="https://x.example/", folder="/".join(f"f{i}" for i in range(formats.MAX_CHROME_DEPTH + 1)))])
        self.assertIsNotNone(node)

    def test_missing_checksum_is_not_fatal_for_the_cli_but_is_said_plainly(self):
        d = valid_doc()
        d.pop("checksum")
        (problem,) = formats.verify_chrome(d)
        self.assertTrue(problem.startswith("no checksum"))

    def test_bom_is_read(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t, "Bookmarks")
            p.write_bytes(b"\xef\xbb\xbf" + json.dumps(valid_doc()).encode())
            self.assertEqual(formats.sniff(p), "chrome")
            self.assertEqual(len(formats.read_file(p)), 2)
            self.assertEqual(len(sync.read_remote(p)[1]), 2)


class Writer(unittest.TestCase):
    def test_urls_the_browser_would_drop_are_left_out_and_reported(self):
        skipped = []
        bms = [Bookmark(url="https://ok.example/", title="ok"), Bookmark(url="example.com", title="bad"), Bookmark(url="http://", title="bad2")]
        doc = json.loads(formats.write_chrome(bms, [], None, skipped))
        self.assertEqual([b.url for b in skipped], ["example.com", "http://"])
        self.assertEqual([b.url for b in formats.parse_chrome(doc)], ["https://ok.example/"])
        self.assertEqual(formats.verify_chrome(doc), [])


class SyncKeys(unittest.TestCase):
    def test_library_folder_and_browser_folder_are_the_same_place(self):
        for lib_folder, file_folder in [("Work/Projects", "Other bookmarks/Work/Projects"), ("", "Other bookmarks"), ("Bookmarks bar/Dev", "Bookmarks bar/Dev"),
                                        ("Mobile bookmarks", "Mobile bookmarks"), ("Other bookmarks/X", "Other bookmarks/X"),
                                        ("Line\nbreak\ttab", "Other bookmarks/Line break tab")]:    # the browser turns tab/CR/LF/U+2028/9 into a space
            self.assertEqual(sync.key_of("https://a.example/", lib_folder), sync.key_of("https://a.example/", file_folder), lib_folder)

    def test_plan_is_empty_when_the_browser_only_re_encoded_what_we_wrote(self):
        local = [Bookmark(url="https://example.org/a b/ü", title="T", folder="Work"), Bookmark(url="https://x.example/", title="U", folder="")]
        remote = [Bookmark(url="https://example.org/a%20b/%C3%BC", title="T", folder="Other bookmarks/Work"), Bookmark(url="https://x.example/", title="U", folder="Other bookmarks")]
        baseline = {sync.key_of(b.url, b.folder): b.title for b in remote}
        plan = sync.plan_sync(local, remote, baseline)
        self.assertEqual(plan.summary(), {"add_to_library": 0, "delete_from_library": 0, "add_to_browser": 0, "delete_from_browser": 0,
                                          "retitle_library": 0, "retitle_browser": 0, "unchanged": 2})

    def test_unholdable_urls_are_never_written_and_never_deleted_from_the_library(self):
        local = [Bookmark(url="example.com", title="no scheme", folder="Other bookmarks/Work", tags=["t"])]
        baseline = {sync.key_of("example.com", "Other bookmarks/Work"): "no scheme"}     # an older sync wrote it; the browser dropped it
        plan = sync.plan_sync(local, [], baseline)
        self.assertEqual((plan.add_remote, plan.delete_local, len(plan.unsyncable)), ([], [], 1))
        self.assertEqual(plan.summary()["not_syncable"], 1)


class Files(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "POSIX permissions")
    def test_write_remote_is_atomic_and_keeps_permissions(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t, "Bookmarks")
            p.write_text(json.dumps(valid_doc()))
            os.chmod(p, 0o600)
            data, _ = sync.read_remote(p)
            backup = sync.write_remote(p, data)
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            self.assertEqual(list(Path(t).glob("*.mhbm-tmp")), [])
            self.assertTrue(backup.exists())
            sync.write_text_atomic(p, "{}")
            self.assertEqual((stat.S_IMODE(p.stat().st_mode), p.read_text()), (0o600, "{}"))

    @unittest.skipIf(sys.platform == "win32", "POSIX lock file")
    def test_browser_running_reads_the_singleton_lock_and_ignores_a_stale_one(self):
        with tempfile.TemporaryDirectory() as t:
            profile = Path(t, "User Data", "Default")
            profile.mkdir(parents=True)
            bookmarks = profile / "Bookmarks"
            bookmarks.write_text("{}")
            Path(t, "User Data", "Local State").write_text("{}")         # what marks the folder above `Default` as a browser's own
            self.assertFalse(sync.browser_running(bookmarks))
            lock = Path(t, "User Data", "SingletonLock")
            host = socket.gethostname()
            lock.symlink_to(f"{host}-{os.getpid()}")                    # a live pid on this computer
            self.assertTrue(sync.browser_running(bookmarks))
            lock.unlink()
            lock.symlink_to(f"{host}-999999999")                        # left behind by a crash
            self.assertFalse(sync.browser_running(bookmarks))
            lock.unlink()
            lock.symlink_to("another-computer-999999999")               # a profile used from elsewhere: cannot be checked, so it counts
            self.assertTrue(sync.browser_running(bookmarks))

if __name__ == "__main__":
    unittest.main()
