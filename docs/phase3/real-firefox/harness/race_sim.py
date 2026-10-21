# Deterministic simulation of "Firefox checkpoints+truncates its WAL between our copy of the db and our copy of the WAL".
import os, shutil, sqlite3, sys, tempfile
from pathlib import Path
from unittest import mock
tree = sys.argv[1]
sys.dont_write_bytecode = True
sys.path.insert(0, tree + "/scripts")
from mhbm import browsers

d = Path(tempfile.mkdtemp()); db = d / "places.sqlite"
live = sqlite3.connect(db)
live.execute("PRAGMA page_size=1024"); live.execute("PRAGMA journal_mode=WAL"); live.execute("PRAGMA wal_autocheckpoint=0")
live.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)"); live.execute("CREATE INDEX tv ON t(v)")
def add(lo, hi):
    live.executemany("INSERT INTO t(v) VALUES (?)", [(f"row-{i:05d}-" + "x" * 80,) for i in range(lo, hi)]); live.commit()
add(0, 1500)
live.execute("PRAGMA wal_checkpoint(TRUNCATE)")      # steady state: everything checkpointed once
add(1500, 3000)                                       # a burst of writes still sitting in the WAL
real_copy2 = shutil.copy2
state = {"n": 0}
def racing_copy2(src, dst, *a, **k):
    r = real_copy2(src, dst, *a, **k)
    if str(src).endswith("places.sqlite") and state["n"] == 0:   # the browser moves on right after we copied the db
        state["n"] += 1
        live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        add(3000, 3600)
    return r
with mock.patch.object(shutil, "copy2", racing_copy2):
    dest = browsers._copy(db)
c = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
try:
    n = c.execute("select count(*) from t").fetchone()[0]
    integ = c.execute("pragma integrity_check").fetchall()
except sqlite3.DatabaseError as e:
    n, integ = None, [(str(e),)]
c.close(); live.close()
total = 3600
print(f"{os.path.basename(tree)}: copy has {n} of {total} rows; integrity_check -> {integ[0][0][:70]!r}; consistent={n in (3000, 3600) and integ == [('ok',)]}")
shutil.rmtree(d, ignore_errors=True); shutil.rmtree(dest.parent, ignore_errors=True)
