"""The Chromium ``Bookmarks`` file this tool writes, patches and reads, checked against a REAL Chromium-family browser.

Everything else in the suite checks our files against our own idea of Chromium's format. These tests start the browser
(headless, a scratch profile, a tiny probe extension that exposes ``chrome.bookmarks``) and compare what it does with what
we claim:

* a file the browser wrote passes ``verify-chrome`` and survives import + export;
* a file ``install-chrome`` wrote loads unchanged (ids, order, titles, URLs, dates) and, after the browser rewrites it,
  still passes ``verify-chrome`` with the same checksum and GUIDs;
* ``sync`` patches a browser-written profile without collisions or losses, and a second ``sync`` after the browser has
  rewritten the file (it canonicalises every URL) finds nothing to do;
* the guards that matter are real: a wrong ``version`` / missing root makes the browser discard the whole file, and
  a URL it cannot parse is dropped silently.

Skipped unless node, Playwright (``playwright-core``) and a browser are available: set ``CHROMIUM_BIN`` to test a specific
binary (Chromium, Brave, ungoogled-chromium, Edge...), or install one with ``npx playwright-core install chromium``.
Each browser start takes about a second; the module needs roughly 20 s.
"""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import common  # noqa: F401  (puts scripts/ on sys.path)
from mhbm import formats, sync
from mhbm.model import Bookmark, assign_ids
from mhbm.urlnorm import browser_key
from test_cli import CliCase

HERE = Path(__file__).resolve().parent
DRIVER = HERE / "realbrowser" / "driver.js"
FIXTURES = HERE / "fixtures"
NODE = shutil.which("node")
BROWSER = {"ok": False, "why": "needs node", "major": 0}


def run_driver(*args, timeout=120):
    return subprocess.run([NODE, str(DRIVER), *map(str, args)], capture_output=True, text=True, timeout=timeout)


def setUpModule():
    if not NODE:
        return
    probe = run_driver("probe")
    if probe.returncode != 0:
        BROWSER["why"] = "no Playwright or no browser: " + probe.stderr.strip()[-200:]
        return
    with tempfile.TemporaryDirectory() as d:
        out = Path(d, "o.json")
        smoke = run_driver("dump", Path(d, "p"), out)
        if smoke.returncode != 0 or not out.exists():
            BROWSER["why"] = "this sandbox cannot start the browser with an extension: " + smoke.stderr.strip()[-200:]
            return
        BROWSER.update(ok=True, major=int(json.loads(out.read_text()).get("version") or 0), bin=json.loads(probe.stdout)["bin"])


def flat_file(data) -> list:
    """Nodes of a Bookmarks document in document order as (depth, id, type, title, url, date_added_ms)."""
    out = []

    def walk(node, depth):
        added = node.get("date_added")
        # the three permanent folders are titled by the browser's own UI strings ("Bookmarks" in Brave <= 143), never by the file
        out.append((depth, str(node["id"]), node["type"], node["name"] if depth else "<root>", node.get("url"),
                    int(added) // 1000 - formats.WEBKIT_EPOCH_OFFSET * 1000 if added not in (None, "0") else None))
        for child in node.get("children", []):
            walk(child, depth + 1)
    for key in formats.CHROME_ROOTS:
        root = data["roots"][key]
        if key == "synced" and not root.get("children"):
            continue    # the browser hides an empty "Mobile bookmarks" root from chrome.bookmarks
        walk(root, 0)
    return out


def flat_browser(tree) -> list:
    """The same shape from the driver's flattened chrome.bookmarks.getTree() (the artificial root is dropped)."""
    depth, out = {"0": -1}, []
    for n in tree:
        if n["id"] == "0":
            continue
        d = depth[n["parentId"]] + 1
        depth[n["id"]] = d
        out.append((d, n["id"], "url" if "url" in n else "folder", n["title"] if d else "<root>", n.get("url"), n.get("dateAdded")))
    return out


def guids(data) -> list:
    out = []

    def walk(node):
        out.append(node["guid"])
        for child in node.get("children", []):
            walk(child)
    for key in formats.CHROME_ROOTS:
        walk(data["roots"][key])
    return out


@unittest.skipUnless(NODE, "needs node")
class RealBrowser(CliCase):
    def setUp(self):
        super().setUp()
        if not BROWSER["ok"]:
            self.skipTest(BROWSER["why"])
        self.profile = self.dir / "profile"
        self.bookmarks = self.profile / "Default" / "Bookmarks"

    # ----------------------------------------------------------------- helpers
    def browser(self, *args):
        r = run_driver(*args)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])

    def seed_profile(self, spec=None):
        """A profile whose Bookmarks file was written by the browser itself."""
        spec_file = self.dir / "seed.json"
        spec_file.write_text(json.dumps(spec or {"bar": [{"title": "seed", "url": "https://seed.example/"}]}, ensure_ascii=False), encoding="utf-8")
        self.browser("create", self.profile, spec_file, self.dir / "seed-tree.json")
        self.assertTrue(self.bookmarks.is_file())
        return json.loads((self.dir / "seed-tree.json").read_text())

    def dump(self, touch=False):
        out = self.dir / f"dump-{len(list(self.dir.glob('dump-*')))}.json"
        self.browser("dump", self.profile, out, *(["--touch"] if touch else []))
        return json.loads(out.read_text())

    def library_from_messy_fixture(self):
        sys.path.insert(0, str(FIXTURES))
        import gen_messy_library
        html_file = self.dir / "messy.html"
        html_file.write_text(gen_messy_library.generate(), encoding="utf-8")
        self.run_cli("import", str(html_file))

    def put_bookmarks(self, *bms, folders=()):
        lib = self.lib()
        assign_ids(list(bms), lib.ids())
        lib.upsert(list(bms))
        lib.add_folders(folders)
        lib.close()

    # --------------------------------------------------------- A: browser -> us
    def test_a_file_written_by_the_browser_verifies_and_survives_import_and_export(self):
        big = "https://example.com/" + "a" * 10000 + "?q=" + "b" * 5000
        spec = {"bar": [{"title": "تحلیل داده‌ها", "url": "https://fa.example.org/تحلیل?x=۱"},
                        {"title": "Dup", "url": "https://dup.example/"}, {"title": "Dup", "url": "https://dup.example/other"},
                        {"title": "Folder فارسی", "children": [
                            {"title": "Nested", "children": [{"title": "Deeper ✓ 日本語 😀", "children": [{"title": "leaf", "url": "https://deep.example/a b"}]}]},
                            {"title": "Empty nested", "children": []}, {"title": "", "url": "https://empty-title.example/"}]},
                        {"title": "Long", "url": big}, {"title": "bookmarklet", "url": "javascript:void(document.title)"},
                        {"title": "data", "url": "data:text/html;base64,PGgxPmhpPC9oMT4="}, {"title": "A/B", "children": [{"title": "x", "url": "https://slash.example/"}]}],
                "other": [{"title": "Other folder", "children": [{"title": "o1", "url": "https://other.example/2"}]}]}
        self.seed_profile(spec)
        data = json.loads(self.bookmarks.read_text(encoding="utf-8"))
        self.assertEqual(formats.verify_chrome(data), [], "our checksum / id / GUID logic disagrees with the browser's own file")
        self.assertEqual(data["version"], 1)
        self.run_cli("import", "--allow-duplicates", str(self.bookmarks))
        out = self.dir / "re.json"
        self.run_cli("export", "--format", "chrome", "--out", str(out))
        again = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(formats.verify_chrome(again), [])
        key = lambda d: sorted((b.folder, b.title, b.url) for b in formats.parse_chrome(d))   # noqa: E731
        self.assertEqual(key(again), key(data))

    # --------------------------------------------------------- B: us -> browser
    def test_install_chrome_output_loads_unchanged_and_stays_valid_after_the_browser_rewrites_it(self):
        self.seed_profile()
        self.library_from_messy_fixture()
        self.run_cli("install-chrome", str(self.bookmarks), "--confirm-chrome-closed")
        ours = json.loads(self.bookmarks.read_text(encoding="utf-8"))
        self.assertEqual(formats.verify_chrome(ours), [])
        loaded = self.dump()
        self.assertEqual(flat_browser(loaded["tree"]), flat_file(ours), "the browser changed ids, order, titles, URLs or dates")
        rewritten = self.dump(touch=True)                       # now the browser saves the file itself
        self.assertEqual(flat_browser(rewritten["tree"]), flat_file(ours))
        theirs = json.loads(self.bookmarks.read_text(encoding="utf-8"))
        self.assertEqual(formats.verify_chrome(theirs), [])
        names = lambda d: [r["name"] for r in d["roots"].values()]   # noqa: E731
        if names(theirs) == names(ours):    # Brave <= 143 renames the bar to "Bookmarks"; the checksum covers names, so it then differs legitimately
            self.assertEqual(theirs["checksum"], ours["checksum"], "the browser computes a different checksum for the same tree")
        self.assertEqual(guids(theirs), guids(ours))
        self.assertTrue((self.profile / "Default" / "Bookmarks.bak").exists())

    def test_install_chrome_leaves_out_what_the_browser_would_silently_drop(self):
        self.seed_profile()
        self.put_bookmarks(Bookmark(url="https://ok.example/", title="Fine", folder="Other bookmarks/Work", added=1700000000),
                           Bookmark(url="example.com", title="No scheme", folder="Other bookmarks/Work", tags=["keep"]),
                           Bookmark(url="http://", title="Empty host", folder="Other bookmarks/Work"),
                           folders=["Other bookmarks/Work"])
        out = self.js("install-chrome", str(self.bookmarks), "--confirm-chrome-closed")
        self.assertEqual(sorted(out["not_written"]), ["example.com", "http://"])
        self.assertEqual(formats.verify_chrome(json.loads(self.bookmarks.read_text(encoding="utf-8"))), [])
        titles = sorted(n["title"] for n in self.dump()["tree"] if n.get("url"))
        self.assertEqual(titles, ["Fine"])
        self.assertEqual(len([b for b in self.lib().all() if b.tags == ["keep"]]), 1, "the library keeps what the browser cannot hold")

    def test_sync_patches_a_browser_written_profile_without_collisions_or_losses(self):
        seed = self.seed_profile({"bar": [{"title": "تحلیل", "url": "https://fa.example/x"}, {"title": "F", "children": [{"title": "in F", "url": "https://f.example/"}]}],
                                  "other": [{"title": "o", "url": "https://o.example/"}]})
        before = json.loads(self.bookmarks.read_text(encoding="utf-8"))
        self.library_from_messy_fixture()
        self.run_cli("sync", str(self.bookmarks), "--apply", "--confirm-chrome-closed")
        patched = json.loads(self.bookmarks.read_text(encoding="utf-8"))
        self.assertEqual(formats.verify_chrome(patched), [])
        ids = [n[1] for n in flat_file(patched)]
        self.assertEqual(len(ids), len(set(ids)), "id collision")
        old = {n[1]: n for n in flat_file(before)}
        new = {n[1]: n for n in flat_file(patched)}
        self.assertTrue(all(new[i][2:] == old[i][2:] for i in old), "sync altered or lost a node the browser already had")
        loaded = self.dump(touch=True)
        self.assertEqual(sorted(n[3:5] for n in flat_browser(loaded["tree"]) if n[2] == "url" and n[3] != "touch"),
                         sorted(n[3:5] for n in flat_file(patched) if n[2] == "url"))
        theirs = json.loads(self.bookmarks.read_text(encoding="utf-8"))
        self.assertEqual((formats.verify_chrome(theirs), theirs["checksum"]), ([], patched["checksum"]))
        self.assertGreater(len(seed["tree"]), 3)

    def test_second_sync_after_the_browser_rewrote_the_file_finds_nothing_to_do(self):
        """Folders without a root name, unfiled bookmarks, URLs the browser re-encodes, names it rewrites, URLs it cannot hold."""
        self.seed_profile()
        self.put_bookmarks(
            Bookmark(url="https://ok.example/page", title="In a root-less folder", folder="Work/Projects", tags=["t1"]),
            Bookmark(url="https://ok.example/loose", title="Unfiled", folder=""),
            Bookmark(url="https://example.org/a b/ü?q=1#x", title="raw spaces and umlaut", folder="Bookmarks bar/Raw"),
            Bookmark(url="https://fa.example.org/تحلیل داده/صفحه", title="raw Persian path", folder="Bookmarks bar/Raw"),
            Bookmark(url="https://münchen.example/straße", title="IDN host", folder="Bookmarks bar/Raw"),
            Bookmark(url="HTTPS://Example.COM", title="upper case, no slash", folder="Bookmarks bar/Raw"),
            Bookmark(url="https://example.net/a/../b/./c", title="dot segments", folder="Bookmarks bar/Raw"),
            Bookmark(url="https://ws.example/1", title="newline in the folder name", folder="Other bookmarks/Line\nbreak"),
            Bookmark(url="example.com", title="no scheme", folder="Other bookmarks/Work", tags=["important"], notes="precious"),
            folders=["Work/Projects", "Bookmarks bar/Raw", "Other bookmarks/Work", "Other bookmarks/Line\nbreak"])
        self.run_cli("sync", str(self.bookmarks), "--apply", "--confirm-chrome-closed")
        self.dump(touch=True)                                    # the browser loads, canonicalises and rewrites the file
        again = self.js("sync", str(self.bookmarks))
        moved = {k: v for k, v in again.items() if k.startswith(("add_", "delete_", "retitle_")) and v}
        self.assertEqual(moved, {}, "sync sees changes that nobody made: " + json.dumps(again))
        self.assertEqual(again.get("not_syncable"), 1)
        self.run_cli("sync", str(self.bookmarks), "--apply", "--confirm-chrome-closed")
        kept = [b for b in self.lib().all() if b.url == "example.com"]
        self.assertEqual([(b.tags, b.notes) for b in kept], [(["important"], "precious")], "a bookmark the browser cannot hold was deleted from the library")

    # ------------------------------------------------- C: why the guards exist
    def test_structural_damage_is_caught_by_verify_chrome_and_really_costs_the_whole_file(self):
        self.seed_profile({"bar": [{"title": "a", "url": "https://a.example/"}], "other": [{"title": "b", "url": "https://b.example/"}]})
        good = json.loads(self.bookmarks.read_text(encoding="utf-8"))

        def load_with(mutate):
            data = json.loads(json.dumps(good))
            mutate(data)
            data["checksum"] = formats.chrome_checksum(data["roots"])
            self.bookmarks.write_text(json.dumps(data), encoding="utf-8")
            problems = formats.verify_chrome(data)
            return problems, len([n for n in self.dump()["tree"] if n.get("url")])
        problems, urls = load_with(lambda d: d.update(version=2))
        self.assertTrue(problems and urls == 0, (problems, urls))                       # whole file discarded
        problems, urls = load_with(lambda d: d["roots"].pop("synced"))
        self.assertTrue(problems and urls == 0, (problems, urls))
        problems, urls = load_with(lambda d: d["roots"]["other"]["children"][0].update(url="not a url"))
        self.assertTrue(problems and urls == 1, (problems, urls))                       # only that node is dropped
        problems, urls = load_with(lambda d: None)
        self.assertEqual((problems, urls), ([], 2))

    def test_browser_url_forms_have_the_same_key_as_the_raw_urls_we_store(self):
        rnd = random.Random(7)
        segs = ["docs", "a b", "ü", "straße", "تحلیل", "日本語", "%41", "%7e", "%2F", "%c3%bc", "a|b", "a^b", "a`b", "a{b}", "a\"b", "a<b>", ".", "..", "%2e", "emoji😀", "a\\b", ""]
        hosts = ["example.com", "Example.COM", "münchen.de", "bücher.example", "日本語.jp", "تست.ir", "a.b.example", "EXAMPLE.org:443", "x.org:8080", "x.org:80"]
        keys, vals = ["q", "id", "x y", "ü", "utm_source"], ["1", "a b", "a+b", "a%20b", "ü", "سلام", "x=y", "", "%41", "日本"]
        urls = []
        while len(urls) < 1500:
            u = rnd.choice(["http", "https", "HTTP", "Https"]) + "://" + rnd.choice(hosts) + "".join("/" + rnd.choice(segs) for _ in range(rnd.choice([0, 1, 2, 3])))
            if rnd.random() < 0.5:
                u += "?" + "&".join(f"{rnd.choice(keys)}={rnd.choice(vals)}" for _ in range(rnd.choice([1, 2])))
            u += rnd.choice(["", "", "#top", "#/route/x", "#a b", "#ü"])
            urls.append(" " + u if rnd.random() < 0.05 else u)
        (self.dir / "urls.json").write_text(json.dumps(urls, ensure_ascii=False), encoding="utf-8")
        self.browser("canon", self.dir / "profile-canon", self.dir / "urls.json", self.dir / "canon.json")
        rows = [r for r in json.loads((self.dir / "canon.json").read_text(encoding="utf-8")) if "canon" in r]
        self.assertGreater(len(rows), 1400)
        bad = [(r["raw"], r["canon"]) for r in rows if browser_key(r["raw"]) != browser_key(r["canon"])]
        self.assertEqual(bad[:5], [])

    def test_chrome_url_ok_agrees_with_what_the_browser_keeps(self):
        keep = ["https://example.com", "HTTP://EXAMPLE.COM", "http:example.com", "https:\\\\example.com", "http:///x", "https://über.de", "http://例え.jp/",
                "http://exa mple.com/", "http://user:pw@example.com", "http://[::1]/", "http://192.168.0.1", "http://localhost", "localhost:8080/x", "ftp://h/",
                "file:///x", "about:blank", "data:,x", "javascript:void(0)", "mailto:a@b", "chrome://settings", "chrome-extension://abc/x", "ssh://h", "magnet:?xt=1",
                "view-source:https://example.com", "tel:+1", "https://example.com/" + "a" * 70000]
        drop = ["", " ", "example.com", "www.example.com/x", "192.168.0.1", "localhost", "git@github.com:a/b.git", "//example.com/x", "/relative", "?q=1", "#frag",
                "http://", "http://:80/", "http://user@/", "http://[::1", "http://example.com:99999/", "http://example.com:abc/", "http://999.999.999.999/",
                "http://exa<mple.com/", "ht tp://x.com", "http//x.com", "://x.com", "chrome://", "ftp://", "x", "über.de"]
        cands = keep + drop
        nodes = [{"id": str(10 + i), "name": f"c{i}", "type": "url", "url": u, "date_added": "13300000000000000", "date_last_used": "0",
                  "guid": f"00000000-0000-4000-8000-{i + 1:012x}"} for i, u in enumerate(cands)]
        roots = {k: {"id": str(i + 1), "name": n, "type": "folder", "guid": formats.CHROME_ROOT_GUIDS[k], "date_added": "13300000000000000",
                     "date_last_used": "0", "date_modified": "0", "children": []} for i, (k, n) in enumerate(formats.CHROME_ROOTS.items())}
        roots["other"]["children"] = nodes
        self.bookmarks.parent.mkdir(parents=True)
        self.bookmarks.write_text(json.dumps({"checksum": formats.chrome_checksum(roots), "roots": roots, "version": 1}, ensure_ascii=False), encoding="utf-8")
        survivors = {n["title"] for n in self.dump()["tree"] if n.get("url")}
        for i, u in enumerate(cands):
            self.assertEqual(f"c{i}" in survivors, u in keep, f"browser {'kept' if f'c{i}' in survivors else 'dropped'} {u[:60]!r}")
            self.assertEqual(formats.chrome_url_ok(u), u in keep, f"chrome_url_ok disagrees about {u[:60]!r}")

    def test_checksum_matters_only_to_older_browsers_but_content_always_survives(self):
        self.seed_profile({"bar": [{"title": "a", "url": "https://a.example/"}, {"title": "b", "url": "https://b.example/"}]})
        data = json.loads(self.bookmarks.read_text(encoding="utf-8"))
        want_ids = [n[1] for n in flat_file(data)]
        data["checksum"] = "0" * 32
        self.bookmarks.write_text(json.dumps(data), encoding="utf-8")
        loaded = flat_browser(self.dump()["tree"])
        self.assertEqual(sorted(n[3:5] for n in loaded if n[2] == "url"), [("a", "https://a.example/"), ("b", "https://b.example/")])
        if BROWSER["major"] and BROWSER["major"] <= 143:
            self.assertNotEqual([n[1] for n in loaded], want_ids[:len(loaded)], "Chromium <= 143 renumbers every id on a checksum mismatch")
        else:
            self.assertEqual([n[1] for n in loaded], [i for i in want_ids if i != "3"])   # newer builds ignore the checksum

    # ------------------------------------------------------- profile safety
    @unittest.skipIf(sys.platform == "win32", "SingletonLock is a POSIX symlink")
    def test_a_running_browser_is_detected_and_refused(self):
        self.seed_profile()
        self.put_bookmarks(Bookmark(url="https://new.example/", title="new", folder="Other bookmarks/X"), folders=["Other bookmarks/X"])
        self.assertFalse(sync.browser_running(self.bookmarks))
        proc = subprocess.Popen([NODE, str(DRIVER), "hold", str(self.profile)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "ready")
            self.assertTrue(sync.browser_running(self.bookmarks))
            before = self.bookmarks.read_bytes()
            with self.assertRaises(SystemExit) as refused:
                self.run_cli("sync", str(self.bookmarks), "--apply", "--confirm-chrome-closed")
            self.assertIn("running", str(refused.exception))
            with self.assertRaises(SystemExit):
                self.run_cli("install-chrome", str(self.bookmarks), "--confirm-chrome-closed")
            self.assertEqual(self.bookmarks.read_bytes(), before)
        finally:
            proc.stdin.close()
            proc.wait(timeout=60)
            proc.stdout.close()
        self.assertFalse(sync.browser_running(self.bookmarks))
        (self.profile / "SingletonLock").symlink_to("somehost-999999999")       # a crash leaves the lock behind: dead pid = not running
        self.assertFalse(sync.browser_running(self.bookmarks))

    @unittest.skipIf(sys.platform == "win32", "POSIX permissions")
    def test_sync_keeps_the_file_private_and_leaves_no_temp_file(self):
        self.seed_profile()
        os.chmod(self.bookmarks, 0o600)
        self.put_bookmarks(Bookmark(url="https://new.example/", title="new", folder="Other bookmarks/X"), folders=["Other bookmarks/X"])
        self.run_cli("sync", str(self.bookmarks), "--apply", "--confirm-chrome-closed")
        self.assertEqual(self.bookmarks.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.bookmarks.parent.glob("*.mhbm-tmp")), [])


if __name__ == "__main__":
    unittest.main()
