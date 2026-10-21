import json
import unittest
from pathlib import Path

import common  # noqa: F401
from mhbm import formats, sync
from mhbm.model import Bookmark
from test_cli import CliCase


def chrome_file(path: Path, bms, folders=()):
    path.write_text(formats.write_chrome(bms, folders), encoding="utf-8")
    return path


def urls(path):
    return sorted((b.url, b.folder) for b in formats.parse_chrome(json.loads(Path(path).read_text())))


class Plan(unittest.TestCase):
    def bm(self, url, folder="Bookmarks bar", title=None):
        return Bookmark(url=url, folder=folder, title=title if title is not None else url)

    def test_first_sync_is_a_union_never_a_deletion(self):
        plan = sync.plan_sync([self.bm("https://a.com"), self.bm("https://only-local.com")],
                              [self.bm("https://a.com"), self.bm("https://only-remote.com")], {})
        self.assertEqual(plan.summary(), {"add_to_library": 1, "delete_from_library": 0, "add_to_browser": 1, "delete_from_browser": 0,
                                          "retitle_library": 0, "retitle_browser": 0, "unchanged": 1})

    def test_deletions_are_recognised_with_a_baseline(self):
        base = {sync.key_of(u, "Bookmarks bar"): u for u in ("https://gone-remote.com", "https://gone-local.com", "https://keep.com")}
        local = [self.bm("https://gone-remote.com"), self.bm("https://keep.com")]
        remote = [self.bm("https://gone-local.com"), self.bm("https://keep.com")]
        plan = sync.plan_sync(local, remote, base)
        self.assertEqual([b.url for b in plan.delete_local], ["https://gone-remote.com"])
        self.assertEqual(plan.delete_remote, [sync.key_of("https://gone-local.com", "Bookmarks bar")])
        self.assertEqual((plan.add_local, plan.add_remote), ([], []))

    def test_url_normalisation_makes_http_www_the_same_bookmark(self):
        plan = sync.plan_sync([self.bm("http://www.a.com/x/", title="T")], [self.bm("https://a.com/x", title="T")], {})
        self.assertEqual((plan.unchanged, plan.remote_changes, plan.local_changes), (1, 0, 0))

    def test_rename_conflicts_follow_the_baseline_and_preference(self):
        key = sync.key_of("https://a.com", "F")
        local, remote = [self.bm("https://a.com", "F", "Local title")], [self.bm("https://a.com", "F", "Remote title")]
        p = sync.plan_sync(local, remote, {key: "Old"})
        self.assertEqual([t for _, t in p.retitle_local], ["Remote title"])
        p = sync.plan_sync(local, remote, {key: "Old"}, prefer="local")
        self.assertEqual(p.retitle_remote, [(key, "Local title")])
        p = sync.plan_sync(local, [self.bm("https://a.com", "F", "Old")], {key: "Old"})   # only local changed
        self.assertEqual(p.retitle_remote, [(key, "Local title")])

    def test_mass_deletion_is_refused_unless_forced(self):
        base = {sync.key_of(f"https://x{i}.com", "F"): "t" for i in range(40)}
        local = []                                                       # everything vanished locally
        remote = [self.bm(f"https://x{i}.com", "F") for i in range(40)]
        with self.assertRaises(sync.SyncError):
            sync.plan_sync(local, remote, base)
        self.assertEqual(len(sync.plan_sync(local, remote, base, force=True).delete_remote), 40)


class Patching(unittest.TestCase):
    def test_in_place_patch_preserves_everything_else(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = chrome_file(Path(d, "Bookmarks"), [Bookmark(url="https://keep.com", title="Keep", folder="Bookmarks bar/Dev", id="1"),
                                                      Bookmark(url="https://drop.com", title="Drop", folder="Bookmarks bar/Dev", id="2")])
            data, _remote = sync.read_remote(path)
            data["roots"]["bookmark_bar"]["children"][0]["children"][0]["meta_info"] = {"custom": "kept"}   # browser-specific data
            keep_guid = data["roots"]["bookmark_bar"]["children"][0]["children"][0]["guid"]
            plan = sync.SyncPlan(add_remote=[Bookmark(url="https://new.com", title="New", folder="Bookmarks bar/Fresh/Sub", added=1600000000)],
                                 delete_remote=[sync.key_of("https://drop.com", "Bookmarks bar/Dev")])
            out = sync.patch_chrome(data, plan)
            self.assertEqual(formats.verify_chrome(out), [])
            self.assertEqual(urls_from(out), [("https://keep.com", "Bookmarks bar/Dev"), ("https://new.com", "Bookmarks bar/Fresh/Sub")])
            kept = out["roots"]["bookmark_bar"]["children"][0]["children"][0]
            self.assertEqual((kept["guid"], kept["meta_info"]), (keep_guid, {"custom": "kept"}))
            ids = []

            def walk(n):
                ids.append(n["id"])
                for c in n.get("children", []):
                    walk(c)
            for r in out["roots"].values():
                walk(r)
            self.assertEqual(len(ids), len(set(ids)))


def urls_from(data):
    return sorted((b.url, b.folder) for b in formats.parse_chrome(data))


class Cli(CliCase):
    def test_full_two_way_sync_converges_and_is_idempotent(self):
        profile = chrome_file(self.dir / "Bookmarks", [
            Bookmark(url="https://shared.com", title="Shared", folder="Bookmarks bar/Dev"),
            Bookmark(url="https://remote-del.com", title="RD", folder="Bookmarks bar/Dev"),
            Bookmark(url="https://local-del.com", title="LD", folder="Bookmarks bar/Dev")])
        self.run_cli("import-browser", "x", "--profile", str(profile))
        self.run_cli("sync", str(profile), "--apply", "--confirm-chrome-closed")           # baseline
        # changes on both sides
        lib = self.lib()
        by_url = {b.url: b for b in lib.all()}
        lib.delete([by_url["https://local-del.com"].id])
        lib.upsert([Bookmark(url="https://local-new.com", title="LN", folder="Bookmarks bar/Dev", id="ln")])
        lib.close()
        data = json.loads(profile.read_text())
        dev = data["roots"]["bookmark_bar"]["children"][0]["children"]
        dev[:] = [n for n in dev if n["url"] != "https://remote-del.com"] + [
            {"id": "900", "name": "RN", "type": "url", "url": "https://remote-new.com", "guid": "11111111-1111-4111-8111-111111111111"}]
        data["checksum"] = formats.chrome_checksum(data["roots"])
        profile.write_text(json.dumps(data))
        r = self.js("sync", str(profile))                                                  # dry run
        self.assertEqual((r["add_to_library"], r["delete_from_library"], r["add_to_browser"], r["delete_from_browser"]), (1, 1, 1, 1))
        before = profile.read_text()
        with self.assertRaises(SystemExit):
            self.run_cli("sync", str(profile), "--apply")                                  # browser file would change: needs the flag
        self.assertEqual(profile.read_text(), before)
        self.run_cli("sync", str(profile), "--apply", "--confirm-chrome-closed")
        want = [("https://local-new.com", "Bookmarks bar/Dev"), ("https://remote-new.com", "Bookmarks bar/Dev"), ("https://shared.com", "Bookmarks bar/Dev")]
        self.assertEqual(urls(profile), want)
        self.assertEqual(sorted((b.url, b.folder) for b in self.lib().all()), want)
        self.assertEqual(formats.verify_chrome(json.loads(profile.read_text())), [])
        self.assertTrue(list(self.dir.glob("Bookmarks.mhbm-backup-*")))
        again = self.js("sync", str(profile))
        self.assertEqual({k: v for k, v in again.items() if k.startswith(("add_", "delete_", "retitle_"))}, {k: 0 for k in again if k.startswith(("add_", "delete_", "retitle_"))})
        self.run_cli("undo")                                                               # the library side of the last sync is undoable
        self.assertNotIn("https://remote-new.com", [b.url for b in self.lib().all()])

    def test_status_and_bad_target(self):
        profile = chrome_file(self.dir / "Bookmarks", [Bookmark(url="https://a.com", title="A", folder="Bookmarks bar")])
        self.run_cli("import-browser", "x", "--profile", str(profile))
        self.assertIsNone(self.js("sync", str(profile), "--status")["sync"])
        self.run_cli("sync", str(profile), "--apply")                                      # nothing to write to the browser: no flag needed
        self.assertIsNotNone(self.js("sync", str(profile), "--status")["sync"])
        with self.assertRaises(SystemExit):
            self.run_cli("sync", str(self.dir / "missing"))


if __name__ == "__main__":
    unittest.main()
