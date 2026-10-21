import shutil, sqlite3, sys, tempfile
from pathlib import Path
with tempfile.TemporaryDirectory() as d:
    d = Path(d); db = d / "p.sqlite"
    w = sqlite3.connect(db, isolation_level=None); w.execute("PRAGMA journal_mode=WAL"); w.execute("PRAGMA wal_autocheckpoint=0")
    w.execute("CREATE TABLE t(x)"); w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for i in range(50): w.execute("INSERT INTO t VALUES (?)", (i,))
    wal = d / "p.sqlite-wal"; print("wal bytes before copy:", wal.stat().st_size)
    t = d / "t3"; t.mkdir(); shutil.copy2(db, t / "p.sqlite")          # old db (50 rows only in WAL)
    r = w.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall(); print("checkpoint result (busy, log, checkpointed):", r, "| wal bytes now:", wal.stat().st_size)
    w.execute("INSERT INTO t VALUES (999)")
    print("wal bytes after one more insert:", wal.stat().st_size)
    shutil.copy2(wal, t / "p.sqlite-wal")
    c = sqlite3.connect(f"file:{t/'p.sqlite'}?mode=ro", uri=True)
    print("rows visible in torn copy:", c.execute("select count(*) from t").fetchone(), "(old db had 0 rows; new wal holds 1 row)")
    print("integrity:", c.execute("pragma quick_check").fetchall())
