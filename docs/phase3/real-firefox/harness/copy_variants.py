"""The pre-fix way of copying a live places.sqlite (database, then -wal and -shm, no verification), instrumented, and a switch that
widens the copy window by copying in slow chunks (slow disk, antivirus scan, network drive).  Used by live_test.py's stress mode to
compare it with whatever `mhbm.browsers._copy` does now."""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

STATS = {"copies": 0, "changed": 0, "wal_reset": 0, "db_changed": 0}
_real_copy2 = shutil.copy2


def fingerprint(path: Path):
    """(db mtime_ns, db size, first 32 bytes of the -wal): the WAL header changes (salts, checkpoint sequence) when SQLite restarts the WAL."""
    st = path.stat()
    try:
        with open(path.with_name(path.name + "-wal"), "rb") as f:
            head = f.read(32)
    except FileNotFoundError:
        head = b""
    return st.st_mtime_ns, st.st_size, head


def instrumented_naive(path: Path) -> Path:
    """The old algorithm, byte for byte, plus counters for how often the files moved while it copied."""
    tmp = Path(tempfile.mkdtemp(prefix="mhbm-"))
    before = fingerprint(path)
    dest = tmp / path.name
    shutil.copy2(path, dest)
    for ext in ("-wal", "-shm"):
        side = path.with_name(path.name + ext)
        if side.exists():
            shutil.copy2(side, tmp / side.name)
    after = fingerprint(path)
    STATS["copies"] += 1
    if before != after:
        STATS["changed"] += 1
        STATS["wal_reset"] += before[2][:24] != after[2][:24]
        STATS["db_changed"] += before[:2] != after[:2]
    return dest


def install_slow(chunk: int, pause: float) -> None:
    """Make shutil.copy2 copy `chunk` bytes at a time with `pause` seconds in between (for every caller in this process)."""
    def slow_copy2(src, dst, *a, **k):
        target = Path(dst) / Path(src).name if Path(dst).is_dir() else Path(dst)
        with open(src, "rb") as r, open(target, "wb") as w:
            while True:
                block = r.read(chunk)
                if not block:
                    break
                w.write(block)
                time.sleep(pause)
        return str(target)

    shutil.copy2 = slow_copy2


def uninstall_slow() -> None:
    shutil.copy2 = _real_copy2
