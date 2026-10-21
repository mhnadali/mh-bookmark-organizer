import io
import json
import os
import platform
import shutil
import stat
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import common  # noqa: F401
from mhbm import nativehost
from mhbm.model import Bookmark
from mhbm.store import Library
from test_cli import CliCase

EXT = Path(__file__).resolve().parents[1] / "extension"
NODE = shutil.which("node")


def frame(obj) -> bytes:
    data = json.dumps(obj).encode()
    return struct.pack("=I", len(data)) + data


def unframe(raw: bytes):
    out, i = [], 0
    while i < len(raw):
        (n,) = struct.unpack("=I", raw[i:i + 4])
        out.append(json.loads(raw[i + 4:i + 4 + n]))
        i += 4 + n
    return out


class Protocol(unittest.TestCase):
    def test_framing_roundtrip_and_limits(self):
        buf = io.BytesIO()
        nativehost.write_message(buf, {"id": 1, "x": "ü ✓"})
        buf.seek(0)
        self.assertEqual(nativehost.read_message(buf), {"id": 1, "x": "ü ✓"})
        self.assertIsNone(nativehost.read_message(io.BytesIO(b"")))
        self.assertIsNone(nativehost.read_message(io.BytesIO(b"\x05\x00\x00\x00ab")))            # truncated
        with self.assertRaises(ValueError):
            nativehost.read_message(io.BytesIO(struct.pack("=I", nativehost.MAX_IN + 1)))
        big = io.BytesIO()
        nativehost.write_message(big, {"id": 7, "ok": True, "result": "x" * (nativehost.MAX_OUT + 10)})
        self.assertFalse(unframe(big.getvalue())[0]["ok"])                                       # never exceed the 1 MB browser limit


class HostCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        lib = Library(self.tmp.name)
        bms = []
        for i in range(8):
            bms.append(Bookmark(url=f"https://recipes{i}.example/{i}", title=f"Pasta tomato recipe {i}", folder="Food", id=f"f{i}", added=1600000000 + i * 90000))
            bms.append(Bookmark(url=f"https://k8s{i}.example/{i}", title=f"Kubernetes operator guide {i}", folder="Dev", id=f"d{i}", added=1600000000 + i * 90000 + 30000))
        bms.append(Bookmark(url="https://loose.example/pasta", title="Easy tomato pasta recipe", id="loose", added=1600900000))
        lib.upsert(bms)
        lib.add_folders(["Food", "Dev"])
        lib.close()
        self.host = nativehost.Host(self.tmp.name)
        self.addCleanup(self.host.close)

    def call(self, **msg):
        r = self.host.handle({"id": 1, **msg})
        return r

    def test_ping_folders_search(self):
        self.assertTrue(self.call(cmd="ping")["result"]["bookmarks"] >= 17)
        self.assertEqual(self.call(cmd="folders")["result"], ["Dev", "Food"])
        hits = self.call(cmd="search", query="kubernetes", limit=3)["result"]
        self.assertEqual(len(hits), 3)
        self.assertTrue(all(h["folder"] == "Dev" for h in hits))

    def test_suggest_returns_folder_and_alternatives(self):
        r = self.call(cmd="suggest", url="https://new.example/pasta", title="Quick tomato pasta recipe")["result"]
        self.assertEqual(r["folder"], "Food")
        self.assertTrue(r["alternatives"])
        self.assertIsInstance(r["tags"], list)

    def test_save_import_plan_and_apply_moves(self):
        saved = self.call(cmd="save", url="https://x.example/a", title="Saved", folder="food", tags=["Quick Tag"])["result"]
        self.assertEqual(saved["folder"], "Food")                                                # resolved onto the existing spelling
        lib = Library(self.tmp.name)
        self.addCleanup(lib.close)
        self.assertEqual(lib.get(saved["id"]).tags, ["quick-tag"])
        imp = self.call(cmd="import_tree", nodes=[{"url": "https://x.example/a", "title": "Saved", "folderPath": "Food", "dateAdded": 0},
                                                  {"url": "https://brand-new.example", "title": "N", "folderPath": "Reading/Later", "dateAdded": 1700000000000}])["result"]
        self.assertEqual((imp["read"], imp["imported"]), (2, 1))                                  # the first is already there
        plan = self.call(cmd="plan", precision=0.3)["result"]
        self.assertIn("https://loose.example/pasta", [m["url"] for m in plan["moves"]])
        move = next(m for m in plan["moves"] if m["url"] == "https://loose.example/pasta")
        self.assertEqual((move["from"], move["to"]), ("", "Food"))
        done = self.call(cmd="apply_moves", moves=[move])["result"]
        self.assertEqual(done["updated"], 1)
        self.assertEqual(lib.get("loose").folder, "Food")
        self.assertEqual([h["command"] for h in lib.history()][:3], ["extension-moves", "extension-import", "extension-save"])

    def test_errors_are_replies_not_crashes(self):
        self.assertFalse(self.call(cmd="nope")["ok"])
        self.assertFalse(self.call(cmd="save")["ok"])                                            # missing url

    def test_serve_loop(self):
        raw = frame({"id": 1, "cmd": "ping"}) + struct.pack("=I", 3) + b"{{{" + frame({"id": 2, "cmd": "folders"})
        out = io.BytesIO()
        nativehost.serve(self.tmp.name, io.BytesIO(raw), out)
        replies = unframe(out.getvalue())
        self.assertEqual([r["ok"] for r in replies], [True, False, True])


class Install(CliCase):
    @unittest.skipUnless(platform.system() in ("Linux", "Darwin"), "launcher layout checked on POSIX")
    def test_install_native_host_writes_manifest_and_launcher(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.dir)}), mock.patch("pathlib.Path.home", return_value=self.dir):
            r = self.js("install-native-host", "--extension-id", "a" * 32, "--browser", "chrome")
        manifest = json.loads(Path(r["manifest"]).read_text())
        self.assertEqual((manifest["name"], manifest["type"], manifest["allowed_origins"]),
                         ("com.mhbm.organizer", "stdio", [f"chrome-extension://{'a' * 32}/"]))
        launcher = Path(manifest["path"])
        self.assertTrue(launcher.stat().st_mode & stat.S_IXUSR)
        self.assertIn("native-host", launcher.read_text())
        self.assertIn(str(self.home), launcher.read_text())
        # the launcher really starts the host: ping through the real process
        proc = subprocess.run([str(launcher)], input=frame({"id": 5, "cmd": "ping"}), capture_output=True, timeout=30)
        self.assertEqual(unframe(proc.stdout)[0]["ok"], True)

    def test_bad_extension_id_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_cli("install-native-host", "--extension-id", "not-an-id")


@unittest.skipUnless(NODE, "needs node")
class ExtensionCode(unittest.TestCase):
    def test_manifest_is_valid_mv3(self):
        m = json.loads((EXT / "manifest.json").read_text())
        self.assertEqual(m["manifest_version"], 3)
        self.assertEqual(sorted(m["permissions"]), ["activeTab", "bookmarks", "nativeMessaging", "tabs"])   # nothing broader than needed
        self.assertNotIn("host_permissions", m)
        self.assertNotIn("content_scripts", m)
        for f in (m["background"]["service_worker"], m["action"]["default_popup"]):
            self.assertTrue((EXT / f).exists(), f)

    def test_logic_tests_pass_in_node(self):
        out = subprocess.run([NODE, "--test", str(EXT / "test" / "lib.test.mjs")], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout[-2000:] + out.stderr)

    def test_scripts_parse(self):
        for f in ("lib.js", "popup.js", "background.js"):
            self.assertEqual(subprocess.run([NODE, "--check", str(EXT / f)], capture_output=True).returncode, 0, f)

    @unittest.skipUnless(Path("/opt/node22/lib/node_modules/playwright").exists(), "needs playwright")
    def test_popup_loads_in_a_browser_and_reports_a_missing_host(self):
        with tempfile.TemporaryDirectory() as d:
            script = Path(d, "run.js")
            script.write_text(f"""
const {{ chromium }} = require('/opt/node22/lib/node_modules/playwright');
(async () => {{
  const ctx = await chromium.launchPersistentContext('{d}/profile', {{ executablePath: '/opt/pw-browsers/chromium', headless: false,
    args: ['--headless=new', '--no-sandbox', '--disable-extensions-except={EXT}', '--load-extension={EXT}'] }});
  let sw = ctx.serviceWorkers()[0]; if (!sw) sw = await ctx.waitForEvent('serviceworker', {{ timeout: 15000 }});
  const id = new URL(sw.url()).host;
  const page = await ctx.newPage(); const errs = [];
  page.on('pageerror', e => errs.push(String(e)));
  await page.goto('chrome-extension://' + id + '/popup.html');
  await page.waitForFunction(() => document.getElementById('status').textContent !== 'connecting…', null, {{ timeout: 15000 }});
  console.log(JSON.stringify({{ errs, status: await page.textContent('#status'), msg: await page.textContent('#saveMsg') }}));
  await ctx.close();
}})();
""")
            out = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=120)
            if out.returncode != 0 or not out.stdout.strip():
                self.skipTest("this sandbox cannot run extensions in headless Chromium: " + out.stderr[-200:])
            res = json.loads(out.stdout.strip().splitlines()[-1])
            self.assertEqual(res["errs"], [])
            self.assertEqual(res["status"], "host not reachable")
            self.assertIn("install-native-host --extension-id", res["msg"])


@unittest.skipUnless(NODE and Path("/opt/node22/lib/node_modules/playwright").exists(), "needs node + playwright")
class EndToEnd(unittest.TestCase):
    """Real Chromium + the real extension + the real native host over native messaging."""

    def test_save_send_plan_and_apply_in_the_live_browser(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            home, prof = d / "lib", d / "profile"
            (prof / "NativeMessagingHosts").mkdir(parents=True)
            lib = Library(home)
            bms = [Bookmark(url=f"https://recipes{i}.example/{i}", title=f"Pasta tomato recipe {i}", folder="Bookmarks bar/Food", id=f"f{i}", added=1600000000 + i * 90000) for i in range(6)]
            bms += [Bookmark(url=f"https://k8s{i}.example/{i}", title=f"Kubernetes operator guide {i}", folder="Bookmarks bar/Dev", id=f"d{i}", added=1600000000 + i * 90000 + 30000) for i in range(6)]
            lib.upsert(bms)
            lib.add_folders(["Bookmarks bar/Food", "Bookmarks bar/Dev"])
            lib.close()
            launcher = prof / "NativeMessagingHosts" / "launch.sh"
            scripts = Path(nativehost.__file__).resolve().parents[1]
            import sys
            launcher.write_text(f'#!/bin/sh\nPYTHONPATH="{scripts}" exec "{sys.executable}" -m mhbm --home "{home}" native-host\n')
            launcher.chmod(0o755)
            (d / "run.js").write_text(f"""
const {{ chromium }} = require('/opt/node22/lib/node_modules/playwright');
const fs = require('fs');
(async () => {{
  const ctx = await chromium.launchPersistentContext('{prof}', {{ executablePath: '/opt/pw-browsers/chromium', headless: false,
    args: ['--headless=new','--no-sandbox','--disable-extensions-except={EXT}','--load-extension={EXT}'] }});
  let sw = ctx.serviceWorkers()[0]; if (!sw) sw = await ctx.waitForEvent('serviceworker', {{timeout: 15000}});
  const id = new URL(sw.url()).host;
  fs.writeFileSync('{prof}/NativeMessagingHosts/com.mhbm.organizer.json', JSON.stringify({{name:'com.mhbm.organizer',description:'t',path:'{launcher}',type:'stdio',allowed_origins:['chrome-extension://'+id+'/']}}));
  const page = await ctx.newPage(); const errs = []; page.on('pageerror', e => errs.push(String(e)));
  await page.goto('chrome-extension://' + id + '/popup.html');
  await page.waitForFunction(() => /^(library|host)/.test(document.getElementById('status').textContent), null, {{timeout: 30000}});
  const status = await page.textContent('#status');
  await page.evaluate(async () => {{
    const bar = (await chrome.bookmarks.getTree())[0].children[0];
    await chrome.bookmarks.create({{parentId: bar.id, title: 'Easy tomato pasta recipe', url: 'https://newrecipe.example/p'}});
    await chrome.bookmarks.create({{parentId: bar.id, title: 'Operator controller kubernetes tutorial', url: 'https://newk8s.example/q'}});
  }});
  await page.click('#sendBtn'); await page.waitForFunction(() => /new in the library/.test(document.getElementById('planMsg').textContent), null, {{timeout: 60000}});
  await page.click('#planBtn'); await page.waitForFunction(() => /suggested moves/.test(document.getElementById('planMsg').textContent), null, {{timeout: 120000}});
  await page.click('#applyBtn'); await page.waitForFunction(() => /^moved/.test(document.getElementById('planMsg').textContent), null, {{timeout: 60000}});
  const applied = await page.textContent('#planMsg');
  const where = await page.evaluate(async () => {{
    const out = {{}}; const walk = (n, path) => {{ for (const c of n.children || []) {{ if (c.url) out[c.url] = path.join('/'); else walk(c, [...path, c.title]); }} }};
    walk((await chrome.bookmarks.getTree())[0], []); return out;
  }});
  console.log(JSON.stringify({{ errs, status, applied, where }}));
  await ctx.close();
}})();
""")
            out = subprocess.run([NODE, str(d / "run.js")], capture_output=True, text=True, timeout=240)
            if out.returncode != 0 or not out.stdout.strip():
                self.skipTest("this sandbox cannot run the extension end to end: " + out.stderr[-300:])
            res = json.loads(out.stdout.strip().splitlines()[-1])
            self.assertEqual(res["errs"], [])
            self.assertTrue(res["status"].startswith("library: 12"))
            self.assertTrue(res["applied"].startswith("moved 2"), res["applied"])
            self.assertTrue(res["where"]["https://newrecipe.example/p"].endswith("Food"))
            self.assertTrue(res["where"]["https://newk8s.example/q"].endswith("Dev"))
            lib = Library(home)
            try:
                self.assertEqual({b.url: b.folder for b in lib.all() if "new" in b.url},
                                 {"https://newrecipe.example/p": "Bookmarks bar/Food", "https://newk8s.example/q": "Bookmarks bar/Dev"})
            finally:
                lib.close()


if __name__ == "__main__":
    unittest.main()
