"""Does copying a LIVE WAL-mode places.sqlite (db + -wal [+ -shm]) give a readable, current snapshot?  Writer connection stays open (like a running Firefox)."""
import os, shutil, sqlite3, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from mhbm import browsers

with tempfile.TemporaryDirectory() as d:
    d = Path(d); prof = d / "prof"; prof.mkdir()
    db = prof / "places.sqlite"
    w = sqlite3.connect(db, isolation_level=None)
    w.execute("PRAGMA journal_mode=WAL"); w.execute("PRAGMA wal_autocheckpoint=0")
    w.executescript("CREATE TABLE moz_places(id INTEGER PRIMARY KEY, url TEXT); CREATE TABLE moz_bookmarks(id INTEGER PRIMARY KEY, type INT, fk INT, parent INT, title TEXT, dateAdded INT, guid TEXT);"
                    "INSERT INTO moz_bookmarks VALUES (1,2,NULL,0,'','0','root________'),(2,2,NULL,1,'unfiled','0','unfiled_____');")
    w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for i in range(50):    # these rows live only in the -wal
        w.execute("INSERT INTO moz_places VALUES (?,?)", (i + 1, f"https://hot{i}.example/"))
        w.execute("INSERT INTO moz_bookmarks VALUES (?,1,?,2,'t',1600000000000000,?)", (i + 10, i + 1, f"g{i}"))
    print("files while 'Firefox' runs:", sorted(p.name for p in prof.iterdir()), "| db size", db.stat().st_size, "wal size", (prof / "places.sqlite-wal").stat().st_size)
    # 1. current code path
    got = browsers.load("firefox", profile=str(db))
    print("browsers.load on hot db (copies db+wal+shm):", len(got), "bookmarks")
    # 2. db only (what a naive copy would do)
    t = d / "naive"; t.mkdir(); shutil.copy2(db, t / "places.sqlite")
    print("naive copy of places.sqlite only:", len(browsers.parse_places(t / "places.sqlite")), "bookmarks (uncheckpointed rows lost)")
    # 3. db + wal, no shm
    t2 = d / "nowshm"; t2.mkdir(); shutil.copy2(db, t2 / "places.sqlite"); shutil.copy2(prof / "places.sqlite-wal", t2 / "places.sqlite-wal")
    print("db + wal without -shm:", len(browsers.parse_places(t2 / "places.sqlite")), "bookmarks")
    # 4. torn: copy db, writer checkpoints (TRUNCATE) and writes more, then copy wal
    t3 = d / "torn"; t3.mkdir(); shutil.copy2(db, t3 / "places.sqlite")
    w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    w.execute("INSERT INTO moz_places VALUES (999,'https://after-checkpoint.example/')"); w.execute("INSERT INTO moz_bookmarks VALUES (999,1,999,2,'t',1600000000000000,'gx')")
    shutil.copy2(prof / "places.sqlite-wal", t3 / "places.sqlite-wal")
    try:
        r = browsers.parse_places(t3 / "places.sqlite"); print("torn copy (old db + newer wal):", len(r), "bookmarks (expected 50 old + maybe the post-checkpoint row; no error)")
    except Exception as e:
        print("torn copy error:", type(e).__name__, e)
    # 5. recommended: sqlite online backup via ro URI on the live DB
    try:
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True); dst = sqlite3.connect(d / "backup.sqlite"); src.backup(dst); dst.close(); src.close()
        print("online backup API from the live db:", len(browsers.parse_places(d / "backup.sqlite")), "bookmarks (consistent, includes WAL)")
    except Exception as e:
        print("online backup error:", type(e).__name__, e)
