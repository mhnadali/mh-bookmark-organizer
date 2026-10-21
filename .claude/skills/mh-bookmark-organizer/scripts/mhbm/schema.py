"""The library's database layout and how an older file is brought up to date.

``PRAGMA user_version`` records which layout a file has. Opening a library compares it with ``SCHEMA_VERSION``:

* equal: nothing is written, so any number of processes can open the library at once, even while another one is writing;
* older: the file is copied to ``backups/`` first, then every step from its version up to the current one runs inside a
  single write transaction (it either all happens or none of it does), and the version is stamped in the same transaction;
* newer: refused, because an older mhbm cannot know what a newer one changed. Upgrade mhbm or open a copy.

Versions
    1  the first release: one ``bookmarks`` table that also held the page text, no version stamp (``user_version`` = 0)
    2  page text in ``pages``; link-check, reading-list and favourite columns; undo journal (``ops`` and ``journal``)
    3  ``suggestions`` and ``feedback``: learning from what you do with the suggestions

A step only has to turn version *n* into *n* + 1. Tables that are new in a version are created by the final
``CREATE TABLE IF NOT EXISTS`` pass, so a step only changes existing tables and moves data.
"""
from __future__ import annotations

import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookmarks(
  id TEXT PRIMARY KEY, url TEXT NOT NULL, norm TEXT, title TEXT, folder TEXT, tags TEXT,
  notes TEXT, added INTEGER, status TEXT, http_code INTEGER, last_checked INTEGER,
  fail_count INTEGER DEFAULT 0, first_failed INTEGER DEFAULT 0, wayback TEXT,
  archive_path TEXT, meta TEXT, state TEXT DEFAULT '', favorite INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_norm ON bookmarks(norm);
CREATE TABLE IF NOT EXISTS pages(id TEXT PRIMARY KEY, content TEXT);
CREATE TABLE IF NOT EXISTS folders(path TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS folder_meta(path TEXT PRIMARY KEY, add_date INTEGER, last_modified INTEGER);
CREATE TABLE IF NOT EXISTS icons(id TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS sync_state(profile TEXT, key TEXT, title TEXT, PRIMARY KEY(profile, key));
CREATE TABLE IF NOT EXISTS sync_meta(profile TEXT PRIMARY KEY, last_sync INTEGER, summary TEXT);
CREATE TABLE IF NOT EXISTS ops(op INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, command TEXT,
  changes INTEGER, undone INTEGER DEFAULT 0, folders TEXT);
CREATE TABLE IF NOT EXISTS journal(seq INTEGER PRIMARY KEY AUTOINCREMENT, op INTEGER, bm_id TEXT, before TEXT);
CREATE TABLE IF NOT EXISTS suggestions(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, bm_id TEXT, norm TEXT, folder TEXT,
  level TEXT, confidence REAL, source TEXT, status TEXT DEFAULT 'open');
CREATE INDEX IF NOT EXISTS idx_sug_bm ON suggestions(bm_id, status);
CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, bm_id TEXT, norm TEXT, host TEXT,
  suggested TEXT, final TEXT, action TEXT, confidence REAL, level TEXT, source TEXT, channel TEXT, explicit INTEGER DEFAULT 0,
  suggestion INTEGER, op INTEGER, op_token TEXT);
CREATE INDEX IF NOT EXISTS idx_fb_norm ON feedback(norm);
"""
STATEMENTS = tuple(s.strip() for s in SCHEMA.split(";") if s.strip())
TABLES = tuple(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA))

# columns added to existing tables after the first release: (table, column, definition)
ADDED_COLUMNS = (
    ("bookmarks", "fail_count", "INTEGER DEFAULT 0"), ("bookmarks", "first_failed", "INTEGER DEFAULT 0"),
    ("bookmarks", "wayback", "TEXT"), ("bookmarks", "meta", "TEXT"), ("bookmarks", "state", "TEXT DEFAULT ''"),
    ("bookmarks", "favorite", "INTEGER DEFAULT 0"), ("ops", "folders", "TEXT"),
)


class SchemaTooNew(ValueError):
    """The library was written by a newer mhbm than this one."""


@contextmanager
def write_transaction(db):
    """A write transaction that takes the write lock first (waiting for it), so what is read inside it cannot change meanwhile."""
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    with db:
        yield


def fingerprint(db) -> str:
    """A string that changes whenever what a plan or an in-memory copy was based on changes: bookmarks (count, newest row), link checks
    (time, failures, codes, statuses), the archive (pages), and the journal (newest operation, how many are undone - ``undo`` adds no row).
    Cheap aggregates only, so it can be taken before every apply."""
    ops = db.execute("SELECT COALESCE(MAX(op),0), COALESCE(SUM(undone),0), COUNT(*) FROM ops").fetchone()
    bms = db.execute("SELECT COUNT(*), COALESCE(MAX(rowid),0), COALESCE(MAX(last_checked),0), COALESCE(SUM(fail_count),0), "
                     "COALESCE(SUM(http_code),0), COALESCE(SUM(LENGTH(status)),0), COALESCE(SUM(favorite),0) FROM bookmarks").fetchone()
    pages = db.execute("SELECT COUNT(*), COALESCE(MAX(rowid),0) FROM pages").fetchone()
    return ":".join(str(int(v)) for v in (*ops, *bms, *pages))


def ro_uri(path) -> str:
    """The ``file:`` URI that opens ``path`` read-only. Percent-encodes ``#``, ``?`` and ``%``, which a hand-built URI would misread as a
    fragment, a query or an escape - opening some other file (and creating it) instead of the library."""
    return Path(path).resolve().as_uri() + "?mode=ro"


def _tables(db) -> set:
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(db, table: str) -> set:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def version_of(db) -> int:
    """The layout version of an open file; a library from before versions were stamped counts as 1, an empty file as 0."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    return version or (1 if "bookmarks" in _tables(db) else 0)


def _missing_columns(db) -> list:
    have = {t: _columns(db, t) for t in {t for t, _, _ in ADDED_COLUMNS}}
    return [(t, c, ddl) for t, c, ddl in ADDED_COLUMNS if have[t] and c not in have[t]]   # a table that does not exist yet is not "missing a column"


def _add_missing_columns(db) -> None:
    for table, column, ddl in _missing_columns(db):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _step_1_to_2(db) -> None:
    """Page text moves out of ``bookmarks`` into ``pages``; the link-check, reading and favourite columns appear."""
    _add_missing_columns(db)
    if "content" in _columns(db, "bookmarks"):
        db.execute("CREATE TABLE IF NOT EXISTS pages(id TEXT PRIMARY KEY, content TEXT)")
        db.execute("INSERT OR IGNORE INTO pages SELECT id, content FROM bookmarks WHERE content IS NOT NULL AND content != ''")
        try:  # DROP COLUMN needs SQLite 3.35; on older versions the unused column simply stays
            db.execute("ALTER TABLE bookmarks DROP COLUMN content")
        except sqlite3.OperationalError:
            pass


def _step_2_to_3(db) -> None:
    """Early builds that stamped version 2 had no ``ops.folders``; ``suggestions`` and ``feedback`` come from the create pass."""
    _add_missing_columns(db)


STEPS = {1: _step_1_to_2, 2: _step_2_to_3}   # from version -> function that makes it version + 1
assert set(STEPS) == set(range(1, SCHEMA_VERSION)), "every version below the current one needs a step"


def backup_before_upgrade(db, root, version: int) -> Path:
    """A consistent copy of the library file, taken while others may be using it, so a failed upgrade loses nothing."""
    folder = Path(root) / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = folder / f"library-schema{version}-{stamp}.db"
    n = 1
    while dest.exists():
        dest = folder / f"library-schema{version}-{stamp}-{n}.db"
        n += 1
    out = sqlite3.connect(dest)
    try:
        db.backup(out)
    finally:
        out.close()
    return dest


def upgrade(db, root) -> int:
    """Create or upgrade the layout; returns the version the file had on arrival (0 = brand new, current = nothing to do)."""
    version = version_of(db)
    if version > SCHEMA_VERSION:
        raise SchemaTooNew(f"this library uses layout {version}, which is newer than this mhbm understands ({SCHEMA_VERSION}); "
                           "upgrade mhbm, or open the library with the version that wrote it")
    if version == SCHEMA_VERSION:
        if _tables(db) >= set(TABLES) and not _missing_columns(db):
            return version      # the normal case: nothing to write
    elif version:
        backup_before_upgrade(db, root, version)
    with write_transaction(db):
        arrived = version_of(db)    # another process may have upgraded the file while we waited for the lock
        if arrived > SCHEMA_VERSION:
            raise SchemaTooNew(f"this library uses layout {arrived}, which is newer than this mhbm understands ({SCHEMA_VERSION})")
        for step in range(arrived, SCHEMA_VERSION):     # a brand-new file (0) has nothing to upgrade: the create pass below is all it needs
            if arrived:
                STEPS[step](db)
        _add_missing_columns(db)
        for statement in STATEMENTS:
            db.execute(statement)
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return version
