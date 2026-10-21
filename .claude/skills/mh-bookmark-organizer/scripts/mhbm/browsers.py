"""Read bookmarks straight from installed browsers (read-only; profile files are copied first)."""
from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from xml.parsers.expat import ExpatError

from . import formats, gecko
from .model import Bookmark, esc_name

CHROMIUM = ("chrome", "chromium", "brave", "edge", "vivaldi", "opera", "opera-gx", "yandex")
GECKO = gecko.NAMES  # firefox, librewolf, waterfox, floorp, zen: same places.sqlite / jsonlz4 formats, found through profiles.ini
ALL = CHROMIUM + GECKO + ("safari",)
UNSUPPORTED = {"arc": "Arc does not keep its bookmarks in a Chromium `Bookmarks` file (they are in StorableSidebar.json, in a private format). "
                      "Export them from Arc, or from another browser, to an HTML file and use `import FILE`."}
WSL_MOUNT_ROOT = Path("/mnt")
SKIPPED_PROFILE_DIRS = {"Guest Profile", "System Profile"}
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_NOT_PEOPLE = {"public", "default", "default user", "all users"}


@dataclass
class Profile:
    """One browser profile: where its bookmarks are, what it is called, and what is worth telling the person."""
    browser: str
    dir: Path
    name: str = ""
    files: tuple = ()          # bookmark stores to read, in order (Chromium: Bookmarks, AccountBookmarks; Gecko: places.sqlite; Safari: the plist)
    fallbacks: tuple = ()      # read only when `files` is empty (Bookmarks.bak; Firefox's newest automatic backup)
    notes: tuple = ()          # why a profile cannot be read, or what to know about it
    default: bool = False      # the profile the browser would open (last used / the installation's default)
    signed_in: bool = False

    @property
    def sources(self) -> tuple:
        return self.files or self.fallbacks


def scrub(text) -> str:
    """Profile names are often the account's e-mail address; never show or log one."""
    return _EMAIL.sub("(account)", str(text or "")).strip()


def _is_wsl() -> bool:
    return platform.system() == "Linux" and bool(os.environ.get("WSL_DISTRO_NAME") or "microsoft" in platform.release().lower())


def _sudo_home():
    """Under ``sudo`` HOME is root's; the person's own home is where their browsers are."""
    user = os.environ.get("SUDO_USER")
    if not user or user == "root":
        return None
    try:
        import pwd

        home = Path(pwd.getpwnam(user).pw_dir)
    except (ImportError, KeyError):
        return None
    return home if home != Path.home() else None


def _homes() -> list:
    return [Path.home()] + ([h] if (h := _sudo_home()) else [])


def _abs_env(name: str):
    value = os.environ.get(name, "")
    return Path(value) if value and Path(value).is_absolute() else None


def _linux_table(home: Path, name: str) -> list:
    cfg = (_abs_env("CHROME_CONFIG_HOME") if name == "chrome" else None) or _abs_env("XDG_CONFIG_HOME") or home / ".config"
    flat = home / ".var/app"
    brave = "BraveSoftware/Brave-Browser"
    table = {
        "chrome": [cfg / "google-chrome", cfg / "google-chrome-beta", cfg / "google-chrome-unstable", flat / "com.google.Chrome/config/google-chrome"],
        "chromium": [cfg / "chromium", home / "snap/chromium/common/chromium", flat / "org.chromium.Chromium/config/chromium"],
        "brave": [cfg / brave, cfg / (brave + "-Beta"), cfg / (brave + "-Dev"), cfg / (brave + "-Nightly"),
                  home / "snap/brave/current/.config" / brave, flat / "com.brave.Browser/config" / brave],
        "edge": [cfg / "microsoft-edge", cfg / "microsoft-edge-beta", cfg / "microsoft-edge-dev", flat / "com.microsoft.Edge/config/microsoft-edge"],
        "vivaldi": [cfg / "vivaldi", cfg / "vivaldi-snapshot", flat / "com.vivaldi.Vivaldi/config/vivaldi"],
        "opera": [cfg / "opera", cfg / "opera-beta", cfg / "opera-developer", flat / "com.opera.Opera/config/opera"],
        "yandex": [cfg / "yandex-browser", cfg / "yandex-browser-beta"],
    }
    return table.get(name, [])


def _mac_table(home: Path, name: str) -> list:
    app = home / "Library/Application Support"
    table = {
        "chrome": [app / f"Google/{d}" for d in ("Chrome", "Chrome Beta", "Chrome Dev", "Chrome Canary")],
        "chromium": [app / "Chromium"],
        "brave": [app / f"BraveSoftware/Brave-Browser{s}" for s in ("", "-Beta", "-Dev", "-Nightly")],
        "edge": [app / f"Microsoft Edge{s}" for s in ("", " Beta", " Dev", " Canary")],
        "vivaldi": [app / "Vivaldi"], "opera": [app / "com.operasoftware.Opera"], "opera-gx": [app / "com.operasoftware.OperaGX"],
        "yandex": [app / "Yandex/YandexBrowser"],
    }
    return table.get(name, [])


def _windows_table(local: Path, roam: Path, name: str) -> list:
    table = {
        "chrome": [local / f"Google/{d}/User Data" for d in ("Chrome", "Chrome Beta", "Chrome Dev", "Chrome SxS")],
        "chromium": [local / "Chromium/User Data"],
        "brave": [local / f"BraveSoftware/Brave-Browser{s}/User Data" for s in ("", "-Beta", "-Dev", "-Nightly")],
        "edge": [local / f"Microsoft/Edge{s}/User Data" for s in ("", " Beta", " Dev", " SxS")],
        "vivaldi": [local / "Vivaldi/User Data"], "yandex": [local / "Yandex/YandexBrowser/User Data"],
        "opera": [roam / f"Opera Software/{d}" for d in ("Opera Stable", "Opera Next", "Opera Developer")],
        "opera-gx": [roam / "Opera Software/Opera GX Stable"],
    }
    return table.get(name, [])


def _wsl_windows_homes() -> list:
    """(Local, Roaming) application-data folders of the people whose Windows drives are mounted into this WSL - this Linux user's namesake
    first (the usual owner of the browser they are running), then the others in alphabetical order."""
    out: list = []
    try:
        drives = sorted(WSL_MOUNT_ROOT.iterdir())
    except OSError:
        return out
    me = {os.environ.get(v, "").lower() for v in ("USER", "LOGNAME", "USERNAME")} - {""}
    for drive in drives:
        users = drive / "Users"
        try:
            people = sorted(users.iterdir(), key=lambda u: (u.name.lower() not in me, u.name.lower())) if users.is_dir() else []
        except OSError:
            continue
        for u in people:
            try:                                    # one folder this user may not enter must not hide everybody else's browsers
                if u.name.lower() not in _NOT_PEOPLE and (u / "AppData").is_dir():
                    out.append((u / "AppData/Local", u / "AppData/Roaming"))
            except OSError:
                continue
    return out


def _bases(name: str) -> list:
    """Folders that may hold ``name``'s profiles. Opera keeps its bookmarks in the folder itself (``root=True`` in ``_candidates``)."""
    system = platform.system()
    bases: list = []
    for home in _homes():
        if system == "Linux":
            bases += _linux_table(home, name)
        elif system == "Darwin":
            bases += _mac_table(home, name)
        elif system == "Windows":
            local = _abs_env("LOCALAPPDATA") or home / "AppData/Local"
            roam = _abs_env("APPDATA") or home / "AppData/Roaming"
            bases += _windows_table(local, roam, name)
    if _is_wsl():
        for local, roam in _wsl_windows_homes():
            bases += _windows_table(local, roam, name)
    if name == "safari" and system == "Darwin":
        bases += [h / "Library/Safari" for h in _homes()]
    return bases


def _natural(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _stat_ok(path: Path):
    """(exists, denied): ``denied`` when the operating system refuses even to look (macOS without Full Disk Access)."""
    try:
        os.stat(str(path))
    except PermissionError:
        return False, True
    except OSError:
        return False, False
    return True, False


SAFARI_NOTE = ("macOS blocks reading Safari's bookmarks: give your terminal Full Disk Access (System Settings > Privacy & Security), "
               "or export them from Safari (File > Export > Bookmarks) and use `import FILE`.")


def _chromium_profile(browser: str, d: Path, info: dict, root: bool = False) -> Profile | None:
    files = tuple(p for n in ("Bookmarks", "AccountBookmarks") if (p := d / n).is_file())
    fallbacks = () if files else tuple(p for n in ("Bookmarks.bak",) if (p := d / n).is_file())
    notes: tuple = ()
    if not files and not fallbacks:
        if not (d / "EncryptedBookmarks2").is_file():
            return None
        notes = ("this profile keeps its bookmarks in EncryptedBookmarks2 (encrypted by the browser), which mhbm cannot read; "
                 "export them to HTML from the browser and use `import FILE`",)
    elif not files:
        notes = ("only Bookmarks.bak (the previous save) exists",)
    return Profile(browser, d, scrub(info.get("name")) or d.name, files, fallbacks, notes,
                   signed_in=bool(info.get("user_name") or info.get("gaia_id")))


def _chromium_profiles(browser: str, base: Path, root: bool) -> list:
    if root:                                     # Opera: the folder is the profile
        prof = _chromium_profile(browser, base, {}, True)
        return [prof] if prof else []
    state = _dict(_read_json(base / "Local State").get("profile"))
    cache = _dict(state.get("info_cache"))
    try:
        dirs = [d for d in base.iterdir() if d.is_dir() and d.name not in SKIPPED_PROFILE_DIRS
                and not _dict(cache.get(d.name)).get("is_ephemeral")]
    except OSError:
        return []
    last = state.get("last_used") if isinstance(state.get("last_used"), str) else ""
    dirs.sort(key=lambda d: (d.name != last, d.name != "Default", _natural(d.name)))
    return [p for d in dirs if (p := _chromium_profile(browser, d, _dict(cache.get(d.name))))]


def _safari_profiles(base: Path) -> list:
    plist = base / "Bookmarks.plist"
    exists, denied = _stat_ok(plist)
    if exists:
        return [Profile("safari", base, "Safari", (plist,))]
    return [Profile("safari", base, "Safari", notes=(SAFARI_NOTE,))] if denied else []


def _gecko_profiles(browser: str) -> list:
    out: list = []
    for home in _homes():
        for root in gecko.roots(browser, home):
            if not root.is_dir():
                continue
            names = gecko.profile_names(root)
            for prof in gecko.profile_dirs(root):
                db = prof / gecko.PLACES
                backups = sorted((prof / "bookmarkbackups").glob(gecko.BACKUP_GLOB))
                if db.is_file() or backups:
                    out.append(Profile(browser, prof, scrub(names.get(prof)) or prof.name, (db,) if db.is_file() else (),
                                       (backups[-1],) if backups else ()))
    return out


def discover(name: str) -> list:
    """Every profile of browser ``name`` on this machine (also the ones that cannot be read, with a note saying why), the one the
    browser would open first, then the others. A folder reachable through several names (symlinks) is listed once."""
    if name in GECKO:
        found = _gecko_profiles(name)
    elif name == "safari":
        found = [p for base in _bases(name) for p in _safari_profiles(base)]
    else:
        root = name in ("opera", "opera-gx")
        found = [p for base in _bases(name) if base.is_dir() for p in _chromium_profiles(name, base, root)]
    seen, out = set(), []
    for p in found:
        key = os.path.realpath(p.dir)
        if key not in seen:
            seen.add(key)
            out.append(p)
    for i, p in enumerate(out):
        p.default = i == 0
    return out


def find_profiles(name: str) -> list:
    """Paths of bookmark stores for ``name``: for Chromium the ``Bookmarks`` file of each profile (``AccountBookmarks`` or
    ``Bookmarks.bak`` when that is all there is), for Gecko ``places.sqlite`` of the profile the installation really uses first and
    the newest ``bookmarkbackups`` file, for Safari the plist."""
    if name in GECKO:
        return gecko.files(name)
    return [p.sources[0] for p in discover(name) if p.sources]


def detect() -> dict:
    found = {n: [str(p) for p in find_profiles(n)] for n in ALL}
    return {n: paths for n, paths in found.items() if paths}


def describe() -> list:
    """All profiles of all browsers as plain dicts for ``browsers --json``: names are scrubbed of e-mail addresses."""
    rows = []
    for name in ALL:
        for p in discover(name):
            rows.append({"browser": name, "profile": p.dir.name, "name": p.name, "path": str(p.sources[0]) if p.sources else str(p.dir),
                         "default": p.default, "signed_in": p.signed_in, "readable": bool(p.sources), "notes": list(p.notes)})
    return rows


MOZLZ4_MAGIC = b"mozLz40\0"


def lz4_block_decompress(src: bytes, size: int) -> bytes:
    """Pure-Python LZ4 *block* decoder (enough for Firefox ``.jsonlz4`` backups)."""
    try:
        return _lz4_block(src, size)
    except IndexError as e:
        raise formats.FormatError("corrupt LZ4 data (truncated)") from e


def _lz4_block(src: bytes, size: int) -> bytes:
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= n:
            break  # the last sequence holds literals only
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0 or offset > len(out):
            raise formats.FormatError("corrupt LZ4 data (bad match offset)")
        match = token & 15
        if match == 15:
            while True:
                b = src[i]
                i += 1
                match += b
                if b != 255:
                    break
        match += 4
        if len(out) + match > size + 65536:  # refuse BEFORE copying: a few KiB of 0xFF length bytes can claim gigabytes
            raise formats.FormatError("corrupt LZ4 data (output too large)")
        start = len(out) - offset
        if offset >= match:
            out += out[start:start + match]
        else:  # the match overlaps its own output: it repeats the last `offset` bytes
            pattern = bytes(out[start:])
            out += pattern * (match // offset) + pattern[:match % offset]
    return bytes(out[:size]) if size else bytes(out)


def read_mozlz4(path: Path) -> bytes:
    raw = Path(path).read_bytes()
    if not raw.startswith(MOZLZ4_MAGIC) or len(raw) < 12:
        raise formats.FormatError(f"{path}: not a mozlz4 file")
    size = int.from_bytes(raw[8:12], "little")
    if size > 256 * 1024 * 1024:
        raise formats.FormatError(f"{path}: implausible decompressed size {size}")
    return lz4_block_decompress(raw[12:], size)


def _fingerprint(path: Path):
    """What changes when a running browser checkpoints or restarts its WAL: the db's mtime/size and the 32-byte WAL header
    (salts + checkpoint sequence).  Plain appends to the WAL do not change it and are harmless to a copy."""
    st = path.stat()
    try:
        with open(path.with_name(path.name + "-wal"), "rb") as f:
            head = f.read(32)
    except FileNotFoundError:
        head = b""
    return st.st_mtime_ns, st.st_size, head


def _copy(path: Path, attempts: int = 6) -> Path:
    """Copy ``path`` and its ``-wal`` (uncheckpointed writes of a running Firefox) into a private temp dir.

    Firefox keeps places.sqlite open in exclusive WAL mode, so the files cannot be opened in place; but a db copied *before* and
    a WAL copied *after* a checkpoint do not belong together (SQLite then reports missing index rows, or silently drops the
    newest bookmarks).  Copy, then re-check the fingerprint; retry if the browser moved in between, refuse if it keeps moving.
    The ``-shm`` is never copied: SQLite rebuilds the wal-index from the WAL."""
    path = Path(path)
    for attempt in range(attempts):
        tmp = Path(tempfile.mkdtemp(prefix="mhbm-"))
        before = _fingerprint(path)
        dest = tmp / path.name
        shutil.copy2(path, dest)
        wal = path.with_name(path.name + "-wal")
        if wal.exists():
            try:
                shutil.copy2(wal, tmp / wal.name)
            except FileNotFoundError:  # the browser removed it (clean shutdown) between exists() and the copy
                pass
        if _fingerprint(path) == before:
            return dest
        shutil.rmtree(tmp, ignore_errors=True)
        time.sleep(0.05 * (attempt + 1))
    raise formats.FormatError(f"{path.name} kept changing while it was copied: the browser is busy writing it. "
                              "Retry in a moment, or close the browser first.")


class _Malformed(formats.FormatError):
    """A database that is damaged, or a copy of one a running browser was writing: worth one more try."""


def parse_places(db_path: Path) -> formats.ParsedList:
    """Firefox ``places.sqlite``: folders, tags and bookmarks."""
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)  # as_uri() quotes '#', '?', '%' in the path
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT b.id, b.parent, b.type, b.title, b.dateAdded, b.guid, b.fk, p.url "
            "FROM moz_bookmarks b LEFT JOIN moz_places p ON p.id = b.fk").fetchall()
    except sqlite3.DatabaseError as e:  # not a database, truncated, a different app's file: say so instead of a traceback
        error = _Malformed if "malformed" in str(e).lower() else formats.FormatError
        raise error(f"{Path(db_path).name}: not a readable Firefox places.sqlite ({e})") from e
    finally:
        conn.close()
    nodes = {r["id"]: r for r in rows}
    root_names = {"toolbar_____": "Bookmarks bar", "unfiled_____": "Other bookmarks",
                  "mobile______": "Mobile bookmarks", "menu________": "Bookmarks menu"}
    tags_root = next((r["id"] for r in rows if r["guid"] == "tags________"), None)
    tags_of: dict = {}
    for r in rows:
        if r["parent"] in nodes and nodes[r["parent"]]["parent"] == tags_root and r["fk"]:
            tags_of.setdefault(r["fk"], []).append(nodes[r["parent"]]["title"])

    def path_of(folder_id):
        names, cur, guard = [], folder_id, 0
        while cur in nodes and guard < 50:
            n = nodes[cur]
            if n["guid"] == "root________":
                break
            names.append(root_names.get(n["guid"]) or n["title"] or "")
            cur, guard = n["parent"], guard + 1
        return "/".join(esc_name(x) for x in reversed(names) if x)

    def under_tags(folder_id):
        cur, guard = folder_id, 0
        while cur in nodes and guard < 50:
            if cur == tags_root:
                return True
            cur, guard = nodes[cur]["parent"], guard + 1
        return False

    items, folders = [], set()
    for r in rows:
        if r["type"] == 2 and not under_tags(r["id"]) and r["guid"] != "root________":
            folders.add(path_of(r["id"]))
        elif r["type"] == 1 and r["url"] and not r["url"].startswith("place:") and not under_tags(r["parent"]):
            items.append(Bookmark(url=r["url"], title=r["title"] or "", folder=path_of(r["parent"]),
                                  tags=tags_of.get(r["fk"], []), added=formats.to_unix(r["dateAdded"])))
    return formats._with_folders(items, {f for f in folders if f})


def _utc(moment) -> int:
    """Unix seconds of a plist date. ``plistlib`` returns naive datetimes that are in UTC; ``.timestamp()`` would read them as local time."""
    if not moment:
        return 0
    return int(moment.timestamp() if moment.tzinfo else moment.replace(tzinfo=timezone.utc).timestamp())


def parse_safari(data: dict) -> formats.ParsedList:
    items, folders = [], set()
    names = {"BookmarksBar": "Bookmarks bar", "BookmarksMenu": "Bookmarks menu", "com.apple.ReadingList": "Reading List"}

    def walk(node, path):
        kind = node.get("WebBookmarkType")
        if kind == "WebBookmarkTypeLeaf" and node.get("URLString"):
            title = (node.get("URIDictionary") or {}).get("title", "")
            added = node.get("ReadingList", {}).get("DateAdded")
            items.append(Bookmark(url=node["URLString"], title=title, folder="/".join(path),
                                  added=_utc(added)))
        elif kind == "WebBookmarkTypeList":
            name = names.get(node.get("Title"), node.get("Title", ""))
            sub = path + [esc_name(name)] if name else path
            if name:
                folders.add("/".join(sub))
            for child in node.get("Children", []):
                walk(child, sub)

    walk(data, [])
    return formats._with_folders(items, folders)


def _source_in(folder: Path) -> Path:
    """A profile *folder* (what the browser's profile manager / about:support shows) -> the file in it that holds the bookmarks."""
    for name in ("places.sqlite", "Bookmarks", "Bookmarks.plist"):
        if (folder / name).is_file():
            return folder / name
    backups = sorted((folder / "bookmarkbackups").glob(gecko.BACKUP_GLOB))
    if backups:
        return backups[-1]
    raise formats.FormatError(f"{folder}: no places.sqlite, Bookmarks, Bookmarks.plist or bookmarkbackups here; pass the file itself")


def _is_sqlite(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(16) == b"SQLite format 3\x00"


def _parse(copy: Path) -> formats.ParsedList:
    if copy.suffix == ".jsonlz4":
        return formats.parse_firefox(json.loads(read_mozlz4(copy).decode("utf-8")))
    if copy.suffix == ".sqlite" or copy.name == "places.sqlite" or _is_sqlite(copy):
        return parse_places(copy)
    if copy.suffix == ".plist":
        return parse_safari(plistlib.loads(copy.read_bytes()))
    if copy.suffix == ".json":  # Firefox's own JSON backup or export, Chrome-style JSON: let the sniffer decide
        return formats.read_file(copy)
    return formats.parse_chrome(json.loads(copy.read_text(encoding="utf-8-sig")))   # Chromium accepts a BOM


_PARSE_ERRORS = (ValueError, TypeError, KeyError, AttributeError, IndexError, plistlib.InvalidFileException, ExpatError)


def _unreadable(path: Path, error: OSError) -> formats.FormatError:
    if path.suffix == ".plist" or "Safari" in path.parts:
        return formats.FormatError(f"{path}: {SAFARI_NOTE}")
    return formats.FormatError(f"{path.name}: cannot be read ({error.strerror or error}); close the browser, or copy the profile folder "
                               "somewhere and give that copy to --profile")


def _read(path: Path, attempts: int = 3) -> formats.ParsedList:
    """Copy ``path`` (never open a live profile in place) and parse the copy. A copy of a database that a running browser was writing can
    be torn; that is retried a few times before giving up."""
    for attempt in range(attempts):
        try:
            copy = _copy(path)
        except OSError as e:
            raise _unreadable(path, e) from e
        try:
            return _parse(copy)
        except (sqlite3.DatabaseError, _Malformed) as e:
            if not isinstance(e, _Malformed) and "malformed" not in str(e).lower():
                raise formats.FormatError(f"{path.name}: not a readable Firefox places.sqlite ({e})") from e
            if attempt == attempts - 1:
                raise formats.FormatError(f"{path.name}: not a readable Firefox places.sqlite ({e}): damaged, or the browser kept writing it "
                                          "while it was copied; close the browser and run the command again") from e
            time.sleep(0.2 * (attempt + 1))
        except formats.FormatError:
            raise
        except OSError as e:
            raise _unreadable(path, e) from e
        except _PARSE_ERRORS as e:
            raise formats.FormatError(f"{path.name}: not a readable bookmarks file ({e})") from e
        finally:
            shutil.rmtree(copy.parent, ignore_errors=True)
    raise formats.FormatError(f"{path.name}: could not be read")   # not reached


def _merge(parts: list) -> formats.ParsedList:
    """The bookmarks of one profile's files (``Bookmarks`` plus ``AccountBookmarks``) as one list; a bookmark in both is listed once."""
    if len(parts) == 1:
        return parts[0]
    items, seen = [], set()
    for part in parts:
        for b in part:
            if (b.url, b.folder) not in seen:
                seen.add((b.url, b.folder))
                items.append(b)
    folders = set().union(*(getattr(p, "folders", set()) for p in parts))
    meta: dict = {}
    for part in parts:
        meta.update(getattr(part, "folder_meta", {}))
    return formats.ParsedList(items, folders, meta)


def _select(key: str, selector: str) -> tuple:
    """``--profile`` / a path: a file, a profile folder, a user-data folder (its first profile), a profile's folder name or its display name.
    Returns ``(label, [files])``."""
    p = Path(selector).expanduser()
    if p.exists():
        if p.is_file():
            return p.parent.name, [p]
        if key in CHROMIUM and (p / "Local State").is_file():
            for prof in discover(key):
                if prof.sources and os.path.realpath(prof.dir.parent) == os.path.realpath(p):
                    return prof.dir.name, list(prof.sources)
        files = [q for n in ("Bookmarks", "AccountBookmarks") if (q := p / n).is_file()]
        return p.name, files or [_source_in(p)]
    if key in ALL:
        want = selector.casefold()
        for prof in discover(key):
            if want in (prof.dir.name.casefold(), prof.name.casefold()):
                if prof.sources:
                    return prof.dir.name, list(prof.sources)
                raise formats.FormatError(f"{prof.name}: {prof.notes[0] if prof.notes else 'no bookmarks file'}")
        raise formats.FormatError(f"no {key} profile named {selector!r}; `mhbm browsers` lists the profiles it can see")
    raise formats.FormatError(f"{selector} is not a file or folder")


def _plan(name_or_path: str, profile: str | None, all_profiles: bool) -> tuple:
    """``([(label, [files])], other usable profiles that were not chosen)``."""
    key = name_or_path.lower()
    if key in UNSUPPORTED:
        raise formats.FormatError(UNSUPPORTED[key])
    if profile:
        return [_select(key, profile)], 0
    if key not in ALL:
        return [_select(key, name_or_path)], 0
    found = discover(key)
    usable = [p for p in found if p.sources]
    if not usable:
        if found and found[0].notes:
            raise formats.FormatError(f"{key}: {found[0].notes[0]}")
        raise formats.FormatError(f"no {key} profile found on this machine; `mhbm browsers` lists what was found")
    chosen = usable if all_profiles else usable[:1]
    return [(p.dir.name, list(p.sources)) for p in chosen], len(usable) - len(chosen)


def load_sources(name_or_path: str, profile: str | None = None, all_profiles: bool = False) -> tuple:
    """Read bookmarks from a browser: ``chrome|brave|edge|vivaldi|opera|opera-gx|yandex|chromium|firefox|librewolf|waterfox|floorp|zen|safari``
    (its first profile - the one the browser opens - unless ``profile`` names another or ``all_profiles``), or a file/folder path.

    Returns ``(bookmarks, files read, other usable profiles left unread, profile names)``."""
    plan, others = _plan(name_or_path, profile, all_profiles)
    parts, sources, labels = [], [], []
    for label, paths in plan:
        parts.append(_merge([_read(path) for path in paths]))
        sources += paths
        labels.append(label)
    items = parts[0] if len(parts) == 1 else formats.ParsedList(
        [b for part in parts for b in part], set().union(*(getattr(x, "folders", set()) for x in parts)))
    return items, sources, others, labels


def load(name_or_path: str, profile: str | None = None) -> formats.ParsedList:
    """Bookmarks of one browser profile (see ``load_sources``)."""
    return load_sources(name_or_path, profile)[0]
