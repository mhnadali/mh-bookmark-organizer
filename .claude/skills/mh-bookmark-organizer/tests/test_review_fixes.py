"""Regression tests for the defects the Phase 3 adversarial review confirmed (each was reproduced before it was fixed).

The MCP ones are in test_mcp.py (class ReviewFindings); these cover the library, the browser-profile code, the rules and the network guard.
"""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import common  # noqa: F401
from mhbm import browsers, formats, netsafe, profilesafe, rules, schema, sync
from mhbm.model import Bookmark
from mhbm.store import Library, StaleRead
from test_cli import CliCase
from test_core import NETSCAPE
from test_profiles import bookmarks_json


def seed(lib, n=6):
    lib.upsert([Bookmark(id=f"b{i}", url=f"https://example.com/{i}", title=f"word{i}", folder="Old") for i in range(n)])
    lib.add_folders(["Old"])


class OperationsAreWhole(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_a_busy_database_does_not_leave_the_library_inside_a_phantom_operation(self):
        with mock.patch.dict(os.environ, {"MHBM_BUSY_TIMEOUT_MS": "150"}), Library(self.dir) as lib:
            seed(lib)
            blocker = sqlite3.connect(self.dir / "library.db", isolation_level=None)
            blocker.execute("BEGIN IMMEDIATE")
            try:
                with self.assertRaises(sqlite3.OperationalError), lib.op("blocked"):
                    pass
            finally:
                blocker.execute("ROLLBACK")
                blocker.close()
            self.assertIsNone(lib._op)                       # before the fix this stayed {} and every later write skipped the journal
            with lib.op("after"):
                lib.upsert([Bookmark(id="b0", url="https://example.com/0", title="renamed", folder="Old")])
            self.assertEqual([o["command"] for o in lib.history()], ["after"])

    def test_undo_is_one_transaction(self):
        with Library(self.dir) as lib:
            seed(lib)
            with lib.op("delete some"):
                lib.delete(["b0", "b1"])
            with mock.patch.object(Library, "_reindex", side_effect=RuntimeError("interrupted")), self.assertRaises(RuntimeError):
                lib.undo()
            self.assertEqual(lib.count(), 4)                                     # nothing was restored ...
            self.assertEqual(lib.history()[0]["undone"], 0)                      # ... and the operation is not marked undone
            self.assertEqual(lib.undo()["restored"], 2)                          # and it can still be undone properly
            self.assertEqual(lib.count(), 6)

    def test_set_folders_never_leaves_the_list_empty(self):
        with Library(self.dir) as lib:
            lib.add_folders(["A", "B"])
            lib.db.execute("CREATE TEMP TRIGGER no_inserts BEFORE INSERT ON folders BEGIN SELECT RAISE(ABORT, 'boom'); END")
            with self.assertRaises(sqlite3.DatabaseError):
                lib.set_folders(["C"])
            lib.db.execute("DROP TRIGGER no_inserts")
            self.assertEqual(lib.folders(), {"A", "B"})

    def test_the_folder_list_is_journaled_from_inside_the_write_lock(self):
        """It was read before BEGIN IMMEDIATE: a concurrent writer's folders could be wiped when the operation was undone."""
        with Library(self.dir) as lib:
            seed(lib)
            seen = []
            real = lib.folders

            def spy():
                seen.append(lib.db.in_transaction)
                return real()
            with mock.patch.object(lib, "folders", spy), lib.op("x"):
                lib.add_folders(["New"])
            self.assertTrue(seen and all(seen), seen)


class Restoring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_an_export_file_is_not_a_snapshot(self):
        with Library(self.dir / "lib") as lib:
            seed(lib)
            export = self.dir / "exp.json"
            export.write_text(formats.render(lib.all(), "json", lib.folders()))
            with self.assertRaises(ValueError) as cm:
                lib.restore(export)
            self.assertIn("not a library snapshot", str(cm.exception))
            self.assertEqual(lib.count(), 6)                                    # nothing happened
            self.assertEqual(lib.get("b0").title, "word0")

    def test_damaged_snapshots_change_nothing(self):
        with Library(self.dir / "lib") as lib:
            seed(lib)
            good = json.loads(lib.snapshot("x").read_text())
            for name, doc in (("not json", "{oops"), ("empty list", "[]x"), ("no ids", {"bookmarks": [{"url": "https://a.example/"}]}),
                              ("text for a number", {"bookmarks": [dict(good["bookmarks"][0], added="2023-01-01")], "pages": {}, "folders": []}),
                              ("pages not text", {"bookmarks": good["bookmarks"], "pages": {"b0": 5}, "folders": []}),
                              ("folders not text", {"bookmarks": good["bookmarks"], "pages": {}, "folders": [1]})):
                with self.subTest(name):
                    path = self.dir / "bad.json"
                    path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
                    with self.assertRaises(ValueError):
                        lib.restore(path)
                    self.assertEqual(lib.count(), 6)

    def test_restore_is_one_transaction(self):
        with Library(self.dir / "lib") as lib:
            seed(lib)
            snap = lib.snapshot("keep")
            lib.delete(["b0"])
            with mock.patch.object(Library, "set_folders", side_effect=RuntimeError("interrupted")), self.assertRaises(RuntimeError):
                lib.restore(snap)
            self.assertEqual(lib.count(), 5)                                    # still the library before the restore, not a half-restored one
            self.assertEqual(lib.restore(snap), 6)
            self.assertEqual(lib.count(), 6)


class ReadsAndWrites(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_a_write_based_on_a_stale_read_is_refused(self):
        with Library(self.dir) as a, Library(self.dir) as b:
            seed(a)
            a.all()
            a.ensure_unchanged_since_read()                                     # nothing happened since
            with b.op("other process"):
                b.upsert([Bookmark(id="b0", url="https://example.com/0", title="changed elsewhere", folder="Old", tags=["kept"])])
            with self.assertRaises(StaleRead):
                a.ensure_unchanged_since_read()
            a.all()                                                             # a new reading
            a.ensure_unchanged_since_read()

    def test_the_fingerprint_notices_undo_link_checks_and_the_archive(self):
        with Library(self.dir) as lib:
            seed(lib)
            with lib.op("one"):
                lib.delete(["b0"])
            fp = lib.fingerprint()
            lib.undo()
            self.assertNotEqual(lib.fingerprint(), fp)                          # undo adds no journal row
            fp = lib.fingerprint()
            lib.db.execute("UPDATE bookmarks SET status='dead', http_code=404, last_checked=? WHERE id='b1'", (int(time.time()),))
            lib.db.commit()
            self.assertNotEqual(lib.fingerprint(), fp)
            fp = lib.fingerprint()
            lib.db.execute("INSERT INTO pages(id, content) VALUES('b2', 'text')")
            lib.db.commit()
            self.assertNotEqual(lib.fingerprint(), fp)
            self.assertEqual(lib.fingerprint(), lib.fingerprint())              # and is stable otherwise

    def test_read_only_works_in_a_folder_whose_name_has_uri_characters(self):
        names = ("my #1 marks", "50%20off")
        if os.name != "nt":
            names += ("a?b",)
        for name in names:
            with self.subTest(name):
                home = self.dir / name
                with Library(home) as lib:
                    seed(lib)
                before = sorted(p.name for p in self.dir.iterdir())
                with Library(home, read_only=True) as ro:
                    self.assertEqual(ro.count(), 6)
                self.assertEqual(sorted(p.name for p in self.dir.iterdir()), before)     # no stray file named after the part before '#' or '?'
        self.assertTrue(schema.ro_uri(self.dir / "x#y").endswith("x%23y?mode=ro"))


class PlanApplication(CliCase):
    def test_apply_plan_refuses_when_another_process_changed_the_library_meanwhile(self):
        self.run_cli("import", self.write("in.html", NETSCAPE))
        plan = self.dir / "plan.json"
        lib = self.lib()
        victim = lib.all()[0]
        plan.write_text(json.dumps({victim.id: {"folder": "Elsewhere", "tags": ["planned"]}}))
        original_all = Library.all

        def all_then_another_process_writes(self_, *a, **kw):
            out = original_all(self_, *a, **kw)
            with Library(self.home) as other:                                   # the web UI, a scheduled job ...
                with other.op("elsewhere"):
                    other.upsert([Bookmark(id=victim.id, url=victim.url, title="NEW TITLE", folder=victim.folder, tags=["theirs"])])
            return out
        with mock.patch.object(Library, "all", all_then_another_process_writes):
            self.run_cli("apply-plan", str(plan), "--apply", expect=2)
        b = self.lib().get(victim.id)
        self.assertEqual((b.title, b.tags), ("NEW TITLE", ["theirs"]))          # their change survived; ours was refused, not merged blindly


class SyncFixes(CliCase):
    def chrome(self):
        lib = self.lib()
        path = self.dir / "Bookmarks"
        path.write_text(formats.write_chrome(lib.all(), sorted(lib.folders())), encoding="utf-8")
        return path

    def test_the_baseline_is_what_the_plan_saw_not_a_re_read_of_the_file(self):
        self.run_cli("import", self.write("in.html", NETSCAPE))
        target = self.chrome()
        self.run_cli("sync", str(target), "--apply")                                   # first sync: both sides equal, baseline recorded
        late = {"id": "9999", "type": "url", "name": "Late", "url": "https://late.example.org/", "date_added": "13200000000000000"}

        def browser_saves_meanwhile(self_, label):
            data = json.loads(target.read_text())
            data["roots"]["bookmark_bar"]["children"].append(late)
            data["checksum"] = formats.chrome_checksum(data["roots"])
            target.write_text(json.dumps(data))
            return Path(self_.root) / "backups" / "x.json"
        with mock.patch.object(Library, "snapshot", browser_saves_meanwhile):
            self.run_cli("sync", str(target), "--apply")                               # a sync that changes nothing in the browser file
        report = self.js("sync", str(target))                                           # the next sync must see 'Late' as NEW in the browser
        self.assertEqual((report["add_to_library"], report["delete_from_browser"]), (1, 0))
        self.run_cli("sync", str(target), "--apply")
        self.assertIn("https://late.example.org/", [b.url for b in self.lib().all()])
        self.assertIn("https://late.example.org/", [b.url for b in formats.parse_chrome(json.loads(target.read_text()))])

    def test_bookmarks_the_browser_cannot_tell_apart_stay_in_the_library_and_are_not_resurrected(self):
        a = Bookmark(id="1", url="https://a.example.com/x", title="A one", folder="Bookmarks bar/Work")
        b = Bookmark(id="2", url="http://a.example.com/x/", title="A two", folder="Bookmarks bar/Work")
        c = Bookmark(id="3", url="https://other.example.com/", title="Other", folder="Bookmarks bar/Work")
        plan = sync.plan_sync([a, b, c], [], {})
        self.assertEqual(len(plan.add_remote), 2)                                       # one for the pair, one for the other
        self.assertEqual([x.id for x in plan.collapsed], ["2"])
        self.assertEqual(plan.summary()["kept_local_same_address"], 1)
        key = sync.key_of(a.url, a.folder)
        baseline = {key: "A one", sync.key_of(c.url, c.folder): "Other"}
        plan = sync.plan_sync([a, b, c], [c], baseline)                                 # the pair's browser node was deleted in the browser
        self.assertEqual([x.id for x in plan.delete_local], ["1"])
        self.assertEqual([x.id for x in plan.collapsed], ["2"])                         # the twin is neither deleted nor pushed back
        self.assertEqual(plan.add_remote, [])

    def test_a_folder_too_deep_for_chromium_is_refused_before_anything_is_written(self):
        deep = "Bookmarks bar/" + "/".join(f"f{i}" for i in range(100))
        data = json.loads(formats.write_chrome([Bookmark(id="1", url="https://a.example/", title="a", folder="Bookmarks bar")], []))
        plan = sync.SyncPlan(add_remote=[Bookmark(id="2", url="https://b.example/", title="b", folder=deep)])
        with self.assertRaises(sync.SyncError) as cm:
            sync.patch_chrome(data, plan)
        self.assertIn("nested more than", str(cm.exception))


class ExportOverAProfile(CliCase):
    def test_export_refuses_a_browser_profile_file_and_other_files_are_written_atomically(self):
        self.run_cli("import", self.write("in.html", NETSCAPE))
        profile = self.dir / "Chrome" / "Default"
        profile.mkdir(parents=True)
        (self.dir / "Chrome" / "Local State").write_text("{}")
        target = profile / "Bookmarks"
        target.write_text(bookmarks_json())
        before = target.read_text()
        with self.assertRaises(SystemExit) as cm:
            self.run_cli("export", "--format", "chrome", "--out", str(target))
        self.assertIn("install-chrome", str(cm.exception))
        self.assertEqual(target.read_text(), before)
        elsewhere = self.dir / "mine.html"
        self.run_cli("export", "--format", "html", "--out", str(elsewhere))
        self.assertIn("<A HREF", elsewhere.read_text())
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.endswith(profilesafe.TMP_SUFFIX)], [])


class MalformedBrowserFiles(CliCase):
    def test_verify_reports_instead_of_crashing(self):
        for name, children in (("null children", None), ("string child", ["x"]), ("number child", [5])):
            with self.subTest(name):
                doc = {"roots": {"bookmark_bar": {"id": "1", "type": "folder", "name": "Bookmarks bar", "children": children},
                                 "other": {"id": "2", "type": "folder", "name": "Other bookmarks", "children": []},
                                 "synced": {"id": "3", "type": "folder", "name": "Mobile bookmarks", "children": []}}, "version": 1}
                problems = formats.verify_chrome(doc)
                self.assertTrue(problems, name)

    def test_absurdly_deep_json_is_a_message_not_a_traceback(self):
        path = self.dir / "deep.json"
        path.write_text("[" * 200_000)
        self.run_cli("verify-chrome", str(path), expect=2)

    def test_restoring_an_empty_backup_is_refused(self):
        prof = self.dir / "P"
        prof.mkdir()
        target = prof / "Bookmarks"
        target.write_text(bookmarks_json())
        backup = profilesafe.backup_file(target)
        backup.write_text("")
        with self.assertRaises(profilesafe.ProfileError):
            profilesafe.restore_backup(target, backup)
        self.assertEqual(target.read_text(), bookmarks_json())


class WindowsUsersOnWsl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mnt = Path(self.tmp.name) / "mnt"
        for user in ("alice", "me", "zed"):
            p = self.mnt / "c/Users" / user / "AppData/Local/Google/Chrome/User Data/Default/Bookmarks"
            p.parent.mkdir(parents=True)
            p.write_text(bookmarks_json())

    def test_a_folder_that_cannot_be_entered_does_not_hide_the_other_users(self):
        real = Path.is_dir

        def is_dir(self_, *a, **kw):
            if self_.parts[-2:] == ("alice", "AppData"):
                raise PermissionError(13, "denied")
            return real(self_, *a, **kw)
        with mock.patch.object(browsers, "WSL_MOUNT_ROOT", self.mnt), mock.patch.object(browsers, "_is_wsl", lambda: True), \
                mock.patch.object(Path, "is_dir", is_dir), mock.patch.dict(os.environ, {"USER": "nobody"}):
            homes = browsers._wsl_windows_homes()
        self.assertEqual([h[0].parts[-3] for h in homes], ["me", "zed"])

    def test_the_current_users_profile_comes_first(self):
        with mock.patch.object(browsers, "WSL_MOUNT_ROOT", self.mnt), mock.patch.object(browsers, "_is_wsl", lambda: True), \
                mock.patch.dict(os.environ, {"USER": "me", "HOME": str(Path(self.tmp.name) / "emptyhome")}, clear=False):
            homes = browsers._wsl_windows_homes()
        self.assertEqual([h[0].parts[-3] for h in homes], ["me", "alice", "zed"])


class RulesCannotHang(unittest.TestCase):
    EVIL = ["(a|aa)+$", "(?:a|a)*b", "(a?){30}a{30}", "(a{1,30}){1,30}b", r"\w*\w*\w*\w*\w*\w*x", "(?x)(a+) +$", "(a+)+$", r"(\w+\s?)*$"]
    FINE = [r"^\[Docs\]", "k8s|kubernetes", r"\bGPT-?\d+\b", r"(?:foo|bar)\s+\d+", "^(?:a|b)+x", r"(\d{3}-){2}\d{4}", r".*foo.*bar.*baz", "(?s)a.b"]

    def test_the_screen(self):
        for pattern in self.EVIL:
            with self.subTest(pattern):
                self.assertTrue(rules.regex_problem(pattern), pattern)
        for pattern in self.FINE:
            with self.subTest(pattern):
                self.assertEqual(rules.regex_problem(pattern), "", pattern)

    @unittest.skipUnless(hasattr(__import__("signal"), "setitimer"), "needs POSIX timers")
    def test_whatever_slips_through_the_screen_is_stopped_at_match_time(self):
        slow = {"id": 99, "when": {"title_regex": "(x+x+)+y"}, "folder": "F", "tags": []}       # bypass validate on purpose: a hand-edited rules.json
        title = "x" * 60
        t0 = time.time()
        with mock.patch.object(rules.sys, "stderr", mock.MagicMock()):
            self.assertFalse(rules.matches(Bookmark(url="https://e.example/", title=title), slow))
            self.assertFalse(rules.matches(Bookmark(url="https://e.example/", title=title), slow))   # skipped the second time: no second wait
        self.assertLess(time.time() - t0, rules.MATCH_SECONDS * 3 + 1)
        self.assertIn(99, rules._slow_rules)
        rules._slow_rules.discard(99)


class PercentEscapedHosts(unittest.TestCase):
    def test_escapes_in_the_host_are_refused_whatever_the_route(self):
        for url in ("http://%31%32%37.0.0.1/x", "http://127.0.0.%31/x", "http://%31%36%39.254.169.254/latest", "https://exa%6dple.com/"):
            with self.subTest(url), self.assertRaises(netsafe.Blocked):
                netsafe.assert_public(url)
        with netsafe.testing_allow_private():
            netsafe.assert_public("http://127.0.0.1/")                                  # the test seam still works


if __name__ == "__main__":
    unittest.main()
