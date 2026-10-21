"""An operation is one transaction: it happens completely or not at all, whatever interrupts it."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

import common  # noqa: F401
from mhbm.model import Bookmark
from mhbm.store import Library

SCRIPTS = str(Path(__file__).resolve().parents[1] / "scripts")


def seed(lib, n=30):
    lib.upsert([Bookmark(id=f"b{i}", url=f"https://example.com/{i}", title=f"word{i} common", folder="Old", tags=["t"]) for i in range(n)])
    lib.add_folders(["Old"])


def snapshot(lib):
    rows = [(b.id, b.url, b.title, b.folder, tuple(b.tags)) for b in lib.all(snippet=0)]
    return (sorted(rows), sorted(lib.folders()), lib.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0],
            lib.db.execute("SELECT COUNT(*) FROM journal").fetchone()[0], sorted(r["rowid"] for r in lib.db.execute("SELECT rowid FROM bm_fts")),
            lib.db.execute("SELECT COUNT(*) FROM feedback").fetchone()[0])


class Rollback(unittest.TestCase):
    def test_an_exception_leaves_no_trace(self):
        with tempfile.TemporaryDirectory() as d, Library(d) as lib:
            seed(lib)
            before = snapshot(lib)
            with self.assertRaises(RuntimeError), lib.op("doomed"):
                lib.upsert([Bookmark(id="new", url="https://example.com/new", title="fresh word", folder="New")])
                lib.delete(["b0", "b1", "b2"])
                lib.add_folders(["New", "New/Sub"])
                lib.record_suggestions([{"bm_id": "b5", "norm": "x", "folder": "Old", "source": "learned"}])
                lib.add_feedback(bm_id="b5", norm="x", host="example.com", suggested="Old", final="New", action="corrected", channel="review",
                                 explicit=True)
                raise RuntimeError("boom")
            self.assertEqual(snapshot(lib), before)                     # bookmarks, folders, index, journal, feedback: all untouched
            self.assertEqual(lib.search("fresh"), [])
            self.assertEqual(len(lib.search("word0")), 1)
            self.assertFalse(lib.db.in_transaction)
            with lib.op("fine afterwards"):                             # and the library is still usable
                lib.upsert([Bookmark(id="b0", url="https://example.com/0", title="renamed", folder="Old")])
            self.assertEqual(lib.get("b0").title, "renamed")
            self.assertEqual(len(lib.history()), 1)

    def test_ctrl_c_inside_an_operation_rolls_it_back_too(self):
        with tempfile.TemporaryDirectory() as d, Library(d) as lib:
            seed(lib)
            before = snapshot(lib)
            with self.assertRaises(KeyboardInterrupt), lib.op("interrupted"):
                lib.delete([f"b{i}" for i in range(10)])
                raise KeyboardInterrupt
            self.assertEqual(snapshot(lib), before)

    def test_a_successful_operation_commits_changes_and_journal_together(self):
        with tempfile.TemporaryDirectory() as d:
            with Library(d) as lib:
                seed(lib)
                with lib.op("rename"):
                    lib.upsert([Bookmark(id="b1", url="https://example.com/1", title="changed", folder="Old")])
                    self.assertTrue(lib.db.in_transaction)              # nothing is committed until the block ends
                self.assertFalse(lib.db.in_transaction)
            with Library(d) as lib:                                      # a fresh connection sees both
                self.assertEqual(lib.get("b1").title, "changed")
                self.assertEqual([o["command"] for o in lib.history()], ["rename"])
                lib.undo()
                self.assertEqual(lib.get("b1").title, "word1 common")

    def test_nested_operations_share_the_outer_transaction(self):
        with tempfile.TemporaryDirectory() as d, Library(d) as lib:
            seed(lib)
            before = snapshot(lib)
            with self.assertRaises(ValueError), lib.op("outer"):
                with lib.op("inner"):
                    lib.delete(["b0"])
                raise ValueError("after the inner block")
            self.assertEqual(snapshot(lib), before)

    def test_another_process_never_sees_half_an_operation(self):
        with tempfile.TemporaryDirectory() as d:
            with Library(d) as lib:
                seed(lib, 200)
            with Library(d) as writer:
                with writer.op("big"):
                    writer.delete([f"b{i}" for i in range(100)])
                    with Library(d) as reader:                           # WAL: the reader sees the state before the operation
                        self.assertEqual(reader.count(), 200)
                    writer.delete([f"b{i}" for i in range(100, 150)])
                self.assertEqual(writer.count(), 50)


KILLED = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, {scripts!r})
    from mhbm.model import Bookmark
    from mhbm.store import Library
    lib = Library(sys.argv[1])
    with lib.op("killed"):
        lib.delete([f"b{{i}}" for i in range(10)])
        lib.upsert([Bookmark(id="x", url="https://example.com/x", title="never", folder="Gone")])
        lib.add_folders(["Gone"])
        os._exit(7)                     # the process dies in the middle of the operation: no exception, no cleanup
""").format(scripts=SCRIPTS)


class Killed(unittest.TestCase):
    def test_a_process_that_dies_mid_operation_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            with Library(d) as lib:
                seed(lib)
                before = snapshot(lib)
            p = subprocess.run([sys.executable, "-I", "-c", KILLED, d], capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 7, p.stderr)
            with Library(d) as lib:
                self.assertEqual(snapshot(lib), before)
                self.assertEqual(lib.health()["integrity"], "ok")

    def test_apply_plan_killed_at_any_moment_is_all_or_nothing(self):
        """The audit saw 5 of 18 random kills leave 14,400-18,400 of 20,000 rows. Whenever the signal lands, the library is the old one
        or the new one, with its journal."""
        n, doomed = 3000, 1500
        for sig, delay in ((signal.SIGKILL, 0.0), (signal.SIGKILL, 0.25), (signal.SIGKILL, 0.6), (signal.SIGTERM, 0.4), (signal.SIGINT, 0.3)):
            if os.name == "nt" and sig != signal.SIGTERM:
                continue
            with self.subTest(signal=sig.name, delay=delay), tempfile.TemporaryDirectory() as d:
                with Library(d) as lib:
                    lib.upsert([Bookmark(id=f"b{i}", url=f"https://example.com/{i}", title=f"word{i}", folder="Old") for i in range(n)])
                plan = Path(d, "plan.json")
                plan.write_text(json.dumps({f"b{i}": {"delete": True} for i in range(doomed)}))
                env = dict(os.environ, MHBM_LANG="en", PYTHONDONTWRITEBYTECODE="1")
                code = f"import sys; sys.path.insert(0, {SCRIPTS!r}); from mhbm.cli import main; sys.exit(main(sys.argv[1:]))"
                p = subprocess.Popen([sys.executable, "-I", "-c", code, "--home", d, "--quiet", "apply-plan", str(plan), "--apply"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
                time.sleep(delay)
                try:
                    p.send_signal(sig)
                except (ProcessLookupError, OSError):
                    pass
                p.wait(timeout=60)
                with Library(d) as lib:
                    self.assertEqual(lib.health()["integrity"], "ok")
                    count, ops = lib.count(), len(lib.history())
                    self.assertIn((count, ops), {(n, 0), (n - doomed, 1)}, (count, ops))
                    if ops:                                              # when it did land, it can be undone
                        lib.undo()
                        self.assertEqual(lib.count(), n)


if __name__ == "__main__":
    unittest.main()
