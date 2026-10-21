"""Several mhbm processes share one library: a scheduled ``maintain``, the web UI, the extension's native host, your shell."""
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

import common  # noqa: F401
from mhbm import store
from mhbm.model import Bookmark
from mhbm.store import Library

SCRIPTS = str(Path(__file__).resolve().parents[1] / "scripts")

WRITER = textwrap.dedent("""
    import sys
    sys.path.insert(0, {scripts!r})
    from mhbm.model import Bookmark
    from mhbm.store import Library
    home, idx, count = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    lib = Library(home)
    for i in range(count):
        with lib.op("stress"):
            lib.upsert([Bookmark(id=f"w{{idx}}-{{i}}", url=f"https://example.com/{{idx}}/{{i}}", title=f"stress {{idx}} {{i}}", folder="Stress")])
            lib.add_folders(["Stress"])
        if i % 7 == 0:
            lib.record_suggestions([{{"bm_id": f"w{{idx}}-{{i}}", "norm": "x", "folder": "Stress", "source": "learned"}}])
    lib.close()
""").format(scripts=SCRIPTS)

READER = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, {scripts!r})
    from mhbm.store import Library
    home, stop = sys.argv[1], sys.argv[2]
    lib = Library(home)
    worst, reads, last = 0.0, 0, 0
    import os
    while not os.path.exists(stop):
        t = time.monotonic()
        n = lib.count()
        lib.search("stress")
        lib.all(snippet=0)
        worst = max(worst, time.monotonic() - t)
        assert n >= last, f"count went backwards: {{last}} -> {{n}}"
        last, reads = n, reads + 1
    print(reads, round(worst, 3))
""").format(scripts=SCRIPTS)

CLI = f"import sys; sys.path.insert(0, {SCRIPTS!r}); from mhbm.cli import main; sys.exit(main(sys.argv[1:]))"


def py(*args, **kw):
    env = dict(os.environ, MHBM_LANG="en")
    return subprocess.Popen([sys.executable, "-I", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, **kw)


class Settings(unittest.TestCase):
    def test_wal_and_busy_timeout_are_set(self):
        with tempfile.TemporaryDirectory() as d, Library(d) as lib:
            self.assertEqual(lib.journal_mode, "wal")
            self.assertEqual(lib.db.execute("PRAGMA busy_timeout").fetchone()[0], store.BUSY_TIMEOUT_MS)
            self.assertEqual(lib.health()["integrity"], "ok")
            self.assertEqual(lib.health()["journal_mode"], "wal")

    def test_opting_out_of_wal(self):
        with tempfile.TemporaryDirectory() as d:
            with Library(d):
                pass
            os.environ["MHBM_JOURNAL_MODE"] = "delete"
            try:
                with Library(d) as lib:
                    self.assertEqual(lib.journal_mode, "delete")
            finally:
                del os.environ["MHBM_JOURNAL_MODE"]
            with Library(d) as lib:                                # and it goes back by default
                self.assertEqual(lib.journal_mode, "wal")

    def test_timeout_can_be_configured(self):
        os.environ["MHBM_BUSY_TIMEOUT_MS"] = "1234"
        try:
            with tempfile.TemporaryDirectory() as d, Library(d) as lib:
                self.assertEqual(lib.db.execute("PRAGMA busy_timeout").fetchone()[0], 1234)
            os.environ["MHBM_BUSY_TIMEOUT_MS"] = "not a number"
            self.assertEqual(store.busy_timeout_ms(), store.BUSY_TIMEOUT_MS)
        finally:
            del os.environ["MHBM_BUSY_TIMEOUT_MS"]

    def test_unwritable_location_keeps_the_journal_mode_it_has(self):
        with tempfile.TemporaryDirectory() as d, Library(d) as lib:
            class Locked:                                          # a connection whose PRAGMA journal_mode = wal fails
                def __init__(self, real):
                    self.real = real

                def execute(self, sql, *a):
                    if "journal_mode =" in sql:
                        raise sqlite3.OperationalError("unable to open database file")
                    return self.real.execute(sql, *a)

            real, lib.db = lib.db, Locked(lib.db)
            try:
                self.assertEqual(lib._tune(), "wal")               # falls back to reading what the file already uses
            finally:
                lib.db = real


class Waiting(unittest.TestCase):
    def test_a_writer_waits_for_the_lock_instead_of_failing(self):
        with tempfile.TemporaryDirectory() as d:
            with Library(d):
                pass
            holder = sqlite3.connect(Path(d) / "library.db", timeout=5, check_same_thread=False)
            holder.execute("BEGIN IMMEDIATE")
            threading.Timer(0.6, holder.commit).start()
            with Library(d) as lib:                                # opening is write-free once the schema is current, so it does not wait
                t = time.monotonic()
                lib.upsert([Bookmark(id="a", url="https://example.com/a")])
                self.assertGreaterEqual(time.monotonic() - t, 0.4)   # it waited for the lock
                self.assertEqual(lib.count(), 1)                     # and then wrote
            holder.close()

    def test_readers_are_not_blocked_by_a_writer(self):
        with tempfile.TemporaryDirectory() as d:
            with Library(d) as lib:
                lib.upsert([Bookmark(id="a", url="https://example.com/a", title="before")])
            writer = sqlite3.connect(Path(d) / "library.db", timeout=5)
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE bookmarks SET title='mid-write'")
            try:
                t = time.monotonic()
                with Library(d) as lib:                            # opens, counts and reads while the other process is mid-transaction
                    self.assertEqual([b.title for b in lib.all()], ["before"])
                self.assertLess(time.monotonic() - t, 1.0)
            finally:
                writer.rollback()
                writer.close()

    def test_a_lock_held_too_long_gives_a_clear_message_and_exit_code(self):
        with tempfile.TemporaryDirectory() as d:
            with Library(d) as lib:
                lib.upsert([Bookmark(id="a", url="https://example.com/a")])
            holder = sqlite3.connect(Path(d) / "library.db", timeout=5)
            holder.execute("BEGIN IMMEDIATE")
            try:
                env = dict(os.environ, MHBM_LANG="en", MHBM_BUSY_TIMEOUT_MS="300")
                p = subprocess.run([sys.executable, "-I", "-c", CLI, "--home", d, "tag", "a", "--add", "x"],
                                   capture_output=True, text=True, env=env, timeout=60)
                self.assertEqual(p.returncode, 75, p.stderr)
                self.assertIn("library is busy", p.stderr)
                self.assertNotIn("Traceback", p.stderr)
            finally:
                holder.rollback()
                holder.close()


class Stress(unittest.TestCase):
    def test_many_processes_writing_reading_and_running_commands(self):
        writers, per_writer = 4, 40
        with tempfile.TemporaryDirectory() as d:
            with Library(d) as lib:
                lib.upsert([Bookmark(id="seed", url="https://example.com/seed", title="seed")])
            stop = Path(d) / "stop"
            readers = [py("-c", READER, d, str(stop)) for _ in range(2)]
            procs = [py("-c", WRITER, d, str(i), str(per_writer)) for i in range(writers)]
            clis = [py("-c", CLI, "--home", d, "--json", "stats") for _ in range(3)]
            errors = []
            for p in procs + clis:
                out, err = p.communicate(timeout=240)
                if p.returncode != 0 or "locked" in err or "Traceback" in err:
                    errors.append((p.args[3:5], p.returncode, err[-400:]))
            stop.write_text("x")
            reads = []
            for p in readers:
                out, err = p.communicate(timeout=60)
                if p.returncode != 0 or "Traceback" in err:
                    errors.append(("reader", p.returncode, err[-400:]))
                else:
                    reads.append(out.split())
            self.assertEqual(errors, [])
            for n, worst in reads:
                self.assertGreater(int(n), 0)
                self.assertLess(float(worst), 10.0)                # a read never waited anywhere near the busy timeout
            with Library(d) as lib:
                self.assertEqual(lib.count(), 1 + writers * per_writer)            # no write was lost
                self.assertEqual(lib.health()["integrity"], "ok")
                ids = {b.id for b in lib.all(snippet=0)}
                self.assertEqual(ids, {"seed"} | {f"w{w}-{i}" for w in range(writers) for i in range(per_writer)})
                hits = lib.search("stress", limit=500)
                self.assertEqual(len(hits), writers * per_writer)                  # the full-text index kept up with every process
                ops = lib.history(limit=1000)
                self.assertEqual(len({o["op"] for o in ops}), len(ops))            # each operation journaled exactly once
                self.assertEqual(len(ops), min(store.KEEP_OPS, writers * per_writer))
                self.assertEqual(sum(o["changes"] for o in ops), len(ops))         # one change per operation: nothing mixed up between processes
                self.assertEqual(lib.db.execute("SELECT COUNT(*) FROM suggestions WHERE status='open'").fetchone()[0],
                                 writers * len(range(0, per_writer, 7)))

    def test_concurrent_undo_applies_an_operation_once(self):
        with tempfile.TemporaryDirectory() as d:
            with Library(d) as lib:
                with lib.op("one"):
                    lib.upsert([Bookmark(id="a", url="https://example.com/a", title="first")])
                with lib.op("two"):
                    lib.upsert([Bookmark(id="a", url="https://example.com/a", title="second")])
            code = (f"import sys, json; sys.path.insert(0, {SCRIPTS!r}); from mhbm.store import Library; l = Library(sys.argv[1])\n"
                    "try:\n    print(json.dumps(l.undo()))\nexcept ValueError as e:\n    print('ERR', e)")
            results = [py("-c", code, d) for _ in range(2)]
            outs = [p.communicate(timeout=60)[0].strip() for p in results]
            with Library(d) as lib:
                titles = [b.title for b in lib.all()]
                undone = [o["undone"] for o in lib.history()]
            self.assertEqual(sorted(undone), [1, 1])               # the two processes undid the two operations, one each...
            self.assertEqual(titles, [])                           # ...so the bookmark (created by the first one) is gone, not stuck halfway
            self.assertFalse(any(o.startswith("ERR") for o in outs), outs)


if __name__ == "__main__":
    unittest.main()
