"""Writing into a browser profile without hurting it: is the browser running, atomic writes, backups that never overwrite each other.

A Chromium-family browser keeps ``Bookmarks`` in memory and rewrites the file when it saves, so anything written while it
runs is silently undone (and the library would then believe the write happened). These helpers are what ``install-chrome``
and ``sync`` rely on:

* ``chromium_lock``   - is a browser running on this profile? (``SingletonLock`` on Linux/macOS, ``lockfile`` on Windows)
* ``preflight``       - everything worth knowing before writing (Chrome Sync, an account file, read-only mount, odd content)
* ``atomic_write_text`` - temp file + fsync + rename, keeping permissions and owner, writing *through* a symlink
* ``backup_file`` / ``list_backups`` / ``restore_backup`` - ``Bookmarks.mhbm-backup-<time>[-n]`` files, never overwritten
* ``fingerprint`` / ``assert_unchanged`` - notice that the browser touched the file while we were working
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

BACKUP_TAG = ".mhbm-backup-"
TMP_SUFFIX = ".mhbm-tmp"
TWIN = "EncryptedBookmarks2"          # a second copy some Chromium builds keep (encrypted); if it survives, the browser may prefer it
_STAMP = re.compile(r"(\d{8}-\d{6})(?:-(\d+))?$")


class ProfileError(ValueError):
    """Something that makes writing to the profile unsafe or impossible; the message says what to do."""


# ------------------------------------------------------------------ is the browser running?
@dataclass(frozen=True)
class LockState:
    state: str        # free | running | stale | unknown
    detail: str = ""

    @property
    def blocks(self) -> bool:
        """``running`` and ``unknown`` both stop a write: when we cannot tell, we do not gamble with the profile."""
        return self.state in ("running", "unknown")


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":      # os.kill(pid, 0) would terminate the process on Windows
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _sandboxed(path: Path) -> bool:
    parts = {p.lower() for p in path.parts}
    return ".var" in parts or "snap" in parts


def _posix_lock(directory: Path) -> LockState | None:
    lock = directory / "SingletonLock"
    if not os.path.lexists(lock):
        return None
    if not lock.is_symlink():
        return LockState("unknown", f"{lock} exists but is not the link a browser leaves")
    match = re.fullmatch(r"(.+)-(\d+)", os.readlink(lock))
    if not match:
        return LockState("unknown", f"{lock} points at an unexpected value")
    host, pid = match.group(1), int(match.group(2))
    if host != socket.gethostname():
        return LockState("running", f"{lock.name} was made on another computer ({host}); mhbm cannot tell whether that browser is still open")
    if _sandboxed(directory):
        return LockState("running", f"{lock.name} names process {pid}, inside a sandbox (Snap/Flatpak) where it cannot be checked")
    if _pid_alive(pid):
        return LockState("running", f"process {pid} holds {lock.name}")
    return LockState("stale", f"{lock.name} was left by process {pid}, which no longer exists (a crash)")


def _windows_lock(directory: Path) -> LockState | None:
    lock = directory / "lockfile"
    if not lock.exists():
        return None
    try:
        with open(lock, "r+b"):
            pass
    except PermissionError:
        return LockState("running", f"{lock} is held open by a browser")
    except OSError:
        return LockState("unknown", f"{lock} could not be checked")
    return LockState("stale", f"{lock} exists but nothing holds it")


def chromium_lock(target: Path) -> LockState:
    """Whether a Chromium-family browser is running on the profile that owns ``target`` (a ``Bookmarks`` file).

    The lock lives in the user-data folder (the parent of ``Default``/``Profile N``) or, for Opera, beside the bookmarks. The
    folder two levels up is only consulted when it has a ``Local State`` file, i.e. when it really is a browser's folder."""
    target = Path(target)
    folders = [target.parent]
    if (target.parent.parent / "Local State").is_file():
        folders.append(target.parent.parent)
    states = [s for d in folders for s in (_posix_lock(d), _windows_lock(d)) if s is not None]
    for wanted in ("running", "unknown", "stale"):
        for s in states:
            if s.state == wanted:
                return s
    return LockState("free")


# ------------------------------------------------------------------ before writing
@dataclass
class Preflight:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    synced: bool = False
    account_file: bool = False
    twins: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _read_only_mount(path: Path) -> bool:
    try:
        return bool(os.statvfs(path.parent).f_flag & os.ST_RDONLY)
    except (AttributeError, OSError):
        return False


def _odd_content(data: dict) -> list:
    extra = []
    raw = data.get("roots")
    roots: dict = raw if isinstance(raw, dict) else {}
    unusual = sorted(set(roots) - {"bookmark_bar", "other", "synced"})
    if unusual:
        extra.append(f"the file has roots mhbm does not know ({', '.join(unusual)}); they are not carried over when it is rewritten")
    count = 0

    def walk(node):
        nonlocal count
        if isinstance(node, dict):
            if node.get("meta_info"):
                count += 1
            for child in node.get("children", []) or []:
                walk(child)
    for node in roots.values():
        walk(node)
    if count:
        extra.append(f"{count} bookmark(s) carry meta_info (nicknames, power-bookmark data) that is not kept when the file is rewritten")
    return extra


def preflight(target: Path, *, allow_synced: bool = False, ignore_lock: bool = False, rewrites_everything: bool = False) -> Preflight:
    """Collect what must stop a write (``errors``) and what the person should know (``warnings``)."""
    target = Path(target)
    pf = Preflight()
    lock = chromium_lock(target)
    if lock.blocks and not ignore_lock:
        pf.errors.append(f"Close the browser completely first (also its background processes): {lock.detail}. "
                         "A running browser rewrites this file when it saves and would undo the write. "
                         "Use --ignore-profile-lock only if you are sure it is closed.")
    elif lock.state == "stale":
        pf.warnings.append(lock.detail)
    if _read_only_mount(target):
        pf.errors.append(f"{target.parent} is on a read-only file system")
    elif not os.access(target if target.exists() else target.parent, os.W_OK):
        pf.errors.append(f"no permission to write {target}")
    data = None
    if target.exists():
        try:
            data = json.loads(target.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            pf.warnings.append(f"{target.name} is not valid JSON (the browser would not read it either); it will be replaced")
    if isinstance(data, dict):
        pf.synced = "sync_metadata" in data
        if pf.synced:
            message = ("Chrome Sync is on for this profile (sync_metadata in the file): the sync engine can restore the old bookmarks over "
                       "what is written here")
            if allow_synced:
                pf.warnings.append(message + "; continuing because --allow-synced-profile was given")
            else:
                pf.errors.append(message + ". Turn Sync off for bookmarks first, or pass --allow-synced-profile.")
        pf.warnings += _odd_content(data)
    neighbour = target.with_name("AccountBookmarks")
    if neighbour.exists():
        pf.account_file = True
        pf.warnings.append(f"{neighbour.name} exists: bookmarks of the signed-in account live there and are not touched by this write")
    pf.twins = [p for p in (target.with_name(TWIN),) if p.exists()]
    for twin in pf.twins:
        pf.warnings.append(f"{twin.name} exists beside {target.name}: some Chromium builds prefer it, so it is moved aside "
                           "(kept under a backup name) when the file is written")
    if rewrites_everything:
        pf.warnings.append("the whole file is regenerated from the library: ids and GUIDs change and the browser's own order is not kept")
    return pf


# ------------------------------------------------------------------ writing
def win_extended(path: str) -> str:
    """The ``\\\\?\\`` form of an absolute Windows path that is too long for the classic limit; anything else is returned as is."""
    if path.startswith("\\\\?\\") or len(path) < 248:
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    if re.match(r"^[A-Za-z]:\\", path):
        return "\\\\?\\" + path
    return path


def _chown_like(path, st) -> None:
    """Give ``path`` the owner of the file it replaces (when run as root or via sudo, a new file would otherwise belong to root)."""
    if hasattr(os, "chown"):
        try:
            os.chown(path, st.st_uid, st.st_gid)
        except OSError:
            pass


def _fsync_dir(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so that a reader sees the old file or the new one, never half of it.

    Temp file in the same folder, flushed and fsynced, then renamed over the target (and the folder fsynced). The target's mode
    and owner are kept; a new file is private (0600). A symlink is followed: the file it points at is replaced and the link stays."""
    path = Path(os.path.realpath(path))
    existing = os.stat(path) if path.exists() else None
    name = win_extended(str(path)) if os.name == "nt" else str(path)
    tmp = name + TMP_SUFFIX
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, (existing.st_mode & 0o777) if existing else 0o600)
        if existing is not None:
            _chown_like(tmp, existing)
        os.replace(tmp, name)
    except PermissionError as e:
        _discard(tmp)
        raise ProfileError(f"cannot replace {path.name}: the browser (or a virus scanner or sync client) holds it open, or you have no "
                           f"permission ({e.strerror or e})") from e
    except BaseException:
        _discard(tmp)
        raise
    _fsync_dir(path.parent)


def _discard(name) -> None:
    try:
        os.unlink(name)
    except OSError:
        pass


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _free_name(path: Path, stamp: str) -> Path:
    candidate = path.with_name(f"{path.name}{BACKUP_TAG}{stamp}")
    n = 1
    while os.path.lexists(candidate):
        n += 1
        candidate = path.with_name(f"{path.name}{BACKUP_TAG}{stamp}-{n}")
    return candidate


PROFILE_FILES = ("Bookmarks", "Bookmarks.bak", "AccountBookmarks", "EncryptedBookmarks2", "places.sqlite")


def looks_like_browser_profile_file(path: Path) -> bool:
    """True when writing ``path`` would write into a browser profile: a Chromium ``Bookmarks`` file next to ``Local State`` (or inside a profile
    folder below it), a Firefox ``places.sqlite`` next to ``prefs.js``, or any file called ``Bookmarks`` that already holds a bookmark tree."""
    p = Path(os.path.realpath(path))
    if p.name not in PROFILE_FILES:
        return False
    if (p.parent / "Local State").exists() or (p.parent.parent / "Local State").exists() or (p.parent / "prefs.js").exists():
        return True
    if p.name.startswith("Bookmarks") and p.is_file():
        try:
            return '"roots"' in p.read_text(encoding="utf-8-sig", errors="replace")[:200_000]
        except OSError:
            return True
    return False


def backup_file(path: Path, stamp: str | None = None) -> Path:
    """Copy ``path`` to ``<name>.mhbm-backup-<time>`` (``-2``, ``-3`` ... when that name is taken); an existing backup is never overwritten.

    The copy is written under a temporary name, flushed to disk, and only then given its backup name: a crash or a full disk never leaves a
    truncated file that looks like a backup (and that a later ``--restore`` would put in place of the real bookmarks)."""
    path = Path(os.path.realpath(path))
    dest = _free_name(path, stamp or _stamp())
    tmp = dest.with_name(dest.name + TMP_SUFFIX)
    try:
        shutil.copy2(path, tmp)
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        try:
            os.link(tmp, dest)                  # fails when the name exists: a backup is never overwritten
        except FileExistsError:
            raise
        except OSError:                         # a file system without hard links
            if os.path.lexists(dest):
                raise
            os.replace(tmp, dest)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    _chown_like(dest, os.stat(path))
    _fsync_dir(dest.parent)
    return dest


def _backup_key(path: Path, base: str):
    match = _STAMP.fullmatch(path.name[len(base) + len(BACKUP_TAG):])
    return (match.group(1), int(match.group(2) or 1)) if match else None


def list_backups(path: Path) -> list:
    """The backups ``backup_file`` made for ``path``, oldest first."""
    path = Path(path)
    found = [(key, p) for p in path.parent.glob(f"{path.name}{BACKUP_TAG}*") if (key := _backup_key(p, path.name)) is not None]
    return [p for _, p in sorted(found)]


def fingerprint(path: Path) -> tuple:
    """Enough to notice that someone else wrote the file: size, modification time and a digest of the content."""
    path = Path(path)
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns, hashlib.sha1(path.read_bytes()).hexdigest()


def assert_unchanged(path: Path, before: tuple) -> None:
    if fingerprint(path) != before:
        raise ProfileError(f"{Path(path).name} was changed by something else (the browser?) while mhbm was working on it; "
                           "nothing was written. Close the browser and run the command again.")


def write_profile_file(target: Path, text: str) -> dict:
    """Back up ``target``, move a twin file aside, and write ``text`` atomically. Returns ``{"backup", "moved_aside"}``."""
    target = Path(os.path.realpath(target))
    backup = backup_file(target) if target.exists() else None
    suffix = backup.name[len(target.name):] if backup else f"{BACKUP_TAG}{_stamp()}"
    moved = []
    for twin in (target.with_name(TWIN),):
        if twin.exists():
            dest = twin.with_name(twin.name + suffix)
            os.replace(twin, dest)
            moved.append(dest)
    atomic_write_text(target, text)
    return {"backup": backup, "moved_aside": moved}


def restore_backup(target: Path, backup: Path | None = None) -> dict:
    """Put a backup back as ``target`` (the newest one unless ``backup`` is given); what it replaces is saved first."""
    target = Path(os.path.realpath(target))
    available = list_backups(target)
    if backup is None:
        if not available:
            raise ProfileError(f"no mhbm backup of {target.name} found in {target.parent}")
        backup = available[-1]
    backup = Path(backup)
    if backup not in available and backup.resolve() not in {p.resolve() for p in available}:
        raise ProfileError(f"{backup} is not a backup that mhbm made of {target}")
    text = backup.read_text(encoding="utf-8")
    try:
        document = json.loads(text.lstrip("\ufeff"))
    except (ValueError, RecursionError) as e:
        raise ProfileError(f"{backup.name} is not a valid Bookmarks file ({e}); it was not restored") from e
    if not isinstance(document, dict) or not isinstance(document.get("roots"), dict) or not document["roots"]:
        raise ProfileError(f"{backup.name} has no bookmark roots (empty or not a Bookmarks file); it was not restored. The file in the profile was not touched.")
    previous = backup_file(target) if target.exists() else None
    suffix = backup.name[len(target.name):]
    restored = []
    for twin in (target.with_name(TWIN),):
        aside = twin.with_name(twin.name + suffix)
        if aside.exists():
            if twin.exists():
                os.replace(twin, _free_name(twin, _stamp()))   # the one the browser made since: kept under a backup name
            os.replace(aside, twin)
            restored.append(twin)
    atomic_write_text(target, text)
    return {"restored": backup, "previous_saved_as": previous, "twins_restored": restored}
