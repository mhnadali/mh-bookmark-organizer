"""SQLite library: incremental FTS5 index, journal-based undo and full snapshots."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import schema
from .model import Bookmark
from .schema import SCHEMA_VERSION, SchemaTooNew  # noqa: F401  (re-exported)
from .textnorm import normalize_text

COLS = ["id", "url", "norm", "title", "folder", "tags", "notes", "added", "status", "http_code",
        "last_checked", "fail_count", "first_failed", "wayback", "archive_path", "meta", "state", "favorite"]
_CHUNK = 400
KEEP_OPS = 50  # undoable operations kept in the journal


class StaleRead(ValueError):
    """Another process changed the library after this one read it."""
BUSY_TIMEOUT_MS = 30_000  # how long a command waits for another mhbm process to finish writing before giving up


def busy_timeout_ms() -> int:
    try:
        return max(0, int(os.environ.get("MHBM_BUSY_TIMEOUT_MS", BUSY_TIMEOUT_MS)))
    except ValueError:
        return BUSY_TIMEOUT_MS


def is_busy(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and ("locked" in str(exc) or "busy" in str(exc))


def _marks(n: int) -> str:
    return ",".join("?" * n)


def _chunks(seq, n=_CHUNK):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ----------------------------------------------------------------- queries
FILTER_KEYS = {"tag", "folder", "site", "host", "before", "after", "status", "is", "title", "state"}


def parse_query(q: str) -> dict:
    """``kubernetes "exact phrase" -noise tag:dev folder:Dev site:github.com after:2024-01-01 is:unfiled``."""
    out: dict = {"terms": [], "phrases": [], "not": [], "filters": []}
    for m in re.finditer(r'(-?)(?:(\w+):)?(?:"([^"]+)"|(\S+))', q or ""):
        neg, key, phrase, word = m.groups()
        key = (key or "").lower()
        value = phrase if phrase is not None else word
        if key in FILTER_KEYS:
            out["filters"].append((key, value, bool(neg)))
        elif key:  # unknown "key:" - treat as plain text
            out["terms"].append(f"{key} {value}".strip())
        elif neg:
            out["not"].append(value)
        elif phrase is not None:
            out["phrases"].append(phrase)
        else:
            out["terms"].append(value)
    return out


def _date(value: str) -> int:
    try:
        return int(datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return 0


def filter_sql(filters) -> tuple:
    clauses: list = []
    params: list = []
    for key, value, neg in filters:
        v = value.lower()
        like = f"%{v}%"
        c = None
        p: list = []
        if key == "tag":
            c, p = "LOWER(b.tags) LIKE ?", [f'%"{v}"%']
        elif key == "folder":
            c, p = ("(LOWER(b.folder) = ? OR LOWER(b.folder) LIKE ? OR LOWER(b.folder) LIKE ? OR LOWER(b.folder) LIKE ?)",
                    [v, v + "/%", "%/" + v, "%/" + v + "/%"])  # whole path, prefix, or any folder name along the path
        elif key in ("site", "host"):
            c, p = "(LOWER(b.url) LIKE ? OR LOWER(b.url) LIKE ?)", [f"%://{v}/%", f"%://%.{v}/%"]
        elif key == "title":
            c, p = "LOWER(b.title) LIKE ?", [like]
        elif key == "status":
            c, p = "b.status = ?", [v]
        elif key == "state":
            c, p = "b.state = ?", [v]
        elif key == "before":
            c, p = "b.added > 0 AND b.added < ?", [_date(value)]
        elif key == "after":
            c, p = "b.added >= ?", [_date(value)]
        elif key == "is":
            c = {"untagged": "b.tags IN ('[]','')", "unfiled": "(b.folder = '' OR b.folder IN "
                 "('Bookmarks bar','Other bookmarks','Mobile bookmarks','Bookmarks menu'))",
                 "archived": "b.id IN (SELECT id FROM pages)", "unchecked": "(b.status = '' OR b.status IS NULL)",
                 "unread": "b.state = 'unread'", "reading": "b.state = 'reading'", "read": "b.state = 'read'",
                 "favorite": "b.favorite = 1", "favourite": "b.favorite = 1"}.get(v)
            p = []
        if c:
            clauses.append(f"NOT ({c})" if neg else c)
            params += p
    return clauses, params


class Library:
    def __init__(self, root, *, check_same_thread: bool = True, read_only: bool = False):
        """``read_only=True`` (also ``MHBM_READONLY=1``) opens the file with SQLite ``mode=ro`` and changes nothing: no directory, no
        migration, no journal-mode switch, no search-index repair. A library that needs an upgrade is refused with a clear message."""
        self.read_only = read_only or os.environ.get("MHBM_READONLY") == "1"
        self.root = Path(root)
        if self.read_only:
            path = self.root / "library.db"
            if not path.is_file():
                raise FileNotFoundError(f"no library at {self.root}")
            self.db = sqlite3.connect(schema.ro_uri(path), uri=True, timeout=busy_timeout_ms() / 1000, check_same_thread=check_same_thread)
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.root / "library.db", timeout=busy_timeout_ms() / 1000, check_same_thread=check_same_thread)
        self.db.row_factory = sqlite3.Row
        self.fts = False
        self._op: Optional[dict] = None
        self._op_token = ""
        self._joined = False             # inside undo/restore: _tx() joins the surrounding write transaction
        self.read_fp = ""                # fingerprint taken when the bookmarks were last read with all()
        self.db.create_function("ntext", 1, normalize_text, deterministic=True)
        try:
            self.db.execute(f"PRAGMA busy_timeout = {busy_timeout_ms()}")
            if self.read_only:
                found = schema.version_of(self.db)
                if found != schema.SCHEMA_VERSION:
                    raise ValueError(f"this library uses layout {found}; open it once with a normal (writable) mhbm command to upgrade it to {schema.SCHEMA_VERSION}")
                self.upgraded_from = found
                self.journal_mode = "read-only"
                self.fts = bool(self.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'bm_fts'").fetchone())
                return
            self.upgraded_from = schema.upgrade(self.db, self.root)   # refuses a newer layout before anything else touches the file
            self.journal_mode = self._tune()
            self.fts = self._init_fts()
            if self.upgraded_from == 1:
                self.reindex_all()      # the first release indexed page text kept inside the bookmarks table; it lives in `pages` now
        except BaseException:
            self.db.close()             # a library that cannot be opened must not leave the file held open
            raise

    # ------------------------------------------------------ housekeeping
    def _tune(self) -> str:
        """Several mhbm processes share one library (a scheduled ``maintain``, the web UI, the extension's native host, your shell).

        Write-ahead logging lets readers carry on while one process writes, and a busy lock is waited for instead of failing.
        ``MHBM_JOURNAL_MODE=delete`` opts out of WAL, for file systems that cannot share memory-mapped files (some network shares).
        """
        want = (os.environ.get("MHBM_JOURNAL_MODE") or "wal").lower()
        if want not in ("wal", "delete"):
            want = "wal"
        try:
            return str(self.db.execute(f"PRAGMA journal_mode = {want}").fetchone()[0]).lower()
        except sqlite3.OperationalError:    # still locked by another process after the wait, or an unsupported location: keep what the file has
            try:
                return str(self.db.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            except sqlite3.OperationalError:
                return "unknown"

    def _write(self):
        return schema.write_transaction(self.db)

    def fingerprint(self) -> str:
        return schema.fingerprint(self.db)

    def ensure_unchanged_since_read(self) -> None:
        """Raise if another process changed the library after ``all()`` read the bookmarks this process is about to write back - otherwise
        its changes would be silently overwritten (and journaled as if they had never happened)."""
        if self.read_fp and self.fingerprint() != self.read_fp:
            raise StaleRead("the library was changed by another mhbm process while this command was working (a scheduled job, the web UI, "
                            "the browser extension). Nothing was written; run the command again.")

    @contextmanager
    def _tx(self):
        """Commit at the end of the block - unless an operation (``op``) is open: then the operation's one transaction commits, or rolls
        back, everything it did, so an interrupted ``--apply`` leaves the old state or the new one, never half of it."""
        if (self._op is not None or self._joined) and self.db.in_transaction:
            yield
        else:
            with self.db:
                yield

    def health(self) -> dict:
        """What the doctor reports: journal mode, wait time, layout version and an integrity check."""
        return {"journal_mode": str(self.db.execute("PRAGMA journal_mode").fetchone()[0]).lower(), "busy_timeout_ms": busy_timeout_ms(),
                "schema": self.db.execute("PRAGMA user_version").fetchone()[0], "integrity": self.db.execute("PRAGMA integrity_check").fetchone()[0]}

    def index_gaps(self) -> int:
        """Bookmarks with no search-index entry plus index entries with no bookmark (0 = the index matches the library)."""
        if not self.fts:
            return 0
        missing = self.db.execute("SELECT COUNT(*) FROM bookmarks WHERE rowid NOT IN (SELECT rowid FROM bm_fts)").fetchone()[0]
        extra = self.db.execute("SELECT COUNT(*) FROM bm_fts WHERE rowid NOT IN (SELECT rowid FROM bookmarks)").fetchone()[0]
        return missing + extra

    def vacuum(self) -> None:
        """Compact the file. SQLite may renumber the rows of a table keyed by text (``bookmarks``), and the search index is
        keyed by those row numbers, so the index is rebuilt afterwards."""
        self.db.execute("VACUUM")
        self.reindex_all()

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _init_fts(self) -> bool:
        try:
            self.db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS bm_fts USING fts5("
                "title, url, folder, tags, notes, content, tokenize='porter unicode61')")
        except sqlite3.OperationalError:
            return False
        self.fts = True
        if self.db.execute("SELECT COUNT(*) FROM bm_fts").fetchone()[0] == 0 and self.count():
            self.reindex_all()  # first open of a migrated library
        return True

    # -------------------------------------------------------- row mapping
    @staticmethod
    def _row(b: Bookmark):
        return (b.id, b.url, b.norm, b.title, b.folder, json.dumps(b.tags), b.notes, b.added, b.status,
                b.http_code, b.last_checked, b.fail_count, b.first_failed, b.wayback, b.archive_path,
                json.dumps(b.meta) if b.meta else "", b.state, int(b.favorite))

    @staticmethod
    def _bm(r) -> Bookmark:
        k = r.keys()
        return Bookmark(
            id=r["id"], url=r["url"], norm=r["norm"] or "", title=r["title"] or "", folder=r["folder"] or "",
            tags=json.loads(r["tags"] or "[]"), notes=r["notes"] or "", added=r["added"] or 0,
            status=r["status"] or "", http_code=r["http_code"] or 0, last_checked=r["last_checked"] or 0,
            fail_count=r["fail_count"] or 0, first_failed=r["first_failed"] or 0, wayback=r["wayback"] or "",
            archive_path=r["archive_path"] or "", meta=json.loads(r["meta"]) if r["meta"] else {},
            state=(r["state"] or "") if "state" in k else "", favorite=bool(r["favorite"]) if "favorite" in k else False,
            content=(r["content"] or "") if "content" in k else "")

    # ------------------------------------------------------------- reads
    def all(self, snippet: int = 2000) -> list:
        """All bookmarks; ``content`` holds the first ``snippet`` chars of page text (0 = skip it)."""
        self.read_fp = self.fingerprint()     # taken first: a writer that commits meanwhile makes the later check fail, never pass
        return list(self.iter_all(snippet=snippet))

    def iter_all(self, batch: int = 500, snippet: int = 2000):
        """Stream bookmarks in batches so very large libraries never need to fit in memory at once."""
        sel = f"b.*, substr(COALESCE(p.content,''),1,{int(snippet)}) AS content" if snippet else "b.*"
        last = ""
        while True:
            rows = self.db.execute(
                f"SELECT {sel}, b.rowid AS _rid FROM bookmarks b LEFT JOIN pages p ON p.id=b.id "
                "WHERE b.rowid > ? ORDER BY b.rowid LIMIT ?", (last or 0, batch)).fetchall()
            if not rows:
                return
            last = rows[-1]["_rid"]
            for r in rows:
                yield self._bm(r)

    def get(self, bid: str):
        r = self.db.execute("SELECT b.*, COALESCE(p.content,'') AS content FROM bookmarks b "
                            "LEFT JOIN pages p ON p.id=b.id WHERE b.id=?", (bid,)).fetchone()
        return self._bm(r) if r else None

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0]

    def ids(self) -> set:
        return {r[0] for r in self.db.execute("SELECT id FROM bookmarks")}

    # ------------------------------------------------------------ writes
    def _remember(self, ids) -> None:
        """Record pre-change rows for the undo journal (once per id per operation)."""
        if self._op is None:
            return
        todo = [i for i in ids if i not in self._op]
        for chunk in _chunks(todo):
            found = {}
            for r in self.db.execute(f"SELECT * FROM bookmarks WHERE id IN ({_marks(len(chunk))})", chunk):
                found[r["id"]] = dict(r)
            for i in chunk:
                self._op[i] = found.get(i)

    def upsert(self, bms) -> None:
        bms = list(bms)
        self._remember(b.id for b in bms)
        sets = ",".join(f"{c}=excluded.{c}" for c in COLS if c != "id")
        with self._tx():
            self.db.executemany(
                f"INSERT INTO bookmarks({','.join(COLS)}) VALUES({_marks(len(COLS))}) "
                f"ON CONFLICT(id) DO UPDATE SET {sets}", [self._row(b) for b in bms])
        self._reindex([b.id for b in bms])

    def delete(self, ids) -> None:
        ids = list(ids)
        self._remember(ids)
        for chunk in _chunks(ids):
            if self.fts:
                self.db.execute(f"DELETE FROM bm_fts WHERE rowid IN (SELECT rowid FROM bookmarks WHERE id IN ({_marks(len(chunk))}))", chunk)
            with self._tx():
                self.db.execute(f"DELETE FROM bookmarks WHERE id IN ({_marks(len(chunk))})", chunk)

    def gc(self) -> int:
        """Drop page text that no longer belongs to any bookmark (kept after delete so undo can restore it)."""
        with self._tx():
            self.db.execute("DELETE FROM icons WHERE id NOT IN (SELECT id FROM bookmarks)")
            return self.db.execute("DELETE FROM pages WHERE id NOT IN (SELECT id FROM bookmarks)").rowcount

    def save_pages(self, bms) -> None:
        rows = [(b.id, b.content) for b in bms if b.content]
        with self._tx():
            self.db.executemany("INSERT INTO pages(id, content) VALUES(?,?) "
                                "ON CONFLICT(id) DO UPDATE SET content=excluded.content", rows)
        self._reindex([r[0] for r in rows])

    def replace_all(self, bms) -> None:
        with self._tx():
            self.db.execute("DELETE FROM bookmarks")
            self.db.execute("DELETE FROM pages")
        self.upsert(bms)
        self.reindex_all()

    # ----------------------------------------------------------- folders
    def folders(self) -> set:
        return {r[0] for r in self.db.execute("SELECT path FROM folders")}

    def add_folders(self, paths) -> None:
        with self._tx():
            self.db.executemany("INSERT OR IGNORE INTO folders(path) VALUES(?)", [(p,) for p in paths if p])

    def set_folders(self, paths) -> None:
        with self._tx():            # one transaction: a failure in between must not leave the folder list empty
            self.db.execute("DELETE FROM folders")
            self.db.executemany("INSERT OR IGNORE INTO folders(path) VALUES(?)", [(p,) for p in paths if p])

    def used_folders(self) -> set:
        """Every folder that holds a bookmark, plus its ancestors."""
        used: set = set()
        for (f,) in self.db.execute("SELECT DISTINCT folder FROM bookmarks WHERE folder != ''"):
            parts = f.split("/")
            used.update("/".join(parts[: i + 1]) for i in range(len(parts)))
        return used

    def sync_baseline(self, profile: str) -> dict:
        return {r[0]: r[1] for r in self.db.execute("SELECT key, title FROM sync_state WHERE profile=?", (profile,))}

    def save_sync_baseline(self, profile: str, state: dict, summary: dict) -> None:
        import time as _t
        with self._tx():
            self.db.execute("DELETE FROM sync_state WHERE profile=?", (profile,))
            self.db.executemany("INSERT INTO sync_state(profile, key, title) VALUES(?,?,?)",
                                [(profile, k, t) for k, t in state.items()])
            self.db.execute("INSERT OR REPLACE INTO sync_meta(profile, last_sync, summary) VALUES(?,?,?)",
                            (profile, int(_t.time()), json.dumps(summary)))

    def sync_meta(self, profile: str):
        r = self.db.execute("SELECT last_sync, summary FROM sync_meta WHERE profile=?", (profile,)).fetchone()
        return {"last_sync": r[0], "summary": json.loads(r[1])} if r else None

    def folder_meta(self) -> dict:
        return {r[0]: (r[1], r[2]) for r in self.db.execute("SELECT path, add_date, last_modified FROM folder_meta")}

    def save_folder_meta(self, meta: dict) -> None:
        with self._tx():
            self.db.executemany("INSERT OR REPLACE INTO folder_meta(path, add_date, last_modified) VALUES(?,?,?)",
                                [(p, d[0], d[1]) for p, d in meta.items()])

    def save_icons(self, bms) -> None:
        rows = [(b.id, b.icon) for b in bms if b.icon]
        with self._tx():
            self.db.executemany("INSERT OR REPLACE INTO icons(id, data) VALUES(?,?)", rows)

    def icons(self) -> dict:
        return {r[0]: r[1] for r in self.db.execute("SELECT id, data FROM icons")}

    # ------------------------------------------------------------ search
    def _reindex(self, ids) -> None:
        if not self.fts:
            return
        for chunk in _chunks(ids):
            m = _marks(len(chunk))
            with self._tx():
                self.db.execute(f"DELETE FROM bm_fts WHERE rowid IN (SELECT rowid FROM bookmarks WHERE id IN ({m}))", chunk)
                self.db.execute(
                    "INSERT INTO bm_fts(rowid,title,url,folder,tags,notes,content) "
                    "SELECT b.rowid,ntext(b.title),b.url,ntext(b.folder),ntext(b.tags),ntext(b.notes),ntext(COALESCE(p.content,'')) "
                    f"FROM bookmarks b LEFT JOIN pages p ON p.id=b.id WHERE b.id IN ({m})", chunk)

    def reindex_all(self) -> None:
        if not self.fts:
            return
        with self._tx():
            self.db.execute("DELETE FROM bm_fts")
            self.db.execute(
                "INSERT INTO bm_fts(rowid,title,url,folder,tags,notes,content) "
                "SELECT b.rowid,ntext(b.title),b.url,ntext(b.folder),ntext(b.tags),ntext(b.notes),ntext(COALESCE(p.content,'')) "
                "FROM bookmarks b LEFT JOIN pages p ON p.id=b.id")

    def search(self, query: str, limit: int = 20, sort: str = "relevance") -> list:
        q = parse_query(query)
        clauses, params = filter_sql(q["filters"])
        tokens = lambda text: re.findall(r"[\w'-]+", normalize_text(text))  # noqa: E731  Persian/Arabic letter variants unified
        positives = [f'"{t}"*' for term in q["terms"] for t in tokens(term)]
        positives += ['"' + " ".join(tokens(p)) + '"' for p in q["phrases"] if tokens(p)]
        negatives = [t for n in q["not"] for t in tokens(n)]
        where = (" AND " + " AND ".join(clauses)) if clauses else ""
        order = "b.added DESC" if sort == "recent" else None

        if positives and self.fts:
            match = " ".join(positives) + "".join(f' NOT "{n}"' for n in negatives)
            sql = ("SELECT b.*, snippet(bm_fts, 5, '[', ']', '...', 12) AS snip FROM bm_fts "
                   f"JOIN bookmarks b ON b.rowid = bm_fts.rowid WHERE bm_fts MATCH ?{where} "
                   f"ORDER BY {order or 'bm25(bm_fts, 8, 3, 2, 4, 2, 1)'} LIMIT ?")
            try:
                rows = self.db.execute(sql, [match] + params + [limit]).fetchall()
                return [(self._bm(r), r["snip"] or "") for r in rows]
            except sqlite3.OperationalError:
                pass
        # No free text (filters only), no FTS5, or an unparsable MATCH: plain SQL
        hay = "ntext(b.title||' '||b.url||' '||b.folder||' '||b.tags||' '||b.notes||' '||COALESCE((SELECT content FROM pages p WHERE p.id=b.id),''))"
        conds = [f"{hay} LIKE ?" for _ in positives] + [f"{hay} NOT LIKE ?" for _ in negatives]
        like = [f"%{p.strip(chr(34)).rstrip('*')}%" for p in positives] + [f"%{n}%" for n in negatives]  # already normalised
        base = " AND ".join(conds + clauses) or "1=1"
        rows = self.db.execute(f"SELECT b.* FROM bookmarks b WHERE {base} ORDER BY b.added DESC LIMIT ?",
                               like + params + [limit]).fetchall()
        return [(self._bm(r), "") for r in rows]

    # ----------------------------------------------------- journal / undo
    @contextmanager
    def op(self, command: str):
        """Group writes into one undoable operation - and into ONE transaction: it commits together with its journal entry, or not at all.

        A crash, ``Ctrl-C``, a kill or an exception halfway through leaves the library exactly as it was before the operation."""
        if self._op is not None:       # nested: the outer operation owns the transaction
            yield
            return
        self._op = {}
        self._op_token = uuid.uuid4().hex
        try:
            if not self.db.in_transaction:
                self.db.execute("BEGIN IMMEDIATE")      # may fail (database busy): the library must not stay marked as inside an operation
            folders_before = sorted(self.folders())     # read inside the write lock: what is journaled is what the operation really started from
            yield
        except BaseException:
            self._op, self._op_token = None, ""
            self.db.rollback()
            raise
        before, self._op = self._op or {}, None
        token, self._op_token = self._op_token, ""
        try:                        # still inside the operation's transaction: the changes and their journal commit together
            entries = self._compact(before)
            judged = self.db.execute("SELECT 1 FROM feedback WHERE op_token=? LIMIT 1", (token,)).fetchone() is not None
            if entries or judged or sorted(self.folders()) != folders_before:  # a decision alone is an operation too: it can be undone
                cur = self.db.execute("INSERT INTO ops(ts, command, changes, folders) VALUES(?,?,?,?)",
                                      (int(time.time()), command, len(entries), json.dumps(folders_before)))
                op_id = cur.lastrowid or 0
                self.db.execute("UPDATE feedback SET op=? WHERE op_token=?", (op_id, token))
                self.db.executemany("INSERT INTO journal(op, bm_id, before) VALUES(?,?,?)",
                                    [(op_id, i, d) for i, d in entries])
                self.db.execute("DELETE FROM journal WHERE op <= ?", (op_id - KEEP_OPS,))
                self.db.execute("DELETE FROM ops WHERE op <= ?", (op_id - KEEP_OPS,))
            self.db.commit()            # the one commit of the whole operation
            self.read_fp = ""            # what was read before is not what is stored now: the next all() takes a new reading
        except BaseException:
            self.db.rollback()
            raise

    def _compact(self, before: dict) -> list:
        """Journal entries ``(id, json|None)``: only the columns that changed, the whole row if it was deleted, nothing if unchanged."""
        now: dict = {}
        ids = list(before)
        for chunk in _chunks(ids):
            for r in self.db.execute(f"SELECT * FROM bookmarks WHERE id IN ({_marks(len(chunk))})", chunk):
                now[r["id"]] = dict(r)
        out: list = []
        for i, old in before.items():
            cur = now.get(i)
            if old is None:
                if cur is not None:
                    out.append((i, None))  # created by this operation
            elif cur is None:
                out.append((i, json.dumps({"row": old})))
            else:
                diff = {c: old.get(c) for c in COLS if c != "id" and old.get(c) != cur.get(c)}
                if diff:
                    out.append((i, json.dumps({"diff": diff})))
        return out

    def history(self, limit: int = 20) -> list:
        return [dict(r) for r in self.db.execute("SELECT * FROM ops ORDER BY op DESC LIMIT ?", (limit,))]

    def undo(self, op_id: int = 0) -> dict:
        with self._write():     # two processes undoing at once must not both apply the same operation
            self._joined = True  # rows, index, journal mark and folder list commit together (or an interruption leaves none of it)
            try:
                return self._undo(op_id)
            finally:
                self._joined = False

    def _undo(self, op_id: int) -> dict:
        row = (self.db.execute("SELECT * FROM ops WHERE op=?", (op_id,)).fetchone() if op_id else
               self.db.execute("SELECT * FROM ops WHERE undone=0 ORDER BY op DESC LIMIT 1").fetchone())
        if row is None:
            raise ValueError("nothing to undo")
        if row["undone"]:
            raise ValueError(f"operation {row['op']} was already undone")
        latest = self.db.execute("SELECT MAX(op) FROM ops WHERE undone=0").fetchone()[0] == row["op"]
        entries = self.db.execute("SELECT bm_id, before FROM journal WHERE op=?", (row["op"],)).fetchall()
        restore, patch, remove = [], [], []
        for e in entries:
            if e["before"] is None:
                remove.append(e["bm_id"])
                continue
            d = json.loads(e["before"])
            if "diff" in d:
                patch.append((e["bm_id"], d["diff"]))
            else:  # whole rows (deleted bookmarks), or the older journal format that stored the row directly
                restore.append(d.get("row", d))
        sets = ",".join(f"{c}=excluded.{c}" for c in COLS if c != "id")
        if self.fts and remove:
            for chunk in _chunks(remove):
                self.db.execute(f"DELETE FROM bm_fts WHERE rowid IN (SELECT rowid FROM bookmarks WHERE id IN ({_marks(len(chunk))}))", chunk)
        with self._tx():
            for chunk in _chunks(remove):
                self.db.execute(f"DELETE FROM bookmarks WHERE id IN ({_marks(len(chunk))})", chunk)
            self.db.executemany(
                f"INSERT INTO bookmarks({','.join(COLS)}) VALUES({_marks(len(COLS))}) ON CONFLICT(id) DO UPDATE SET {sets}",
                [tuple(r.get(c) for c in COLS) for r in restore])
            for bid, diff in patch:
                cols = [c for c in diff if c in COLS and c != "id"]
                if cols:
                    self.db.execute(f"UPDATE bookmarks SET {','.join(c + '=?' for c in cols)} WHERE id=?", [diff[c] for c in cols] + [bid])
            self.db.execute("UPDATE ops SET undone=1 WHERE op=?", (row["op"],))
            # an undone decision is no decision: forget its feedback and let the suggestion be offered again
            self.db.execute("UPDATE suggestions SET status='open' WHERE id IN (SELECT suggestion FROM feedback WHERE op=? AND suggestion IS NOT NULL)",
                            (row["op"],))
            self.db.execute("DELETE FROM feedback WHERE op=?", (row["op"],))
        self._reindex([r["id"] for r in restore] + [i for i, _ in patch])
        if row["folders"] and latest:  # exact folder set (empty folders included) as it was before the operation
            self.set_folders(json.loads(row["folders"]))
        return {"op": row["op"], "command": row["command"], "restored": len(restore) + len(patch), "removed": len(remove)}

    # ---------------------------------------------------------- feedback
    def record_suggestions(self, rows) -> int:
        """Remember what was suggested (``rows``: dicts with bm_id, norm, folder, level, confidence, source).

        An older open suggestion for the same bookmark is superseded: only the latest one can be accepted or rejected.
        """
        rows = [r for r in rows if r.get("bm_id") and r.get("folder") is not None]
        now = int(time.time())
        with self._tx():
            for chunk in _chunks([r["bm_id"] for r in rows]):
                self.db.execute(f"UPDATE suggestions SET status='superseded' WHERE status='open' AND bm_id IN ({_marks(len(chunk))})", chunk)
            self.db.executemany(
                "INSERT INTO suggestions(ts, bm_id, norm, folder, level, confidence, source) VALUES(?,?,?,?,?,?,?)",
                [(now, r["bm_id"], r.get("norm", ""), r["folder"], r.get("level", "exact"), r.get("confidence"), r.get("source", ""))
                 for r in rows])
        return len(rows)

    def open_suggestion(self, bm_id: str):
        r = self.db.execute("SELECT * FROM suggestions WHERE bm_id=? AND status='open' ORDER BY id DESC LIMIT 1", (bm_id,)).fetchone()
        return dict(r) if r else None

    def add_feedback(self, *, bm_id: str, norm: str, host: str, suggested: str, final: str, action: str, confidence=None,
                     level: str = "exact", source: str = "", channel: str = "", explicit: bool = False, suggestion=None) -> None:
        """One decision about one suggestion: ``accepted`` / ``corrected`` (the user filed it elsewhere) / ``rejected``."""
        with self._tx():
            self.db.execute(
                "INSERT INTO feedback(ts, bm_id, norm, host, suggested, final, action, confidence, level, source, channel, explicit,"
                " suggestion, op_token) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(time.time()), bm_id, norm, host, suggested, final, action, confidence, level, source, channel, 1 if explicit else 0,
                 suggestion, self._op_token))
            if suggestion is not None:
                self.db.execute("UPDATE suggestions SET status='closed' WHERE id=?", (suggestion,))

    def feedback_rows(self, explicit_only: bool = False, limit: int = 0) -> list:
        sql = "SELECT * FROM feedback" + (" WHERE explicit=1" if explicit_only else "") + " ORDER BY id DESC" + (f" LIMIT {int(limit)}" if limit else "")
        return [dict(r) for r in self.db.execute(sql)]

    def clear_feedback(self) -> int:
        with self._tx():
            n = self.db.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
            self.db.execute("DELETE FROM feedback")
            self.db.execute("DELETE FROM suggestions")
        return n

    # --------------------------------------------------------- snapshots
    def snapshot(self, label: str) -> Path:
        d = self.root / "backups"
        d.mkdir(exist_ok=True)
        path = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}.json"
        n = 1
        while path.exists():
            path = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}-{n}.json"
            n += 1
        rows = [dict(r) for r in self.db.execute("SELECT * FROM bookmarks")]
        pages = {r[0]: r[1] for r in self.db.execute("SELECT id, content FROM pages")}
        path.write_text(json.dumps({"bookmarks": rows, "pages": pages, "folders": sorted(self.folders())},
                                   ensure_ascii=False), encoding="utf-8")
        return path

    def restore(self, path) -> int:
        """Replace the library with a snapshot written by ``snapshot()`` (``backups --restore``). The file is checked first and the replacement
        is ONE transaction, so a wrong or damaged file changes nothing; a copy of the current library is saved before."""
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        rows = data.get("bookmarks") if isinstance(data, dict) else data
        pages = data.get("pages", {}) if isinstance(data, dict) else {}
        folders = data.get("folders", []) if isinstance(data, dict) else []
        problem = self._snapshot_problem(rows, pages, folders)
        if problem or not isinstance(rows, list):
            raise ValueError(f"{path} is not a library snapshot: {problem}. (Files made by `export` are not snapshots: bring those in with `import`.)")
        self.snapshot("pre-restore")
        bms = []
        for r in rows:
            r = dict(r)
            r.setdefault("content", "")
            bms.append(self._bm(_Row(r)))
        with self._write():
            self._joined = True
            try:
                self.replace_all(bms)
                self.db.executemany("INSERT OR REPLACE INTO pages(id, content) VALUES(?,?)", list(pages.items()))
                self.reindex_all()
                self.set_folders(folders)
            finally:
                self._joined = False
        return len(rows)

    @staticmethod
    def _snapshot_problem(rows, pages, folders) -> str:
        if not isinstance(rows, list):
            return "no list of bookmarks"
        if not isinstance(pages, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in pages.items()):
            return "the page texts are not a table of text"
        if not isinstance(folders, list) or not all(isinstance(f, str) for f in folders):
            return "the folder list is not a list of text"
        ints = ("added", "http_code", "last_checked", "fail_count", "first_failed", "favorite")
        for n, r in enumerate(rows):
            if not isinstance(r, dict):
                return f"entry {n} is not an object"
            for key in ("id", "url", "norm"):
                if not isinstance(r.get(key), str) or (key != "norm" and not r[key]):
                    return f"entry {n} has no {key!r} (a snapshot keeps every bookmark's id and normalised URL)"
            for key in ints:
                if r.get(key) is not None and (not isinstance(r[key], int) or isinstance(r[key], bool)):
                    return f"entry {n}: {key!r} is not a number"
        return ""

    def snapshots(self) -> list:
        d = self.root / "backups"
        return sorted(d.glob("*.json")) if d.exists() else []


class _Row(dict):
    """dict that quacks like sqlite3.Row for ``_bm``."""

    def __getitem__(self, k):
        return self.get(k)
