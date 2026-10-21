"""Baseline behaviour of the CURRENT browsers.py on synthetic fake home directories.
Run:  PYTHONDONTWRITEBYTECODE=1 python3 -I exp01_discovery_baseline.py <path-to-skill-scripts-dir>
Everything lives under a fresh temp dir; nothing outside is read."""
import json, os, sys, tempfile, platform, contextlib, sqlite3
from pathlib import Path
from unittest import mock

sys.path.insert(0, sys.argv[1])
from mhbm import browsers, formats  # noqa

BM = json.dumps({"roots": {"bookmark_bar": {"type": "folder", "name": "Bookmarks bar", "children": [
    {"type": "url", "name": "A", "url": "https://a.example/", "date_added": "13200000000000000"}]},
    "other": {"type": "folder", "name": "Other", "children": []}, "synced": {"type": "folder", "name": "Mobile", "children": []}}, "version": 1})


@contextlib.contextmanager
def fake(system, home, **env):
    keep = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "XDG_CONFIG_HOME", "CHROME_CONFIG_HOME")}
    for k in keep:
        os.environ.pop(k, None)
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        with mock.patch.object(platform, "system", lambda: system):
            yield
    finally:
        for k, v in keep.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def put(root, rel, text=BM):
    p = Path(root, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def show(title, found):
    print(f"--- {title}")
    print("    detect() ->", json.dumps({k: [Path(x).as_posix().split('/home/')[-1] if False else x for x in v] for k, v in found.items()}, indent=2)[:900] or "{}")


def rel(d, found):
    def one(x):
        try:
            return str(Path(x).relative_to(d))
        except ValueError:
            return "<outside home>/" + "/".join(Path(x).parts[-3:])
    return {k: [one(x) for x in v] for k, v in found.items()}

with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    def case(name):
        h = d / name
        h.mkdir()
        return h

    # S1: XDG_CONFIG_HOME
    h = case("s1"); put(h, "xdg/google-chrome/Default/Bookmarks")
    with fake("Linux", h, XDG_CONFIG_HOME=h / "xdg"):
        print("S1 Linux XDG_CONFIG_HOME=<home>/xdg with google-chrome/Default/Bookmarks ->", rel(h, browsers.detect()))

    # S2: channels
    h = case("s2")
    for p in (".config/google-chrome-beta/Default/Bookmarks", ".config/google-chrome-unstable/Default/Bookmarks",
              ".config/microsoft-edge-beta/Default/Bookmarks", ".config/BraveSoftware/Brave-Browser-Nightly/Default/Bookmarks",
              ".config/opera-beta/Bookmarks", ".config/vivaldi-snapshot/Default/Bookmarks"):
        put(h, p)
    with fake("Linux", h):
        print("S2 Linux channel dirs (chrome beta/dev, edge beta, brave nightly, opera beta, vivaldi snapshot) ->", rel(h, browsers.detect()))

    # S3: flatpak / snap
    h = case("s3")
    for p in (".var/app/com.google.Chrome/config/google-chrome/Default/Bookmarks", ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser/Default/Bookmarks",
              ".var/app/org.chromium.Chromium/config/chromium/Default/Bookmarks", ".var/app/com.microsoft.Edge/config/microsoft-edge/Default/Bookmarks",
              "snap/brave/current/.config/BraveSoftware/Brave-Browser/Default/Bookmarks", "snap/chromium/common/chromium/Default/Bookmarks"):
        put(h, p)
    with fake("Linux", h):
        print("S3 flatpak/snap ->", rel(h, browsers.detect()))

    # S4: Chrome profile naming / order / AccountBookmarks / Encrypted
    h = case("s4"); base = ".config/google-chrome"
    for prof in ("Guest Profile", "Profile 1", "Profile 2", "Profile 10", "System Profile"):
        put(h, f"{base}/{prof}/Bookmarks")
    with fake("Linux", h):
        print("S4a Chrome w/o Default, with Guest/System/Profile 1,2,10 (order; found[0] is what import-browser uses) ->", rel(h, browsers.detect()))
    h = case("s4b"); put(h, f"{base}/Default/AccountBookmarks"); put(h, f"{base}/Default/Preferences", "{}")
    with fake("Linux", h):
        print("S4b profile holding only AccountBookmarks ->", rel(h, browsers.detect()))
        try:
            browsers.load("chrome")
        except formats.FormatError as e:
            print("    load('chrome') ->", e)
    h = case("s4c"); put(h, f"{base}/Default/EncryptedBookmarks2", "v10\x00binary")
    with fake("Linux", h):
        print("S4c profile holding only EncryptedBookmarks2 ->", rel(h, browsers.detect()))
    h = case("s4d"); put(h, f"{base}/Default/Bookmarks"); put(h, f"{base}/Default/AccountBookmarks"); put(h, f"{base}/Default/EncryptedBookmarks2", "x")
    with fake("Linux", h):
        print("S4d Bookmarks + AccountBookmarks + EncryptedBookmarks2 ->", rel(h, browsers.detect()), "(AccountBookmarks content is never read)")

    # S5: macOS
    h = case("s5")
    for p in ("Library/Application Support/Google/Chrome Beta/Default/Bookmarks", "Library/Application Support/Google/Chrome Canary/Default/Bookmarks",
              "Library/Application Support/com.operasoftware.OperaGX/Bookmarks", "Library/Application Support/Arc/User Data/Default/Bookmarks",
              "Library/Application Support/Microsoft Edge Beta/Default/Bookmarks", "Library/Application Support/Yandex/YandexBrowser/Default/Bookmarks",
              "Library/Application Support/BraveSoftware/Brave-Browser-Beta/Default/Bookmarks"):
        put(h, p)
    put(h, "Library/Application Support/Google/Chrome/Default/Bookmarks")
    with fake("Darwin", h):
        print("S5 macOS (Chrome stable+beta+canary, Opera GX, Arc, Edge Beta, Yandex, Brave Beta) ->", rel(h, browsers.detect()))

    # S6: Windows, env vars
    h = case("s6")
    for p in ("AppData/Local/Google/Chrome SxS/User Data/Default/Bookmarks", "AppData/Local/Microsoft/Edge Beta/User Data/Default/Bookmarks",
              "AppData/Roaming/Opera Software/Opera GX Stable/Bookmarks", "AppData/Local/Yandex/YandexBrowser/User Data/Default/Bookmarks",
              "AppData/Local/Google/Chrome/User Data/Profile 3/Bookmarks"):
        put(h, p)
    with fake("Windows", h):
        print("S6a Windows, env vars unset (fallback to home/AppData) ->", rel(h, browsers.detect()))
    h2 = case("s6b"); put(h2, "Redirected/Local/Google/Chrome/User Data/Default/Bookmarks")
    with fake("Windows", h2, LOCALAPPDATA=h2 / "Redirected/Local"):
        print("S6b Windows with LOCALAPPDATA redirected ->", rel(h2, browsers.detect()))
    # WSL: Linux kernel, Windows profiles under /mnt/c
    h = case("s7"); put(h, "mnt/c/Users/alice/AppData/Local/Google/Chrome/User Data/Default/Bookmarks")
    with fake("Linux", h), mock.patch.object(browsers, "WSL_MOUNT_ROOT", h / "mnt", create=True), mock.patch.object(browsers, "_is_wsl", lambda: True, create=True):
        print("S7 WSL (Windows Chrome under <mnt>/c/Users/alice/...) ->", rel(h, browsers.detect()))

    # S8: Firefox multi profile
    h = case("s8"); fx = ".mozilla/firefox"
    for prof in ("0zzz.dev-edition-default", "abcd1234.default-release", "wxyz.default"):
        db = Path(h, fx, prof, "places.sqlite"); db.parent.mkdir(parents=True)
        c = sqlite3.connect(db)
        c.executescript("CREATE TABLE moz_places(id INTEGER PRIMARY KEY, url TEXT); CREATE TABLE moz_bookmarks(id INTEGER PRIMARY KEY, type INT, fk INT, parent INT, title TEXT, dateAdded INT, guid TEXT);"
                        f"INSERT INTO moz_places VALUES (1,'https://{prof.split('.')[0]}.example/');"
                        "INSERT INTO moz_bookmarks VALUES (1,2,NULL,0,'','0','root________'),(2,2,NULL,1,'unfiled','0','unfiled_____'),(3,1,1,2,'t',1600000000000000,'g3');")
        c.commit(); c.close()
    put(h, f"{fx}/profiles.ini", "[Profile2]\nName=default\nIsRelative=1\nPath=wxyz.default\n\n[Profile1]\nName=default-release\nIsRelative=1\nPath=abcd1234.default-release\nDefault=1\n\n"
        "[Profile0]\nName=dev-edition-default\nIsRelative=1\nPath=0zzz.dev-edition-default\n\n[General]\nStartWithLastProfile=1\nVersion=2\n")
    with fake("Linux", h):
        print("S8 Firefox: profiles.ini says default-release (abcd1234) is the default ->", rel(h, browsers.detect()))
        print("    import-browser firefox imports:", [b.url for b in browsers.load("firefox")], "(found[0] = alphabetical, not the default profile)")
    # absolute-path profile
    h = case("s8b"); ext = d / "s8b-external" / "myprofile"; ext.mkdir(parents=True)
    c = sqlite3.connect(ext / "places.sqlite"); c.executescript("CREATE TABLE moz_places(id INTEGER PRIMARY KEY, url TEXT); CREATE TABLE moz_bookmarks(id INTEGER PRIMARY KEY, type INT, fk INT, parent INT, title TEXT, dateAdded INT, guid TEXT);"); c.commit(); c.close()
    put(h, ".mozilla/firefox/profiles.ini", f"[Profile0]\nName=ext\nIsRelative=0\nPath={ext}\nDefault=1\n")
    with fake("Linux", h):
        print("S8b Firefox profile at absolute path from profiles.ini ->", rel(h, browsers.detect()) if True else None)
