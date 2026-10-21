"""Find the profiles of Gecko browsers (Firefox, LibreWolf, Waterfox, Floorp, Zen) the way the browser itself does.

A *root* is the directory that holds ``profiles.ini`` (and ``installs.ini``).  Where it lives depends on the browser, the OS,
how it was installed and, since Firefox 147, on whether ``~/.mozilla`` already existed: a fresh Linux install now keeps its
profiles under ``$XDG_CONFIG_HOME/mozilla/firefox`` (``~/.config/mozilla/firefox``).

Which profile is *the* profile: the ``[Install<hash>]`` section (mirrored in ``installs.ini``) names the profile this
installation really uses.  ``Default=1`` in a ``[ProfileN]`` section is a legacy marker and, on a modern install, often points at
an unused ``xxxx.default`` profile, so it ranks below the install default.
"""
from __future__ import annotations

import configparser
import os
import platform
from pathlib import Path

NAMES = ("firefox", "librewolf", "waterfox", "floorp", "zen")
PLACES = "places.sqlite"
BACKUP_GLOB = "bookmarks-*.jsonlz4"


def roots(name: str, home: Path | None = None) -> list:
    """Candidate directories holding ``profiles.ini`` for ``name``, in the order the browser would prefer them."""
    home, system = home or Path.home(), platform.system()
    xdg_env = os.environ.get("XDG_CONFIG_HOME", "")
    xdg = Path(xdg_env) if xdg_env and Path(xdg_env).is_absolute() else home / ".config"
    roam = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
    flat = home / ".var/app"
    table = {
        "Linux": {
            # ~/.mozilla/firefox wins when it exists (Firefox >= 147 only uses the XDG path on a fresh home)
            "firefox": [home / ".mozilla/firefox", xdg / "mozilla/firefox", home / "snap/firefox/common/.mozilla/firefox",
                        flat / "org.mozilla.firefox/.mozilla/firefox", flat / "org.mozilla.firefox/config/mozilla/firefox"],
            "librewolf": [home / ".librewolf", xdg / "librewolf", flat / "io.gitlab.librewolf-community/.librewolf"],
            "waterfox": [home / ".waterfox", xdg / "waterfox", flat / "net.waterfox.waterfox/.waterfox"],
            "floorp": [xdg / "floorp", home / ".floorp", flat / "one.ablaze.floorp/.floorp"],
            "zen": [xdg / "zen", home / ".zen", flat / "app.zen_browser.zen/.zen"],
        },
        "Darwin": {n: [home / "Library/Application Support" / d] for n, d in
                   (("firefox", "Firefox"), ("librewolf", "LibreWolf"), ("waterfox", "Waterfox"), ("floorp", "Floorp"), ("zen", "zen"))},
        "Windows": {"firefox": [roam / "Mozilla/Firefox"], "librewolf": [roam / "LibreWolf"], "waterfox": [roam / "Waterfox"],
                    "floorp": [roam / "Floorp"], "zen": [roam / "zen"]},
    }
    return table.get(system, {}).get(name, [])


def _ini(path: Path):
    cp = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        cp.read(path, encoding="utf-8-sig")   # Firefox on Windows writes a BOM sometimes
    except (OSError, UnicodeError, configparser.Error):
        return None
    return cp


def _join(root: Path, value: str, relative: bool = True) -> Path:
    value = value.strip()
    p = Path(value)
    if relative and not p.is_absolute():
        return root / value.replace("\\", "/")     # a profiles.ini written on Windows separates with backslashes
    return p


def _listed(root: Path) -> tuple:
    """(install defaults, ``Default=1`` profiles, the other profiles) named by installs.ini / profiles.ini, as paths."""
    installs: list = []
    default: list = []
    others: list = []
    for fname in ("installs.ini", "profiles.ini"):
        cp = _ini(root / fname)
        if cp is None:
            continue
        for sec in cp.sections():
            if fname == "installs.ini" or sec.lower().startswith("install"):
                if cp.get(sec, "default", fallback="").strip():
                    installs.append(_join(root, cp.get(sec, "default")))
            elif sec.lower().startswith("profile") and cp.get(sec, "path", fallback="").strip():
                path = _join(root, cp.get(sec, "path"), cp.get(sec, "isrelative", fallback="1").strip() != "0")
                (default if cp.get(sec, "default", fallback="0").strip() == "1" else others).append(path)
    return installs, default, others


def profile_names(root: Path) -> dict:
    """``{profile directory: Name=}`` from profiles.ini (what Firefox's profile manager shows)."""
    names: dict = {}
    cp = _ini(root / "profiles.ini")
    for sec in (cp.sections() if cp is not None else []):
        if sec.lower().startswith("profile") and cp.get(sec, "path", fallback="").strip():
            path = _join(root, cp.get(sec, "path"), cp.get(sec, "isrelative", fallback="1").strip() != "0")
            names[path] = cp.get(sec, "name", fallback="").strip()
    return names


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def profile_dirs(root: Path) -> list:
    """Profile directories under ``root``, most likely the user's one first: the install default(s), the legacy ``Default=1``
    profile, the other listed profiles, then unlisted directories that hold a ``places.sqlite`` (selectable profiles,
    profiles whose ``profiles.ini`` entry was lost), most recently used first."""
    installs, default, others = _listed(root)
    stray = sorted({p.parent for pat in (f"*/{PLACES}", f"Profiles/*/{PLACES}") for p in root.glob(pat)}
                   | {p.parent for pat in ("*/bookmarkbackups", "Profiles/*/bookmarkbackups") for p in root.glob(pat) if p.is_dir()},
                   key=lambda d: _mtime(d / PLACES), reverse=True)
    return list(dict.fromkeys(installs + default + others + stray))


def files(name: str) -> list:
    """``places.sqlite`` of every profile (default first), then the newest ``bookmarkbackups`` file of every profile."""
    dbs: list = []
    backups: list = []
    for root in roots(name):
        if not root.is_dir():
            continue
        for prof in profile_dirs(root):
            if (prof / PLACES).is_file():
                dbs.append(prof / PLACES)
            newest = sorted((prof / "bookmarkbackups").glob(BACKUP_GLOB))
            if newest:
                backups.append(newest[-1])
    return list(dict.fromkeys(dbs + backups))
