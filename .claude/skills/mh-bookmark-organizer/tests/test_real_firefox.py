"""Optional: reads profiles written by a REAL Gecko process.  Skipped unless MHBM_FIREFOX_DIR names a directory with the `firefox`
executable (see tests/real_firefox/README.md); CI downloads a pinned Firefox for the job that sets it."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import common
from mhbm import browsers, formats

HARNESS = Path(__file__).resolve().parent / "real_firefox"
sys.path.insert(0, str(HARNESS))
FIREFOX_DIR = os.environ.get("MHBM_FIREFOX_DIR", "")
EXE = os.environ.get("MHBM_GECKO_EXE", "firefox")


def _skip_reason() -> str:
    if not FIREFOX_DIR:
        return "MHBM_FIREFOX_DIR not set (no real Gecko binary to drive)"
    if not (Path(FIREFOX_DIR) / EXE).exists():
        return f"{Path(FIREFOX_DIR) / EXE} does not exist"
    if sys.platform != "linux":
        return "the harness drives a Linux build of the browser"
    if os.environ.get("MHBM_NO_NETNS") != "1":  # the browser is normally cut off from the network with `unshare -n`
        if not shutil.which("unshare") or subprocess.run(["unshare", "-n", "true"], capture_output=True).returncode != 0:
            return "cannot create a network namespace here (set MHBM_NO_NETNS=1 to run the browser on the real network, prefs still disable it)"
    return ""


def run_harness(script: str, *args: str) -> dict:
    p = subprocess.run([sys.executable, "-I", str(HARNESS / script), *args], capture_output=True, text=True, timeout=240,
                       env={**os.environ, "MHBM_SCRIPTS": str(Path(common.__file__).resolve().parents[1] / "scripts")})
    if p.returncode != 0:
        raise AssertionError(f"{script} failed ({p.returncode}):\n{p.stdout[-1500:]}\n{p.stderr[-1500:]}")
    return json.loads(p.stdout[p.stdout.index("{"):])


@unittest.skipIf(_skip_reason(), _skip_reason())
class RealGecko(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import make_netscape
        cls.tmp = Path(tempfile.mkdtemp(prefix="mhbm-gecko-"))
        (cls.tmp / "rich.html").write_text(make_netscape.rich(), encoding="utf-8")
        cls.clean = cls.tmp / "clean"
        cls.killed = cls.tmp / "killed"
        cls.report = {
            "clean": run_harness("build_profile.py", str(cls.clean), str(cls.tmp / "rich.html"), "--extras", "--end", "clean"),
            "kill": run_harness("build_profile.py", str(cls.killed), str(cls.tmp / "rich.html"), "--extras", "--end", "kill", "--no-backup"),
        }

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _compare(self, profile: Path, sources: str):
        import compare
        truth = json.loads(Path(str(profile) + ".truth.json").read_text(encoding="utf-8"))
        want = sorted(compare.truth_items(truth, include_place_queries=False))
        for src in sources.split(","):
            if src == "sqlite":
                parsed = browsers.load("firefox", profile=str(profile / "places.sqlite"))
            elif src == "jsonlz4":
                parsed = browsers.load("firefox", profile=str(sorted((profile / "bookmarkbackups").glob("bookmarks-*.jsonlz4"))[-1]))
            else:
                parsed = browsers.load("firefox", profile=str(profile) + ".backup.json")
            self.assertEqual(sorted(compare.mhbm_items(parsed)), want, src)
            self.assertEqual(set(parsed.folders), compare.truth_folders(truth), src)

    def test_cleanly_closed_profile_all_three_sources(self):
        self._compare(self.clean, "sqlite,jsonlz4,json")
        self.assertEqual(self.report["clean"]["exit"], 0)

    def test_killed_firefox_leaves_a_wal_that_is_still_read(self):
        files = self.report["kill"]["files"]
        self.assertGreater(files["places.sqlite-wal"], 0)           # the commits really are only in the WAL
        self._compare(self.killed, "sqlite")

    def test_profile_folder_argument(self):
        self.assertEqual(len(browsers.load("firefox", profile=str(self.clean))), len(browsers.load("firefox", profile=str(self.clean / "places.sqlite"))))

    def test_our_html_export_is_imported_faithfully_by_firefox(self):
        """Write-back direction for Firefox is 'export HTML, import in Firefox': every bookmark must arrive with its URL, title and
        date, the toolbar folder must become Firefox's toolbar and 'Other bookmarks' its unfiled root."""
        lib = formats.read_file(Path(common.__file__).resolve().parent / "fixtures" / "messy-library.html")
        (self.tmp / "export.html").write_text(formats.write_netscape(list(lib), lib.folders), encoding="utf-8")
        report = run_harness("build_profile.py", str(self.tmp / "exported"), str(self.tmp / "export.html"), "--end", "clean", "--no-backup")
        self.assertEqual(report["exit"], 0)
        tree = json.loads((self.tmp / "exported.truth.json").read_text(encoding="utf-8"))["tree"]

        def count(n):
            return int(n.get("type") == "text/x-moz-place") + sum(count(c) for c in n.get("children", []))

        roots = {c["guid"]: count(c) for c in tree["children"]}
        on_bar = sum(1 for b in lib if b.folder.split("/")[0] == "Bookmarks bar")
        self.assertEqual(roots["toolbar_____"], on_bar)
        self.assertEqual(roots["unfiled_____"], len(lib) - on_bar)
        self.assertEqual(roots["menu________"], 0)

    def test_reading_while_firefox_is_running(self):
        probe = run_harness("live_test.py", str(self.clean), str(self.tmp / "live"), "--probe")["phase_A"]
        self.assertTrue(probe["direct_ro_blocked"], probe)            # why the files are copied: Firefox holds the db exclusively
        self.assertTrue(probe["live_load_equals_gecko_truth"], probe)


if __name__ == "__main__":
    unittest.main()
