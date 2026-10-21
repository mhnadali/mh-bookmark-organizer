"""Opening a library written by an older release, a newer release, or several processes at once."""
import contextlib
import io
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import common  # noqa: F401
from mhbm import cli, schema
from mhbm.model import Bookmark
from mhbm.schema import SCHEMA_VERSION, SchemaTooNew
from mhbm.store import Library

FIXTURES = Path(__file__).parent / "fixtures"
OLD = ["old-library-v1", "old-library-v2-early", "old-library-v2-late"]
SCRIPTS = str(Path(__file__).resolve().parents[1] / "scripts")


def load_old(name: str, home) -> None:
    """A library file exactly as that release left it, including its full-text index (built the way that release built it)."""
    db = sqlite3.connect(Path(home) / "library.db")
    db.executescript((FIXTURES / f"{name}.sql").read_text(encoding="utf-8"))
    inline = "content" in {r[1] for r in db.execute("PRAGMA table_info(bookmarks)")}
    db.execute("CREATE VIRTUAL TABLE bm_fts USING fts5(title, url, folder, tags, notes, content, tokenize='porter unicode61')")
    text, join = ("b.content", "") if inline else ("COALESCE(p.content,'')", "LEFT JOIN pages p ON p.id=b.id")
    db.execute(f"INSERT INTO bm_fts(rowid,title,url,folder,tags,notes,content) SELECT b.rowid,b.title,b.url,b.folder,b.tags,b.notes,{text} "
               f"FROM bookmarks b {join}")
    db.commit()
    db.close()


def pragma(path, name="user_version"):
    db = sqlite3.connect(path)
    try:
        return db.execute(f"PRAGMA {name}").fetchone()[0]
    finally:
        db.close()


def columns(lib, table):
    return {r[1] for r in lib.db.execute(f"PRAGMA table_info({table})")}


class OldLibraries(unittest.TestCase):
    def test_every_old_layout_opens_with_all_its_data(self):
        for name in OLD:
            with self.subTest(name), tempfile.TemporaryDirectory() as d:
                load_old(name, d)
                started = pragma(Path(d) / "library.db")
                with Library(d) as lib:
                    self.assertEqual(lib.upgraded_from, started or 1)
                    self.assertEqual(lib.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                    self.assertEqual(lib.health()["integrity"], "ok")
                    self.assertEqual(lib.count(), 7)
                    b = lib.get("b0")
                    self.assertEqual((b.title, b.folder, b.tags, b.added), ("Operators guide", "Dev/Kubernetes", ["k8s", "ops"], 1_600_000_000))
                    self.assertEqual(lib.get("b4").folder, "" if name == "old-library-v1" else "Dev/Python")
                    self.assertTrue({"fail_count", "first_failed", "wayback", "meta", "state", "favorite"} <= columns(lib, "bookmarks"))
                    self.assertNotIn("content", columns(lib, "bookmarks"))
                    self.assertIn("folders", columns(lib, "ops"))
                    for table in schema.TABLES:
                        lib.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                    # page text moved to `pages` and is searchable, in both scripts
                    self.assertEqual([x.id for x, _ in lib.search("reconcile")], ["b0"])
                    self.assertEqual([x.id for x, _ in lib.search("coroutines")], ["b2"])
                    self.assertEqual([x.id for x, _ in lib.search("فارسی")], ["b6"])
                    self.assertEqual(lib.db.execute("SELECT content FROM pages WHERE id='b3'").fetchone()[0], "flour water salt and patience")

    def test_a_copy_of_the_old_file_is_kept_before_upgrading(self):
        for name in OLD:
            with self.subTest(name), tempfile.TemporaryDirectory() as d:
                load_old(name, d)
                before = pragma(Path(d) / "library.db")
                with Library(d):
                    pass
                copies = sorted((Path(d) / "backups").glob("library-schema*.db"))
                self.assertEqual(len(copies), 1, copies)
                old = sqlite3.connect(copies[0])
                self.assertEqual(old.execute("PRAGMA user_version").fetchone()[0], before)      # untouched: still the old layout...
                self.assertEqual(old.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0], 7)  # ...with all of the data
                self.assertEqual(old.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                old.close()

    def test_history_survives_and_undo_still_works(self):
        for name in ("old-library-v2-early", "old-library-v2-late"):
            with self.subTest(name), tempfile.TemporaryDirectory() as d:
                load_old(name, d)
                with Library(d) as lib:
                    self.assertEqual([o["command"] for o in lib.history()], ["organize", "import"])
                    self.assertEqual(lib.get("b4").folder, "Dev/Python")
                    r = lib.undo()
                    self.assertEqual(r["command"], "organize")
                    self.assertEqual(lib.get("b4").folder, "")                  # the file move made by the old release is undone

    def test_an_upgraded_library_is_fully_usable(self):
        for name in OLD:
            with self.subTest(name), tempfile.TemporaryDirectory() as d:
                load_old(name, d)
                with Library(d) as lib:
                    with lib.op("new work"):
                        lib.upsert([Bookmark(id="n1", url="https://example.com/new", title="Brand new", folder="Dev/Kubernetes")])
                    lib.record_suggestions([{"bm_id": "b5", "norm": "x", "folder": "Dev/Python", "source": "learned", "confidence": 0.9}])
                    lib.add_feedback(bm_id="b5", norm="x", host="example.net", suggested="Dev/Python", final="Dev/Python", action="accepted",
                                     confidence=0.9, channel="review", explicit=True)
                    self.assertEqual(lib.count(), 8)
                    self.assertEqual([x.id for x, _ in lib.search("brand")], ["n1"])
                    self.assertEqual(len(lib.feedback_rows()), 1)
                    lib.undo()
                    self.assertEqual(lib.count(), 7)

    def test_reopening_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            load_old("old-library-v2-late", d)
            with Library(d):
                pass
            with Library(d) as lib:
                self.assertEqual(lib.upgraded_from, SCHEMA_VERSION)
            self.assertEqual(len(list((Path(d) / "backups").glob("library-schema*.db"))), 1)    # no second copy, no second upgrade

    def test_a_failing_step_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            load_old("old-library-v2-early", d)

            def broken(db):
                schema._add_missing_columns(db)
                db.execute("INSERT INTO bookmarks(id, url) VALUES('half-done', 'x')")
                raise RuntimeError("step failed halfway")

            with mock.patch.dict(schema.STEPS, {2: broken}):
                with self.assertRaises(RuntimeError):
                    Library(d)
            db = sqlite3.connect(Path(d) / "library.db")
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)                # still the old layout...
            self.assertNotIn("favorite", {r[1] for r in db.execute("PRAGMA table_info(bookmarks)")})
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0], 7)      # ...and no half-done rows
            db.close()
            with Library(d) as lib:                                                             # the next open (with the bug fixed) succeeds
                self.assertEqual((lib.count(), lib.upgraded_from), (7, 2))


class Future(unittest.TestCase):
    def make(self, d, version):
        with Library(d) as lib:
            lib.upsert([Bookmark(id="a", url="https://example.com/a")])
        db = sqlite3.connect(Path(d) / "library.db")
        db.execute(f"PRAGMA user_version = {version}")
        db.commit()
        db.close()

    def test_a_newer_library_is_refused_and_left_alone(self):
        with tempfile.TemporaryDirectory() as d:
            self.make(d, SCHEMA_VERSION + 5)
            with self.assertRaises(SchemaTooNew) as cm:
                Library(d)
            self.assertIn("newer", str(cm.exception))
            db = sqlite3.connect(Path(d) / "library.db")
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION + 5)   # not downgraded
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0], 1)
            db.close()
            self.assertFalse((Path(d) / "backups").exists())

    def test_command_line_reports_it_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as d:
            self.make(d, SCHEMA_VERSION + 1)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = cli.main(["--home", d, "stats"])
            self.assertEqual(rc, 2)
            self.assertIn("newer", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())


class Drift(unittest.TestCase):
    def test_a_missing_table_is_recreated(self):
        with tempfile.TemporaryDirectory() as d:
            with Library(d) as lib:
                lib.upsert([Bookmark(id="a", url="https://example.com/a")])
                lib.db.execute("DROP TABLE feedback")
                lib.db.commit()
            with Library(d) as lib:
                self.assertEqual(lib.feedback_rows(), [])
                self.assertEqual(lib.count(), 1)

    def test_a_missing_column_is_added(self):
        with tempfile.TemporaryDirectory() as d:
            load_old("old-library-v2-late", d)
            with Library(d):
                pass
            db = sqlite3.connect(Path(d) / "library.db")
            try:
                db.execute("ALTER TABLE ops DROP COLUMN folders")       # needs SQLite 3.35; older builds keep the column, and the test is moot
                db.commit()
            except sqlite3.OperationalError:
                self.skipTest("SQLite too old to drop a column")
            finally:
                db.close()
            with Library(d) as lib:
                self.assertIn("folders", columns(lib, "ops"))


class Fresh(unittest.TestCase):
    def test_new_library_is_stamped_and_needs_no_backup(self):
        with tempfile.TemporaryDirectory() as d, Library(d) as lib:
            self.assertEqual(lib.upgraded_from, 0)
            self.assertEqual(lib.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertFalse((Path(d) / "backups").exists())

    def test_every_version_below_current_has_a_step(self):
        self.assertEqual(set(schema.STEPS), set(range(1, SCHEMA_VERSION)))

    def test_two_processes_opening_an_old_library_at_once(self):
        with tempfile.TemporaryDirectory() as d:
            load_old("old-library-v1", d)
            code = f"import sys; sys.path.insert(0, {SCRIPTS!r}); from mhbm.store import Library; l = Library(sys.argv[1]); print(l.count(), l.upgraded_from)"
            procs = [subprocess.Popen([sys.executable, "-I", "-c", code, d], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
            results = [p.communicate(timeout=120) + (p.returncode,) for p in procs]
            for out, err, rc in results:
                self.assertEqual((rc, err), (0, ""), err)
                self.assertEqual(out.split()[0], "7")
            self.assertEqual(sorted(o.split()[1] for o, _, _ in results).count("1") >= 1, True)      # at least one did the upgrade, the rest found it done
            with Library(d) as lib:
                self.assertEqual(lib.health()["integrity"], "ok")
                self.assertEqual(lib.db.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 5)       # page text moved exactly once
                self.assertEqual([x.id for x, _ in lib.search("reconcile")], ["b0"])


if __name__ == "__main__":
    unittest.main()
