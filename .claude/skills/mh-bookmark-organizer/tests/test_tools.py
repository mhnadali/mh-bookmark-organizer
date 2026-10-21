import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import common
from mhbm import review
from mhbm.model import Bookmark
from test_cli import CliCase
from test_core import NETSCAPE


class Doctor(CliCase):
    def test_healthy_library_and_repairs(self):
        self.run_cli("import", self.write("in.html", NETSCAPE))
        r = self.js("doctor")
        names = {c["name"]: c["status"] for c in r["checks"]}
        self.assertEqual((names["database"], names["search index"], names["link-check guard"]), ("ok", "ok", "ok"))
        self.assertEqual(r["summary"]["fail"], 0)
        lib = self.lib()
        lib.db.execute("DELETE FROM bm_fts WHERE rowid = (SELECT MIN(rowid) FROM bm_fts)")   # drift: one entry missing
        lib.db.commit()
        self.assertEqual({c["name"]: c["status"] for c in self.js("doctor")["checks"]}["search index"], "warn")
        self.run_cli("doctor", "--fix")
        self.assertEqual({c["name"]: c["status"] for c in self.js("doctor")["checks"]}["search index"], "ok")
        self.assertTrue(self.lib().search("github"))

    def test_index_that_points_at_the_wrong_rows_is_found_and_repaired(self):
        """Same number of entries as bookmarks, but keyed to rows that are not there: a plain count would call that healthy."""
        self.run_cli("import", self.write("in.html", NETSCAPE))
        lib = self.lib()
        n = lib.count()
        lib.db.execute("UPDATE bm_fts SET rowid = rowid + 1000")
        lib.db.commit()
        self.assertEqual(lib.db.execute("SELECT COUNT(*) FROM bm_fts").fetchone()[0], n)
        self.assertEqual(lib.index_gaps(), 2 * n)
        check = {c["name"]: c for c in self.js("doctor")["checks"]}["search index"]
        self.assertEqual(check["status"], "warn")
        self.assertIn("not matching", check["detail"])
        self.run_cli("doctor", "--fix")
        self.assertEqual(self.lib().index_gaps(), 0)
        self.assertTrue(self.lib().search("github"))

    def test_doctor_reports_layout_and_journal_mode(self):
        self.run_cli("import", self.write("in.html", NETSCAPE))
        detail = {c["name"]: c for c in self.js("doctor")["checks"]}["database"]["detail"]
        self.assertIn("layout 3", detail)
        self.assertIn("wal journal", detail)

    def test_vacuum_keeps_search_working_after_rows_are_gone(self):
        lib = self.lib()
        lib.upsert([Bookmark(id=f"id{i}", url=f"https://example.com/{i}", title=f"word{i} common") for i in range(12)])
        lib.delete([f"id{i}" for i in (0, 1, 2, 5, 6)])
        lib.vacuum()
        self.assertEqual(lib.index_gaps(), 0)
        self.assertEqual([b.id for b, _ in lib.search("word9")], ["id9"])
        self.assertEqual(len(lib.search("common")), 7)

    def test_bad_config_is_a_failure_with_exit_code_2(self):
        Path(self.home).mkdir(parents=True, exist_ok=True)
        Path(self.home, "rules.json").write_text("[{broken")
        out = self.run_cli_raw("doctor")
        self.assertEqual(out[0], 2)

    def run_cli_raw(self, *args):
        import contextlib
        import io

        from mhbm import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = cli.main(["--home", self.home, "--quiet", "--json", *args])
            except Exception as e:  # a corrupt file must surface as an error, never a traceback
                code = 2 if isinstance(e, ValueError) else 99
            cli.close_all()
        return code, buf.getvalue()

    def test_network_probes_report_a_blocked_network_honestly(self):
        with common.proxy_env():
            _code, text = self.run_cli_raw("doctor", "--network")
        r = json.loads(text)
        net = {c["name"]: c for c in r["checks"] if c["name"].startswith("network")}
        if net["network: HTTPS"]["status"] == "fail":                 # sandbox without outbound access
            self.assertEqual(net["network: other probes"]["status"], "info")


class Wizard(CliCase):
    def test_unattended_safe_mode_applies_only_non_destructive_steps(self):
        rows = ["url,title,folder"] + [f"https://s{i}.com/{i},(3) Kubernetes operator {i} | Site{i},Dev" for i in range(3)]
        rows += ["https://dup.com/a,Dup,F1", "https://dup.com/a,Dup,F2"]
        src = self.write("w.csv", "\n".join(rows))
        out = self.dir / "o.html"
        with mock.patch("builtins.print"):
            code = self.run_cli_code("wizard", src, "--auto", "safe", "--out", str(out))
        self.assertEqual(code, 0)
        bms = self.lib().all()
        self.assertEqual(sum(1 for b in bms if "dup.com" in b.url), 2)       # duplicates NOT merged in safe mode
        self.assertTrue(all(not b.title.startswith("(3)") for b in bms))      # titles were cleaned
        self.assertTrue(out.exists() and (Path(self.home) / "dashboard.html").exists())

    def test_all_mode_merges_duplicates_and_interactive_answers_are_honoured(self):
        src = self.write("w.csv", "url,title,folder\nhttps://dup.com/a,Dup,F1\nhttps://dup.com/a,Dup,F2\nhttps://x.com,X,F1\n")
        with mock.patch("builtins.print"):
            self.run_cli_code("wizard", src, "--auto", "all")
        self.assertEqual(self.lib().count(), 2)
        # interactive: answer 'n' to everything, then 'q' never needed; nothing should change
        self.setUp()
        with mock.patch("builtins.print"), mock.patch("builtins.input", side_effect=["n"] * 10), mock.patch.dict("os.environ", {"MHBM_WIZARD_SCRIPTED": "1"}):
            self.run_cli_code("wizard", src)
        self.assertEqual(self.lib().count(), 3)

    def test_quit_stops_early_and_empty_library_is_refused(self):
        src = self.write("w.csv", "url,title\nhttps://a.com,A\n")
        with mock.patch("builtins.print") as p, mock.patch("builtins.input", side_effect=["q"]), mock.patch.dict("os.environ", {"MHBM_WIZARD_SCRIPTED": "1"}):
            self.run_cli_code("wizard", src)
        self.assertTrue(any("stopped" in str(c) for c in p.call_args_list))
        self.setUp()
        with self.assertRaises(SystemExit):
            self.run_cli_code("wizard", "--auto", "safe")

    def test_wizard_refuses_to_prompt_without_a_terminal(self):
        with mock.patch("sys.stdin.isatty", return_value=False), self.assertRaises(SystemExit):
            self.run_cli_code("wizard", self.write("w.csv", "url\nhttps://a.com\n"))

    def run_cli_code(self, *args):
        import contextlib
        import io

        from mhbm import cli
        with contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["--home", self.home, "--quiet", *args])
        cli.close_all()
        return code


class ReviewPage(CliCase):
    def plan(self):
        return {"a": {"folder": "Dev/Docs", "source": "learned", "level": "exact", "confidence": 0.93, "reason": "similar to X",
                      "alternatives": [["Dev/Other", 0.4]], "tags": ["x"]},
                "b": {"title": "New title", "reason": "cleaned"}, "c": {"url": "https://c.com/slug", "reason": "canonical"},
                "d": {"delete": True, "reason": "duplicate"}}

    def library(self):
        return [Bookmark(url=f"https://{i}.com", title=f"T{i} </script><b>x", id=i) for i in "abcd"]

    def test_html_is_inert_and_complete(self):
        html = review.render(self.plan(), self.library(), {"Dev/Docs"})
        data = html.split('id="data" type="application/json">')[1].split("</script>")[0]
        self.assertNotIn("</", data)
        parsed = json.loads(data.replace("<\\/", "</"))
        self.assertEqual([i["id"] for i in parsed["items"]], ["a", "b", "c", "d"])
        self.assertEqual(parsed["items"][0]["alternatives"], [["Dev/Other", 0.4]])

    def test_command_writes_the_page(self):
        self.run_cli("import", self.write("in.csv", "url,title\nhttps://a.com,A\n"))
        bid = self.lib().all()[0].id
        plan = self.write("p.json", json.dumps({bid: {"folder": "X"}}))
        out = self.dir / "r.html"
        self.assertEqual(self.js("review", plan, "--out", str(out))["items"], 1)
        self.assertIn("Review plan", out.read_text())

    @unittest.skipUnless(shutil.which("node") and Path("/opt/node22/lib/node_modules/playwright").exists(), "needs node + playwright")
    def test_browser_interaction_produces_the_edited_plan(self):
        page = self.dir / "review.html"
        page.write_text(review.render(self.plan(), self.library(), {"Dev/Docs", "Dev/Other"}), encoding="utf-8")
        script = self.dir / "drive.js"
        script.write_text(f"""
const {{ chromium }} = require('/opt/node22/lib/node_modules/playwright');
(async () => {{
  const b = await chromium.launch({{ executablePath: '/opt/pw-browsers/chromium', args: ['--no-sandbox'] }});
  const p = await b.newPage({{ acceptDownloads: true }});
  const errs = []; p.on('pageerror', e => errs.push(String(e))); p.on('console', m => m.type() === 'error' && errs.push(m.text()));
  await p.goto('file://{page}');
  const boxes = await p.$$('input[type=checkbox]');
  await boxes[3].uncheck();                                   // reject the delete suggestion
  await p.fill('input[aria-label=folder]', 'Dev/Other');        // override the folder of item a
  const [dl] = await Promise.all([p.waitForEvent('download'), p.click('#dl')]);
  const path = await dl.path(); const text = require('fs').readFileSync(path, 'utf8');
  await p.fill('#conf', '95');                                  // confidence filter hides the 93% item
  const shown = (await p.textContent('#count'));
  console.log(JSON.stringify({{ errs, plan: JSON.parse(text), shown }}));
  await b.close();
}})();
""")
        out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=90)
        self.assertEqual(out.returncode, 0, out.stderr)
        res = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(res["errs"], [])
        self.assertEqual(res["plan"]["a"], {"folder": "Dev/Other", "tags": ["x"], "suggested": "Dev/Docs", "confidence": 0.93,
                                            "level": "exact", "source": "learned", "reviewed": True})
        self.assertEqual(res["plan"]["b"], {"title": "New title"})
        self.assertEqual(res["plan"]["c"], {"url": "https://c.com/slug"})
        self.assertNotIn("d", res["plan"])
        self.assertIn("shown", res["shown"])

    @unittest.skipUnless(shutil.which("node") and Path("/opt/node22/lib/node_modules/playwright").exists(), "needs node + playwright")
    def test_only_a_used_page_counts_as_a_review(self):
        plan = {"a": {"folder": "Dev/Docs", "source": "learned", "level": "exact", "confidence": 0.93},
                "b": {"folder": "Dev/Other", "source": "learned", "level": "parent", "confidence": 0.88}}
        page = self.dir / "review2.html"
        page.write_text(review.render(plan, self.library(), {"Dev/Docs", "Dev/Other"}), encoding="utf-8")
        script = self.dir / "drive2.js"
        script.write_text(f"""
const {{ chromium }} = require('/opt/node22/lib/node_modules/playwright');
(async () => {{
  const b = await chromium.launch({{ executablePath: '/opt/pw-browsers/chromium', args: ['--no-sandbox'] }});
  const p = await b.newPage({{ acceptDownloads: true }});
  await p.goto('file://{page}');
  const grab = async () => {{ const [dl] = await Promise.all([p.waitForEvent('download'), p.click('#dl')]);
    return JSON.parse(require('fs').readFileSync(await dl.path(), 'utf8')); }};
  const untouched = await grab();                              // downloaded as proposed, nothing looked at
  const boxes = await p.$$('input[type=checkbox]');
  await boxes[1].uncheck();                                    // turn down the second suggestion
  const used = await grab();
  console.log(JSON.stringify({{ untouched, used }}));
  await b.close();
}})();
""")
        out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=90)
        self.assertEqual(out.returncode, 0, out.stderr)
        res = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual({k: v["reviewed"] for k, v in res["untouched"].items()}, {"a": False, "b": False})
        self.assertEqual(res["used"]["a"], {"folder": "Dev/Docs", "suggested": "Dev/Docs", "confidence": 0.93, "level": "exact",
                                            "source": "learned", "reviewed": True})
        self.assertEqual(res["used"]["b"], {"rejected": True, "suggested": "Dev/Other", "confidence": 0.88, "level": "parent",
                                            "source": "learned", "reviewed": True})


if __name__ == "__main__":
    unittest.main()
