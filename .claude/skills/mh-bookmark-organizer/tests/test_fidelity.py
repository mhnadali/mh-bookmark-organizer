import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import common  # noqa: F401
from mhbm import browsers, formats
from mhbm.model import Bookmark
from mhbm.store import Library
from test_cli import CliCase

ICON = "data:image/png;base64,iVBORw0KGgo="
HTML = f"""<!DOCTYPE NETSCAPE-Bookmark-file-1>
<DL><p>
  <DT><H3 ADD_DATE="1600000000" LAST_MODIFIED="1700000000" PERSONAL_TOOLBAR_FOLDER="true">Bookmarks bar</H3>
  <DL><p>
    <DT><A HREF="https://a.com/" ADD_DATE="1600000001" ICON="{ICON}">With icon</A>
    <DT><A HREF="https://b.com/" ICON="javascript:alert(1)">Bad icon</A>
    <DT><A HREF="https://c.com/"></A>
  </DL><p>
</DL><p>
"""


class ChromeFormat(unittest.TestCase):
    def doc(self):
        bms = [Bookmark(url="https://a.com", title="A", folder="Bookmarks bar/Dev", added=1600000000, id="a"),
               Bookmark(url="https://b.com", title="", folder="Other bookmarks", id="b"),
               Bookmark(url="https://c.com", title="Тест ✓ 日本", folder="Work/Home", id="c")]
        return json.loads(formats.write_chrome(bms, {"Bookmarks bar/Dev", "Bookmarks bar/Empty"}, {"Bookmarks bar/Dev": (1500000000, 1600000000)}))

    def test_written_file_passes_verification(self):
        self.assertEqual(formats.verify_chrome(self.doc()), [])

    def test_ids_guids_roots_and_dates(self):
        d = self.doc()
        r = d["roots"]
        self.assertEqual({k: v["guid"] for k, v in r.items()}, formats.CHROME_ROOT_GUIDS)
        dev = next(c for c in r["bookmark_bar"]["children"] if c["name"] == "Dev")
        self.assertEqual(dev["date_modified"], str((1600000000 + formats.WEBKIT_EPOCH_OFFSET) * 10**6))
        self.assertTrue(any(c["name"] == "Empty" for c in r["bookmark_bar"]["children"]))
        self.assertEqual(next(c for c in r["other"]["children"] if c["url"] == "https://b.com")["name"], "")  # empty title kept

    def test_checksum_follows_the_documented_algorithm(self):
        d = self.doc()
        md5 = hashlib.md5()

        def walk(n):
            md5.update(n["id"].encode())
            md5.update(n["name"].encode("utf-16-le"))
            if n["type"] == "url":
                md5.update(b"url" + n["url"].encode())
            else:
                md5.update(b"folder")
                for c in n["children"]:
                    walk(c)
        for k in ("bookmark_bar", "other", "synced"):
            walk(d["roots"][k])
        self.assertEqual(d["checksum"], md5.hexdigest())

    def test_verification_catches_tampering(self):
        d = self.doc()
        d["roots"]["other"]["children"][0]["url"] = "https://evil.example"
        self.assertTrue(any("checksum mismatch" in p for p in formats.verify_chrome(d)))
        d = self.doc()
        d["roots"]["other"]["children"].append(dict(d["roots"]["other"]["children"][0]))
        self.assertTrue(any("duplicate id" in p for p in formats.verify_chrome(d)))
        d = self.doc()
        del d["roots"]["synced"]
        self.assertTrue(any("missing root" in p for p in formats.verify_chrome(d)))
        d = self.doc()
        d["roots"]["other"]["children"][0]["guid"] = "nope"
        self.assertTrue(any("malformed guid" in p for p in formats.verify_chrome(d)))

    def test_round_trip(self):
        back = formats.parse_chrome(self.doc())
        self.assertEqual(sorted((b.url, b.folder, b.title) for b in back),
                         [("https://a.com", "Bookmarks bar/Dev", "A"), ("https://b.com", "Other bookmarks", ""),
                          ("https://c.com", "Other bookmarks/Work/Home", "Тест ✓ 日本")])


class HtmlFidelity(unittest.TestCase):
    def test_icons_folder_dates_and_empty_titles_survive(self):
        bms = formats.parse_netscape(HTML)
        self.assertEqual((bms[0].icon, bms[1].icon, bms[2].title), (ICON, "", ""))      # non-image icons are ignored
        self.assertEqual(bms.folder_meta["Bookmarks bar"], (1600000000, 1700000000))
        out = formats.write_netscape(bms, bms.folders, icons={}, folder_meta=bms.folder_meta)
        again = formats.parse_netscape(out)
        self.assertEqual([(b.url, b.title, b.icon, b.added) for b in again], [(b.url, b.title, b.icon, b.added) for b in bms])
        self.assertEqual(again.folder_meta, bms.folder_meta)
        self.assertIn("></A>", out)                                                    # empty anchor text not replaced by the URL


class Cli(CliCase):
    def test_icons_and_dates_go_through_the_library(self):
        self.run_cli("import", self.write("in.html", HTML))
        out = self.dir / "o.html"
        self.run_cli("export", "--format", "html", "--out", str(out))
        text = out.read_text()
        self.assertIn(f'ICON="{ICON}"', text)
        self.assertIn('LAST_MODIFIED="1700000000"', text)

    def test_skipped_repeats_are_listed(self):
        src = self.write("in.csv", "url,title,folder\nhttps://a.com,A,X\nhttps://a.com,A again,X\nhttps://b.com,B,X\n")
        r = self.js("import", src)
        self.assertEqual((r["imported"], r["skipped_existing"], r["skipped_items"]), (2, 1, [{"url": "https://a.com", "folder": "X"}]))
        self.assertIn("skipped: [X] https://a.com", self.run_cli("import", src, "--show-skipped"))

    def test_verify_chrome_command(self):
        self.run_cli("import", self.write("in.html", HTML))
        good = self.dir / "Bookmarks"
        self.run_cli("export", "--format", "chrome", "--out", str(good))
        self.assertTrue(self.js("verify-chrome", str(good))["ok"])
        bad = json.loads(good.read_text())
        bad["roots"]["bookmark_bar"]["children"][0]["name"] = "tampered"
        tampered = self.dir / "Bad"
        tampered.write_text(json.dumps(bad))
        self.run_cli("verify-chrome", str(tampered), expect=2)

    def test_persian_and_arabic_letter_variants_find_each_other(self):
        lib = Library(self.home)
        self.addCleanup(lib.close)
        lib.upsert([Bookmark(url="https://a.com", title="كتاب تحليل بازار", notes="می‌خواهم ۱۲۳", id="a")])
        q = lambda s: [h[0].id for h in lib.search(s)]  # noqa: E731
        self.assertEqual(q("کتاب"), ["a"])                 # Persian kaf finds Arabic kaf
        self.assertEqual(q("تحلیل"), ["a"])                # Persian yeh finds Arabic yeh
        self.assertEqual(q("می خواهم"), ["a"])             # ZWNJ == space
        self.assertEqual(q("123"), ["a"])                  # Persian digits == ASCII digits


def lz4_literals(data: bytes) -> bytes:
    n = len(data)
    token = bytearray([min(n, 15) << 4])
    if n >= 15:
        rest = n - 15
        while rest >= 255:
            token.append(255)
            rest -= 255
        token.append(rest)
    return bytes(token) + data


class Mozlz4(unittest.TestCase):
    def test_decoder(self):
        self.assertEqual(browsers.lz4_block_decompress(lz4_literals(b"hello"), 5), b"hello")
        long = bytes(range(256)) * 3
        self.assertEqual(browsers.lz4_block_decompress(lz4_literals(long), len(long)), long)       # extended literal length
        overlapping = bytes([3 << 4 | 5]) + b"abc" + bytes([3, 0]) + bytes([0])
        self.assertEqual(browsers.lz4_block_decompress(overlapping, 12), b"abcabcabcabc")
        with self.assertRaises(formats.FormatError):
            browsers.lz4_block_decompress(bytes([1 << 4 | 5]) + b"a" + bytes([9, 0]), 10)         # offset beyond output

    def test_firefox_backup_file_is_imported(self):
        data = {"children": [{"root": "toolbarFolder", "title": "toolbar", "children": [
            {"typeCode": 1, "uri": "https://ff.example", "title": "FF", "tags": "t", "dateAdded": 1600000000000000}]}]}
        raw = json.dumps(data).encode()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "bookmarks-2026-01-01_1_abc.jsonlz4")
            p.write_bytes(browsers.MOZLZ4_MAGIC + len(raw).to_bytes(4, "little") + lz4_literals(raw))
            got = browsers.load("firefox", profile=str(p))
            self.assertEqual([(b.url, b.folder, b.tags) for b in got], [("https://ff.example", "Bookmarks bar", ["t"])])
            bad = Path(d, "bad.jsonlz4")
            bad.write_bytes(b"not mozlz4 at all")
            with self.assertRaises(formats.FormatError):
                browsers.load("firefox", profile=str(bad))


if __name__ == "__main__":
    unittest.main()
