import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import common
from mhbm import cli
from mhbm.store import Library
from test_core import NETSCAPE


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # CLI calls keep their Library handles open until explicitly closed.
        # Register this after tmp.cleanup so unittest's LIFO cleanup closes
        # SQLite handles before Windows removes the temporary directory.
        self.addCleanup(cli.close_all)
        self.dir = Path(self.tmp.name)
        self.home = str(self.dir / "lib")

    def run_cli(self, *args, expect=0):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--home", self.home, "--quiet", *args])
            cli.close_all()
        self.assertEqual(code, expect, err.getvalue() + out.getvalue())
        return out.getvalue()

    def js(self, *args):
        return json.loads(self.run_cli("--json", *args))

    def lib(self):
        lib = Library(self.home)
        self.addCleanup(lib.close)
        return lib

    def write(self, name, text):
        p = self.dir / name
        p.write_text(text, encoding="utf-8")
        return str(p)


class Workflow(CliCase):
    def test_import_dedupe_undo_and_plans(self):
        src = self.write("in.html", NETSCAPE)
        self.assertEqual(self.js("import", src)["imported"], 3)
        st = self.js("stats")
        self.assertEqual((st["total"], st["duplicate_extras"], st["empty_folders"]), (3, 1, ["Bookmarks bar/Empty"]))
        self.run_cli("dedupe")                                    # dry run
        self.assertEqual(self.lib().count(), 3)
        plan = self.dir / "dd.json"
        self.run_cli("dedupe", "--plan-out", str(plan))
        entries = json.loads(plan.read_text())
        self.assertEqual(sum(1 for e in entries.values() if e.get("delete")), 1)
        self.run_cli("apply-plan", str(plan), "--apply")
        self.assertEqual(self.lib().count(), 2)
        self.run_cli("undo")
        self.assertEqual(self.lib().count(), 3)
        self.run_cli("dedupe", "--apply")
        self.assertEqual(self.lib().count(), 2)
        ops = self.js("history")["operations"]
        self.assertEqual([o["command"] for o in ops], ["dedupe", "apply-plan", "import"])
        self.assertEqual([o["undone"] for o in ops], [0, 1, 0])
        self.run_cli("undo", "--op", str(self.js("history")["operations"][0]["op"]))
        self.assertEqual(self.lib().count(), 3)

    def test_reimporting_the_same_export_does_not_double_the_library(self):
        src = self.write("in.html", NETSCAPE)
        self.assertEqual(self.js("import", src)["imported"], 3)
        again = self.js("import", src)
        self.assertEqual((again["imported"], again["skipped_existing"], again["library"]), (0, 3, 3))
        self.assertEqual(self.js("import", src, "--allow-duplicates")["library"], 6)
        self.assertEqual(self.lib().count(), 6)
        self.assertEqual(len({b.id for b in self.lib().all()}), 6)

    def test_clean_urls_only_touches_tracking(self):
        src = self.write("in.csv", "url,title\nhttps://a.com/p?b=2&a=1#x,plain\nhttps://a.com/q?utm_source=n&id=7,tracked\n")
        self.run_cli("import", src)
        self.assertEqual(self.js("clean-urls")["count"], 1)
        self.run_cli("clean-urls", "--apply")
        urls = sorted(b.url for b in self.lib().all())
        self.assertEqual(urls, ["https://a.com/p?b=2&a=1#x", "https://a.com/q?id=7"])

    def test_learning_organize_via_reviewable_plan(self):
        rows = ["url,title,folder,tags"]
        for i in range(8):
            rows.append(f"https://recipes{i % 3}.com/d/{i},Easy pasta recipe tomato sauce {i},Food/Cooking,recipe")
            rows.append(f"https://k8s{i % 3}.dev/d/{i},Kubernetes operator controller guide {i},Work/Platform,k8s")
        rows += ["https://recipes1.com/new,Quick tomato pasta recipe,,", "https://github.com/a/b,repo,,", "https://zzz.org/q,Quantum gardening,,"]
        self.run_cli("import", self.write("c.csv", "\n".join(rows)))
        info = self.js("learn")
        self.assertTrue(info["ready"])
        plan = self.dir / "plan.json"
        self.run_cli("organize", "--plan-out", str(plan))
        self.assertEqual(len(json.loads(plan.read_text())), 1)   # only the learned one: github would need a brand-new folder
        self.run_cli("organize", "--allow-new-folders", "--plan-out", str(plan))
        data = json.loads(plan.read_text())
        self.assertEqual(len(data), 2)                       # the unknowable one is left for the AI pass
        self.assertEqual({v["source"] for v in data.values()}, {"learned", "rules"})
        self.assertEqual(self.lib().get(next(i for i, v in data.items() if v["source"] == "learned")).folder, "")  # nothing applied yet
        self.run_cli("apply-plan", str(plan), "--apply")
        folders = {b.url: b.folder for b in self.lib().all()}
        self.assertEqual(folders["https://recipes1.com/new"], "Food/Cooking")
        self.assertEqual(folders["https://github.com/a/b"], "Dev/Code Hosting")
        batch = self.dir / "batch.json"
        self.run_cli("ai-batch", "--out", str(batch))
        self.assertEqual([i["host"] for i in json.loads(batch.read_text())["items"]], ["zzz.org"])
        # the AI may reuse an existing folder of any depth, but may not invent a deeper-than-3 new one
        ai = self.dir / "ai.json"
        zid = json.loads(batch.read_text())["items"][0]["id"]
        ai.write_text(json.dumps({zid: {"folder": "Food/Cooking", "tags": ["x"]}}))
        self.assertEqual(self.js("apply-plan", str(ai))["rejected"], [])

    def test_restructure_cli_prunes_emptied_folders(self):
        rows = ["url,title,folder"] + [f"https://r{i}.com,t{i},Tiny/Sub" for i in range(2)] + ["https://lone.com,lone,Tiny/Single"]
        self.run_cli("import", self.write("c.csv", "\n".join(rows)))
        self.run_cli("restructure", "--apply")
        self.assertEqual(sorted(b.folder for b in self.lib().all()), ["Tiny", "Tiny/Sub", "Tiny/Sub"])
        self.assertNotIn("Tiny/Single", self.lib().folders())

    def test_rename_prune_and_tag(self):
        self.run_cli("import", self.write("in.html", NETSCAPE))
        self.run_cli("folders", "--rename", "Bookmarks bar/Reading", "Read", "--apply")
        bm = next(b for b in self.lib().all() if "example.com/post/" in b.url)
        self.assertEqual(bm.folder, "Read")
        self.run_cli("tag", bm.id, "--add", "Later", "--remove", "x", "--title", "Renamed")
        self.assertEqual((self.lib().get(bm.id).tags, self.lib().get(bm.id).title), (["later"], "Renamed"))
        self.assertEqual(self.js("folders", "--prune-empty")["empty"], ["Bookmarks bar/Empty"])
        self.run_cli("folders", "--prune-empty", "--apply")
        self.assertNotIn("Bookmarks bar/Empty", self.lib().folders())

    def test_exports(self):
        self.run_cli("import", self.write("in.html", NETSCAPE))
        out = self.dir / "o.html"
        self.run_cli("export", "--format", "html", "--out", str(out))
        self.assertIn(["Bookmarks bar", "Empty"][1], out.read_text())
        vault = self.dir / "vault"
        self.run_cli("export", "--format", "obsidian", "--out", str(vault))
        self.assertTrue(list(vault.rglob("*.md")))
        with self.assertRaises(SystemExit):
            self.run_cli("export", "--format", "obsidian")
        filtered = self.dir / "f.json"
        self.run_cli("export", "--format", "json", "--folder", "Bookmarks bar/Reading", "--out", str(filtered))
        self.assertEqual(len(json.loads(filtered.read_text())["bookmarks"]), 1)

    def test_install_chrome_guards(self):
        self.run_cli("import", self.write("in.html", NETSCAPE))
        target = self.dir / "Bookmarks"
        target.write_text('{"roots":{}}')
        with self.assertRaises(SystemExit):
            self.run_cli("install-chrome", str(target))
        self.assertEqual(target.read_text(), '{"roots":{}}')
        self.run_cli("install-chrome", str(target), "--confirm-chrome-closed")
        self.assertEqual(len(list(self.dir.glob("Bookmarks.mhbm-backup-*"))), 1)
        self.assertEqual(len(json.loads(target.read_text())["roots"]["bookmark_bar"]["children"]), 3)
        with self.assertRaises(SystemExit):
            self.run_cli("install-chrome", str(self.dir / "notbookmarks.json"), "--confirm-chrome-closed")

    def test_config_file_and_flag_precedence(self):
        Path(self.home).mkdir(parents=True)
        Path(self.home, "config.json").write_text(json.dumps({"workers": 3, "extra_tracking_params": ["sessionid"]}))
        self.assertEqual(self.js("config")["effective"]["workers"], 3)
        self.run_cli("import", self.write("in.csv", "url\nhttps://a.com/p?sessionid=9&id=1\n"))
        self.assertEqual(self.js("clean-urls")["count"], 1)
        Path(self.home, "config.json").write_text("{not json")
        self.run_cli("config", expect=2)

    def test_config_never_prints_a_secret(self):
        Path(self.home).mkdir(parents=True)
        Path(self.home, "config.json").write_text(json.dumps({"notify": {
            "smtp_password": "hunter2", "webhook_url": "https://hooks.example/T000/SECRETTOKEN", "api_key": "k-123",
            "smtp": {"host": "mail.example", "user": "me", "pass": "p4ss"}, "proxy": "http://bob:swordfish@proxy.example:3128/",
            "desktop": True, "to": ["me@example.com"]}}))
        for text in (self.run_cli("config"), json.dumps(self.js("config"))):
            for secret in ("hunter2", "SECRETTOKEN", "k-123", "p4ss", "swordfish"):
                self.assertNotIn(secret, text)
        notify = self.js("config")["effective"]["notify"]
        self.assertEqual((notify["smtp_password"], notify["smtp"]["host"], notify["desktop"]), ("(set; hidden)", "mail.example", True))
        self.assertIn("proxy.example", notify["proxy"])        # the address stays visible, only the login part is hidden

    def test_completion_is_valid_bash(self):
        script = self.dir / "c.sh"
        script.write_text(self.run_cli("completion", "bash"))
        if shutil.which("bash") and os.name != "nt":   # Windows "bash" is often WSL, which cannot read the temp path
            self.assertEqual(subprocess.run(["bash", "-n", str(script)]).returncode, 0)
        self.assertIn("complete -F _mhbm mhbm", script.read_text())

    def test_error_page_titles_are_reported(self):
        rows = "url,title\nhttps://a.com,example-host.net | 522: Connection timed out\nhttps://b.com,404 Not Found\nhttps://c.com,Just a moment...\nhttps://d.com,How to fix a 404 error in nginx and apache for beginners guide\nhttps://e.com,Great article\n"
        self.run_cli("import", self.write("e.csv", rows))
        titles = [t["title"] for t in self.js("stats")["error_titles"]]
        self.assertEqual(sorted(titles), ["404 Not Found", "Just a moment...", "example-host.net | 522: Connection timed out"])

    def test_errors_are_clean(self):
        self.run_cli("import", str(self.dir / "missing.html"), expect=2)
        self.run_cli("import", self.write("bad.json", "{oops"), expect=2)
        with self.assertRaises(SystemExit):
            self.run_cli("undo")


class Network(CliCase):
    def test_check_prune_archive_search_dashboard(self):
        with common.Server() as s:
            rows = ["url,title"] + [f"{s.base}{p},{p}" for p in ("/ok", "/gone", "/moved", "/forbidden")]
            self.run_cli("import", self.write("n.csv", "\n".join(rows)))
            flags = ["--per-host", "4", "--confirmations", "2", "--min-gap-hours", "0"]
            r = self.js("check", *flags, "--follow-redirects")
            self.assertEqual((r["status"], r["redirects_rewritten"]), ({"ok": 1, "suspect": 1, "redirect": 1, "restricted": 1}, 1))
            self.assertEqual(self.js("prune")["count"], 0)       # suspects are never pruned by default
            r = self.js("check", *flags, "--recheck-suspect")
            self.assertEqual(r["status"]["dead"], 1)
            self.assertEqual(self.js("prune")["count"], 1)
            self.run_cli("prune", "--apply")
            self.assertEqual(self.lib().count(), 3)
            self.run_cli("undo")
            self.assertEqual(self.lib().count(), 4)
            self.run_cli("prune", "--apply")

            self.assertEqual(self.js("archive", "--unarchived", "--per-host", "4")["indexed"], 2)
            hits = self.js("search", "kubernetes", "operators")["results"]
            self.assertEqual(len(hits), 2)
            self.assertTrue(hits[0]["saved_copy"].startswith("archive/"))
            self.assertEqual(len(self.js("search", "is:archived", "controllers")["results"]), 2)
            self.assertEqual(self.js("archive", "--unarchived")["indexed"], 0)  # idempotent

            dash = self.dir / "d.html"
            self.run_cli("dashboard", "--out", str(dash))
            html = dash.read_text()
            self.assertIn("Bookmark library", html)
            self.assertNotIn("</script><", html.split('id="data"')[1].split("</script>")[0])
            script = re.findall(r"<script>(.*?)</script>", html, re.S)[-1]
            node = shutil.which("node")
            if node:
                f = self.dir / "dash.js"
                f.write_text(script)
                self.assertEqual(subprocess.run([node, "--check", str(f)], capture_output=True).returncode, 0)


class Offline(CliCase):
    def test_check_records_nothing_when_the_network_is_down(self):
        rows = ["url,title"] + [f"http://host{i}.invalid/page,t{i}" for i in range(14)]
        self.run_cli("import", self.write("n.csv", "\n".join(rows)))
        with common.proxy_env():
            r = self.js("check", "--workers", "8", "--timeout", "3")
        self.assertTrue(r["offline"])
        self.assertEqual(r["status"], {"unchecked": 14})
        self.assertTrue(all(b.fail_count == 0 and b.status == "" for b in self.lib().all()))
        self.assertEqual(self.js("prune", "--status", "dead", "suspect")["count"], 0)


if __name__ == "__main__":
    unittest.main()
