import http.client
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import quote

import common  # noqa: F401
from mhbm import webui
from mhbm.model import Bookmark
from mhbm.store import Library

NODE = shutil.which("node")


class UiCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        lib = Library(self.tmp.name)
        bms = [Bookmark(url=f"https://a{i}.example/{i}", title=f"Kubernetes operator {i}", folder="Dev", id=f"d{i}", tags=["k8s"]) for i in range(6)]
        bms += [Bookmark(url=f"https://food.example/{i}", title=f"Pasta recipe {i} | Site", folder="Food", id=f"f{i}") for i in range(6)]
        bms += [Bookmark(url="https://dup.example/x", title="Dup", id="x1"), Bookmark(url="http://www.dup.example/x/", title="Dup", id="x2", folder="Food")]
        lib.upsert(bms)
        lib.add_folders(["Dev", "Food"])
        lib.close()
        self.server = webui.UIServer(self.tmp.name)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def req(self, method, path, body=None, *, token=True, host=None, origin=None, ctype="application/json"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if token is True:
            h["X-MHBM-Token"] = self.server.token
        elif token:
            h["X-MHBM-Token"] = token
        if origin:
            h["Origin"] = origin
        data = None
        if body is not None:
            data = json.dumps(body) if ctype == "application/json" else body
            h["Content-Type"] = ctype
        c.request(method, path, data, h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw


class Security(UiCase):
    def test_token_is_required_for_every_api_call(self):
        for method, path in [("GET", "/api/state"), ("GET", "/api/bookmarks"), ("POST", "/api/undo"), ("GET", "/api/export")]:
            self.assertEqual(self.req(method, path, {} if method == "POST" else None, token=False)[0], 403, path)
            self.assertEqual(self.req(method, path, {} if method == "POST" else None, token="wrong")[0], 403, path)

    def test_dns_rebinding_and_cross_origin_requests_are_refused(self):
        self.assertEqual(self.req("GET", "/api/state", host="evil.example")[0], 403)
        self.assertEqual(self.req("GET", "/api/state", host=f"evil.example:{self.port}")[0], 403)
        self.assertEqual(self.req("GET", "/api/state", origin="https://evil.example")[0], 403)
        self.assertEqual(self.req("GET", "/", host="evil.example")[0], 403)
        self.assertEqual(self.req("GET", "/api/state", origin=f"http://127.0.0.1:{self.port}")[0], 200)

    def test_state_changing_calls_need_json(self):
        self.assertEqual(self.req("POST", "/api/undo", "x=1", ctype="application/x-www-form-urlencoded")[0], 415)
        self.assertEqual(self.req("PATCH", "/api/bookmark/d0", "title=x", ctype="text/plain")[0], 415)

    def test_page_has_a_strict_csp_and_no_token(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request("GET", "/", headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        body = r.read().decode()
        self.assertIn("default-src 'none'", r.getheader("Content-Security-Policy"))
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertNotIn(self.server.token, body)
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))

    def test_refuses_to_bind_beyond_loopback_from_the_cli(self):
        from mhbm import cli
        from test_cli import CliCase  # noqa: F401
        with self.assertRaises(SystemExit):
            cli.main(["--home", self.tmp.name, "ui", "--host", "0.0.0.0"])

    def test_malformed_input_never_crashes_the_server(self):
        self.assertEqual(self.req("POST", "/api/apply", {"plan": []})[0], 400)
        self.assertEqual(self.req("GET", "/api/nothing")[0], 404)
        self.assertEqual(self.req("PATCH", "/api/bookmark/nope", {"title": "x"})[0], 404)
        self.assertEqual(self.req("PATCH", "/api/bookmark/d0", {"state": "bogus"})[0], 400)
        self.assertEqual(self.req("GET", "/api/plan?kind=bogus")[0], 400)
        self.assertEqual(self.req("GET", "/api/state")[0], 200)


class Functions(UiCase):
    def test_state_list_search_and_edit_with_undo(self):
        s = self.req("GET", "/api/state")[1]
        self.assertEqual((s["total"], s["unfiled"]), (14, 1))
        self.assertEqual(len(self.req("GET", "/api/bookmarks?q=kubernetes&limit=3")[1]["items"]), 3)
        self.assertEqual(len(self.req("GET", "/api/bookmarks?limit=5&offset=10")[1]["items"]), 4)
        code, row = self.req("PATCH", "/api/bookmark/d0", {"title": "Renamed", "folder": "dev", "tags": "A, B", "state": "unread", "favorite": True, "notes": "n"})
        self.assertEqual((code, row["title"], row["folder"], row["tags"], row["state"], row["favorite"]), (200, "Renamed", "Dev", ["a", "b"], "unread", True))
        self.assertEqual(self.req("GET", "/api/bookmarks?q=Renamed")[1]["items"][0]["id"], "d0")
        self.assertEqual(self.req("POST", "/api/undo", {})[1]["command"], "ui-edit")
        self.assertEqual(self.req("GET", "/api/bookmarks?q=Renamed")[1]["items"], [])

    def test_bulk_operations(self):
        self.assertEqual(self.req("POST", "/api/bulk", {"ids": ["d0", "d1"], "folder": "Reading", "add_tags": ["Later"]})[1], {"updated": 2})
        items = {i["id"]: i for i in self.req("GET", "/api/bookmarks?q=tag:later")[1]["items"]}
        self.assertEqual(sorted(items), ["d0", "d1"])
        self.assertEqual(items["d0"]["folder"], "Reading")
        self.assertEqual(self.req("POST", "/api/bulk", {"ids": ["d0"], "delete": True})[1], {"deleted": 1})
        self.assertEqual(self.req("POST", "/api/undo", {})[1]["removed"], 0)
        self.assertEqual(self.req("GET", "/api/state")[1]["total"], 14)

    def test_plans_can_be_computed_trimmed_and_applied(self):
        for kind in ("organize", "autotag", "titles", "canonical", "dedupe", "restructure"):
            code, r = self.req("GET", "/api/plan?kind=" + kind)
            self.assertEqual(code, 200, kind)
            self.assertIn("plan", r)
        d = self.req("GET", "/api/plan?kind=dedupe")[1]
        self.assertEqual(sum(1 for c in d["plan"].values() if c.get("delete")), 1)
        t = self.req("GET", "/api/plan?kind=titles")[1]
        chosen = {i: c for i, c in t["plan"].items() if i == "f0"}
        self.assertEqual(chosen["f0"]["title"], "Pasta recipe 0")
        res = self.req("POST", "/api/apply", {"kind": "titles", "plan": chosen})[1]
        self.assertEqual((res["changed"], res["deleted"]), (1, 0))
        self.assertEqual(self.req("GET", "/api/bookmarks?q=" + quote("Pasta recipe 0"))[1]["items"][0]["title"], "Pasta recipe 0")
        self.assertEqual(self.req("GET", "/api/history")[1]["operations"][0]["command"], "ui-titles")

    def test_export_downloads(self):
        code, raw = self.req("GET", "/api/export?format=raindrop")
        self.assertEqual(code, 200)
        self.assertIn(b"https://a0.example/0", raw)
        self.assertEqual(self.req("GET", "/api/export?format=obsidian")[0], 400)


@unittest.skipUnless(NODE and Path("/opt/node22/lib/node_modules/playwright").exists(), "needs node + playwright")
class Browser(UiCase):
    def test_edit_search_and_undo_through_the_real_page(self):
        script = Path(self.tmp.name, "drive.js")
        script.write_text(f"""
const {{ chromium }} = require('/opt/node22/lib/node_modules/playwright');
(async () => {{
  const b = await chromium.launch({{ executablePath: '/opt/pw-browsers/chromium', args: ['--no-sandbox'] }});
  const p = await b.newPage(); const errs = []; p.on('pageerror', e => errs.push(String(e))); p.on('console', m => m.type() === 'error' && errs.push(m.text()));
  await p.goto('http://127.0.0.1:{self.port}/#token={self.server.token}');
  await p.waitForSelector('.row');
  const first = await p.textContent('#summary');
  await p.fill('#q', 'pasta'); await p.waitForTimeout(700);
  const rows = (await p.$$('#list .row')).length;
  await p.click('#list .row button');
  await p.fill('#eTitle', 'Edited in the UI'); await p.click('#eSave'); await p.waitForTimeout(700);
  await p.fill('#q', 'Edited'); await p.waitForTimeout(700);
  const after = await p.textContent('#list');
  await p.click('#undo'); await p.waitForTimeout(700);
  await p.fill('#q', 'pasta'); await p.waitForTimeout(700);
  const undone = await p.textContent('#list');
  const hash = await p.evaluate(() => location.hash);
  console.log(JSON.stringify({{ errs, first, rows, after: after.includes('Edited in the UI'), undone: undone.includes('Edited in the UI'), hash }}));
  await b.close();
}})();
""")
        out = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        res = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(res["errs"], [])
        self.assertIn("14 bookmarks", res["first"])
        self.assertEqual(res["rows"], 6)
        self.assertTrue(res["after"])
        self.assertFalse(res["undone"])
        self.assertEqual(res["hash"], "")                                   # the token is removed from the address bar


if __name__ == "__main__":
    unittest.main()
