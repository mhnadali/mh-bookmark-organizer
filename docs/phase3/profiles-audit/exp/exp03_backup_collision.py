"""E2 redone with a profile file that differs from what the library would write, to show the original backup is destroyed."""
import contextlib, io, sys, tempfile, time
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[1])
from mhbm import cli, formats, sync
from mhbm.model import Bookmark

def run(home, *args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["--home", str(home), "--quiet", *args]); cli.close_all()
    return code

with tempfile.TemporaryDirectory() as d:
    d = Path(d); home = d / "lib"; target = d / "Bookmarks"
    lib_src = d / "src"; lib_src.write_text(formats.write_chrome([Bookmark(url="https://library.example/", title="L", folder="Bookmarks bar")]))
    run(home, "import-browser", "x", "--profile", str(lib_src))
    original = formats.write_chrome([Bookmark(url="https://irreplaceable.example/", title="IRREPLACEABLE", folder="Bookmarks bar")])
    target.write_text(original)
    with mock.patch.object(time, "strftime", lambda fmt, *a: "20260101-000000"):   # two runs inside one second
        run(home, "install-chrome", str(target), "--confirm-chrome-closed")
        run(home, "install-chrome", str(target), "--confirm-chrome-closed")
    backups = list(d.glob("Bookmarks.mhbm-backup-*"))
    print("backups:", [b.name for b in backups])
    print("original bookmark still recoverable from any backup:", any("irreplaceable.example" in b.read_text() for b in backups))
