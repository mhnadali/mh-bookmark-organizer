"""Baseline behaviour of install-chrome / sync write path, URI handling, Safari timestamps, --profile docs claim.
Run: PYTHONDONTWRITEBYTECODE=1 python3 -I exp02_write_path_baseline.py <skill-scripts-dir>"""
import contextlib, io, json, os, plistlib, shutil, sqlite3, stat, sys, tempfile, time
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, sys.argv[1])
from mhbm import browsers, cli, formats, sync  # noqa
from mhbm.model import Bookmark


def run(home, *args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(["--home", str(home), "--quiet", *args]); cli.close_all()
        except BaseException as e:  # noqa
            return f"EXC {type(e).__name__}: {e}", out.getvalue(), err.getvalue()
    return code, out.getvalue(), err.getvalue()


with tempfile.TemporaryDirectory() as d:
    d = Path(d); home = d / "lib"
    prof = d / "Default"; prof.mkdir()
    target = prof / "Bookmarks"
    target.write_text(formats.write_chrome([Bookmark(url="https://orig.example/", title="Orig", folder="Bookmarks bar")]), encoding="utf-8")
    print("# E1 import the profile into the library:", run(home, "import-browser", "x", "--profile", str(target))[0])

    # E2 backup collision: two install-chrome in the same second
    orig_text = target.read_text()
    with mock.patch.object(time, "strftime", lambda fmt, *a: "20260101-000000"):
        run(home, "install-chrome", str(target), "--confirm-chrome-closed")
        first_output = target.read_text()
        run(home, "install-chrome", str(target), "--confirm-chrome-closed")
    backups = sorted(prof.glob("Bookmarks.mhbm-backup-*"))
    surviving = backups[0].read_text()
    print("# E2 same-second collision: backups=", [b.name for b in backups], "| backup still holds the ORIGINAL file:", surviving == orig_text,
          "| backup holds output of 1st run:", surviving == first_output)

    # E3 non-atomic write: truncated target if the write fails half-way (disk full / kill)
    target.write_text(orig_text, encoding="utf-8")
    real = Path.write_text
    def half(self, data, *a, **k):
        if self.name == "Bookmarks":
            real(self, data[: len(data) // 2], *a, **k)
            raise OSError(28, "No space left on device")
        return real(self, data, *a, **k)
    with mock.patch.object(Path, "write_text", half):
        res = run(home, "install-chrome", str(target), "--confirm-chrome-closed")
    print("# E3 disk-full mid-write ->", str(res[0])[:90], "| target is now valid JSON:", end=" ")
    try:
        json.loads(target.read_text()); print(True)
    except Exception as e:
        print(False, f"({type(e).__name__})")
    print("     (the exception is not turned into an 'error:' message either: cli.main only catches FormatError/FileNotFoundError/JSONDecodeError/ValueError)")

    # E4 sync.write_remote: ownership/mode/symlink
    real_dir = d / "dotfiles"; real_dir.mkdir()
    realf = real_dir / "Bookmarks"; realf.write_text(orig_text); os.chmod(realf, 0o600); os.chown(realf, 1000, 1000)
    link = prof / "Bookmarks.lnk"; link.symlink_to(realf)
    data = json.loads(orig_text)
    backup = sync.write_remote(link, data)
    st = os.stat(link, follow_symlinks=False)
    print("# E4 write_remote through a symlink: still a symlink:", stat.S_ISLNK(st.st_mode), "| real file untouched (old mtime/owner):",
          os.stat(realf).st_uid, oct(os.stat(realf).st_mode & 0o777), "| link replaced by regular file owner/mode:", st.st_uid, oct(st.st_mode & 0o777))
    plain = prof / "Bookmarks"; plain.write_text(orig_text); os.chmod(plain, 0o600); os.chown(plain, 1000, 1000)
    b = sync.write_remote(plain, data)
    s1, s2 = os.stat(plain), os.stat(b)
    print("# E5 write_remote on a 0600 file owned by uid 1000, run as root: new file uid/mode =", s1.st_uid, oct(s1.st_mode & 0o777),
          "| backup uid/mode =", s2.st_uid, oct(s2.st_mode & 0o777), " (browser user would no longer own its Bookmarks file)")
    plain.write_text(orig_text); os.chown(plain, 1000, 1000); os.chmod(plain, 0o600)
    run(home, "install-chrome", str(plain), "--confirm-chrome-closed")
    s1 = os.stat(plain)
    print("# E6 install-chrome (in-place truncate) keeps owner/mode:", s1.st_uid, oct(s1.st_mode & 0o777), "| but backup is owned by", os.stat(sorted(prof.glob('Bookmarks.mhbm-backup-*'))[-1]).st_uid)

    # E7 two write_remote in the same second
    plain.write_text(orig_text)
    with mock.patch.object(time, "strftime", lambda fmt, *a: "20260101-111111"):
        b1 = sync.write_remote(plain, json.loads(orig_text)); plain.write_text("SECOND VERSION")
        b2 = sync.write_remote(plain, json.loads(orig_text)) if False else None
    print("# E7 (sync backup name has the same second-resolution scheme as install-chrome; see E2)")

    # E8 sqlite URI with odd characters
    for odd in ("a b", "a#b", "a%b", "a?b", "persian-محمد"):
        sub = d / odd; sub.mkdir()
        db = sub / "places.sqlite"
        c = sqlite3.connect(db); c.executescript("CREATE TABLE moz_places(id INTEGER PRIMARY KEY, url TEXT); CREATE TABLE moz_bookmarks(id INTEGER PRIMARY KEY, type INT, fk INT, parent INT, title TEXT, dateAdded INT, guid TEXT);"
                                                  "INSERT INTO moz_places VALUES (1,'https://a.example/'); INSERT INTO moz_bookmarks VALUES (1,2,NULL,0,'','0','root________'),(2,2,NULL,1,'u','0','unfiled_____'),(3,1,1,2,'t',1600000000000000,'g');"); c.commit(); c.close()
        try:
            r = [x.url for x in browsers.parse_places(db)]
        except Exception as e:
            r = f"{type(e).__name__}: {e}"
        print(f"# E8 parse_places in dir {odd!r}:", r)

    # E9 Safari DateAdded timezone
    leaf = {"WebBookmarkType": "WebBookmarkTypeLeaf", "URLString": "https://rl.example/", "URIDictionary": {"title": "RL"},
            "ReadingList": {"DateAdded": datetime(2024, 1, 1, 12, 0, 0)}}  # plistlib gives naive datetimes in UTC
    root = {"WebBookmarkType": "WebBookmarkTypeList", "Title": "", "Children": [{"WebBookmarkType": "WebBookmarkTypeList", "Title": "com.apple.ReadingList", "Children": [leaf]}]}
    parsed = plistlib.loads(plistlib.dumps(root))
    print("# E9 plistlib date type:", type(parsed["Children"][0]["Children"][0]["ReadingList"]["DateAdded"]).__name__, "tzinfo=", parsed["Children"][0]["Children"][0]["ReadingList"]["DateAdded"].tzinfo)
    for tz in ("UTC", "America/Los_Angeles", "Asia/Tehran"):
        os.environ["TZ"] = tz; time.tzset()
        print(f"     TZ={tz:20s} parse_safari added =", browsers.parse_safari(parsed)[0].added, "(correct UTC value:", int(datetime(2024,1,1,12,tzinfo=__import__('datetime').timezone.utc).timestamp()), ")")
    os.environ["TZ"] = "UTC"; time.tzset()

    # E10 --profile "Profile 1" (as TROUBLESHOOTING.md suggests)
    code, out, err = run(home, "import-browser", "chrome", "--profile", "Profile 1")
    print("# E10 import-browser chrome --profile 'Profile 1' ->", code, err.strip())

    # E11 PermissionError / DatabaseError surface as tracebacks?
    bad = d / "places.sqlite"; bad.write_bytes(b"this is not a database" * 100)
    print("# E11 corrupt places.sqlite ->", run(home, "import-browser", "firefox", "--profile", str(bad))[0])
    unreadable = d / "Unreadable" ; unreadable.write_text("{}"); os.chmod(unreadable, 0)
    print("# E11b unreadable file (root bypasses chmod, so simulate PermissionError):", end=" ")
    with mock.patch.object(shutil, "copy2", side_effect=PermissionError(13, "Permission denied")):
        print(run(home, "import-browser", "x", "--profile", str(target))[0])

    # E12 sync accepts Bookmarks.bak and *.json
    bak = prof / "Bookmarks.bak"; bak.write_text(orig_text)
    print("# E12 sync accepts Bookmarks.bak (a file Chrome never reads at load):", run(home, "sync", str(bak))[0], "(0 = accepted)")
