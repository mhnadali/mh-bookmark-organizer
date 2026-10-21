"""Triage: grouping unfiled bookmarks, one decision per group, the CLI, the web UI and the learning that follows."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import common  # noqa: F401
import test_organize
from mhbm import learn, triage
from mhbm.model import Bookmark, assign_ids
from mhbm.store import Library
from test_cli import CliCase
from test_webui import UiCase


def bm(i, url, title="", folder="", added=0):
    return Bookmark(url=url, title=title or f"t{i}", folder=folder, id=f"b{i}", added=added)


class Grouping(unittest.TestCase):
    def test_the_same_site_forms_a_group_even_across_days_and_other_sites_do_not_join(self):
        day = 86400
        bms = [bm(1, "https://www.shop.example/a", added=1000), bm(2, "https://shop.example/b", added=1000 + 5 * day),
               bm(3, "https://shop.example/c", added=1000 + 9 * day), bm(4, "https://blog.shop.example/x", added=1000 + 30 * day),
               bm(5, "https://other.example/y", added=1000 + 31 * day)]
        clusters = triage.build(bms)
        self.assertEqual(sorted(len(c.members) for c in clusters), [1, 1, 3])      # a sub-domain is a different site
        site = next(c for c in clusters if len(c.members) == 3)
        self.assertEqual((site.kind, sorted(b.id for b in site.members)), ("site", ["b1", "b2", "b3"]))

    def test_things_saved_together_group_when_their_guesses_agree(self):
        class Fake:
            ready = True

            def distribution(self, b, exclude=()):
                return {"Food": 0.8, "Travel": 0.1} if "pasta" in b.title else {"Travel": 0.7, "Food": 0.1}

        bms = [bm(1, "https://a.example/1", "pasta one", added=1000), bm(2, "https://b.example/2", "pasta two", added=1100),
               bm(3, "https://c.example/3", "beach", added=1200), bm(4, "https://d.example/4", "pasta far away", added=900000)]
        clusters = triage.build(bms, Fake())
        together = next(c for c in clusters if {b.id for b in c.members} == {"b1", "b2"})
        self.assertEqual((together.folder, together.kind), ("Food", "sitting"))
        self.assertAlmostEqual(together.support, 0.8)
        self.assertEqual(together.agreement, 1.0)
        self.assertEqual(together.alternatives[0][0], "Travel")
        self.assertNotIn("Food", [f for f, _ in together.alternatives])     # alternatives never repeat the suggestion
        lone = next(c for c in clusters if {b.id for b in c.members} == {"b4"})   # same guess but a different day: not linked
        self.assertEqual(lone.kind, "single")

    def test_a_group_never_grows_beyond_the_cap(self):
        bms = [bm(i, f"https://big.example/{i}", added=1000 + i) for i in range(75)]
        clusters = triage.build(bms)
        self.assertEqual(sum(len(c.members) for c in clusters), 75)
        self.assertTrue(all(len(c.members) <= triage.MAX_CLUSTER for c in clusters))

    def test_your_rules_group_what_they_file(self):
        from mhbm import rules as rules_mod
        rules = [rules_mod.add([], "keyword", "recipe", "Food/Recipes")]
        bms = [bm(1, "https://x.example/1", "best recipe"), bm(2, "https://y.example/2", "another recipe"), bm(3, "https://z.example/3", "unrelated")]
        clusters = triage.build(bms, rules=rules, known_folders={"Food/Recipes"})
        rule = next(c for c in clusters if c.source == "rules")
        self.assertEqual((rule.folder, rule.support, len(rule.members)), ("Food/Recipes", 1.0, 2))

    def test_only_unfiled_bookmarks_are_grouped_and_filed_ones_are_left_alone(self):
        bms = [bm(1, "https://a.example/1", folder="Dev"), bm(2, "https://a.example/2"), bm(3, "https://a.example/3")]
        clusters = triage.build(bms)
        self.assertEqual([sorted(b.id for b in c.members) for c in clusters], [["b2", "b3"]])
        self.assertEqual(triage.build([bm(1, "https://a.example/1", folder="Dev")]), [])

    def test_a_cluster_id_names_its_members_and_nothing_else(self):
        a = triage.Cluster(members=[bm(1, "https://a.example/1"), bm(2, "https://a.example/2")])
        b = triage.Cluster(members=[bm(2, "https://a.example/2"), bm(1, "https://a.example/1")])
        c = triage.Cluster(members=[bm(1, "https://a.example/1"), bm(3, "https://a.example/3")])
        self.assertEqual(a.id, b.id)
        self.assertNotEqual(a.id, c.id)

    def test_sites(self):
        self.assertEqual(triage.site_of("https://www.news.example.co.uk/x"), "news.example.co.uk")
        self.assertEqual(triage.site_of("https://A.B.example.com/"), "a.b.example.com")
        self.assertEqual(triage.site_of("not a url"), "")

    def test_the_plan_only_contains_groups_that_clear_the_bars(self):
        strong = triage.Cluster(members=[bm(1, "https://a.example/1"), bm(2, "https://a.example/2")], folder="X", support=0.8)
        weak = triage.Cluster(members=[bm(3, "https://b.example/3"), bm(4, "https://b.example/4")], folder="Y", support=0.1)
        lone = triage.Cluster(members=[bm(5, "https://c.example/5")], folder="Z", support=0.9)
        none = triage.Cluster(members=[bm(6, "https://d.example/6")])
        plan = triage.to_plan([strong, weak, lone, none], min_support=0.3, min_size=2)
        self.assertEqual(sorted(plan), ["b1", "b2"])
        self.assertEqual((plan["b1"]["folder"], plan["b1"]["source"], plan["b1"]["cluster"]), ("X", "triage", strong.id))
        self.assertEqual(sorted(triage.to_plan([strong, weak, lone, none])), ["b1", "b2", "b3", "b4", "b5"])


class Deciding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lib = Library(self.tmp.name)
        self.addCleanup(self.lib.close)
        self.bms = [bm(i, f"https://a.example/{i}", added=1000 + i) for i in range(4)]
        for b in self.bms:
            b.norm = b.url
        self.lib.upsert(self.bms + [Bookmark(url="https://f.example/1", title="filed", folder="Food", id="f1", norm="https://f.example/1")])
        self.lib.add_folders(["Food"])
        self.cluster = triage.Cluster(members=list(self.bms), folder="Food", support=0.7)

    def test_filing_moves_everyone_but_the_members_set_aside_and_records_each_outcome(self):
        with self.lib.op("triage"):
            res = triage.decide(self.lib, self.cluster, "food", exclude_ids=["b3"])          # case-insensitive match onto the existing folder
        self.assertEqual((res["filed"], res["left"], res["folder"]), (3, 1, "Food"))
        self.assertEqual({self.lib.get(f"b{i}").folder for i in range(3)}, {"Food"})
        self.assertEqual(self.lib.get("b3").folder, "")
        rows = self.lib.feedback_rows()
        self.assertEqual({(r["action"], r["channel"], r["explicit"]) for r in rows}, {("accepted", "triage", 1)})
        self.assertEqual(len(rows), 3)

    def test_choosing_another_folder_is_a_correction_and_a_new_folder_is_created(self):
        with self.lib.op("triage"):
            res = triage.decide(self.lib, self.cluster, "Travel/Beach")
        self.assertEqual(res["folder"], "Travel/Beach")
        self.assertIn("Travel/Beach", self.lib.folders())
        self.assertEqual({r["action"] for r in self.lib.feedback_rows()}, {"corrected"})
        self.lib.undo()                                                                    # one undo takes the whole decision back
        self.assertEqual({self.lib.get(f"b{i}").folder for i in range(4)}, {""})
        self.assertEqual(self.lib.feedback_rows(), [])

    def test_rejecting_changes_nothing_but_is_remembered(self):
        with self.lib.op("triage"):
            res = triage.decide(self.lib, self.cluster, "", action="reject")
        self.assertEqual((res["action"], res["filed"]), ("reject", 0))
        self.assertEqual({self.lib.get(f"b{i}").folder for i in range(4)}, {""})
        self.assertEqual({r["action"] for r in self.lib.feedback_rows()}, {"rejected"})
        from mhbm import feedback
        self.assertEqual(feedback.rejections(self.lib), {b.url: {"Food"} for b in self.bms})

    def test_skipping_records_nothing(self):
        with self.lib.op("triage"):
            res = triage.decide(self.lib, self.cluster, "", action="skip")
        self.assertEqual((res["action"], res["left"]), ("skip", 4))
        self.assertEqual(self.lib.feedback_rows(), [])


class Learning(unittest.TestCase):
    def test_filing_a_few_bookmarks_teaches_the_model_about_the_rest(self):
        filed, _ = test_organize.synthetic_library(12, 14)
        model = learn.FolderModel(filed)
        probe = Bookmark(url="https://newsite.example/p", title="zzqx unique wording", id="probe", added=filed[-1].added + 10)
        self.assertFalse(model.distribution(probe).get("Hobbies/Music", 0) > 0.3)
        taught = [Bookmark(url=f"https://newsite.example/{i}", title=f"zzqx unique wording {i}", folder="Hobbies/Music", id=f"t{i}",
                           added=filed[-1].added + i) for i in range(4)]
        self.assertEqual(model.add_examples(taught + [taught[0]]), 4)                     # a repeat is not added twice
        self.assertEqual(model.add_examples([Bookmark(url="https://x.example", title="unfiled", id="u9")]), 0)
        self.assertGreater(model.distribution(probe).get("Hobbies/Music", 0), 0.3)
        self.assertEqual(len(model.docs), len(filed) + 4)

    def test_a_model_that_is_not_ready_learns_nothing(self):
        model = learn.FolderModel([])
        self.assertEqual(model.add_examples([Bookmark(url="https://x.example", title="t", folder="A", id="a")]), 0)


def triage_csv():
    filed, unfiled = test_organize.synthetic_library(12, 14)
    rows = ["url,title,folder,tags,added"]
    rows += [f"{b.url},{b.title},{b.folder},{b.tags[0]},{b.added}" for b in filed]
    rows += [f"{b.url},{b.title},,,{b.added}" for b in unfiled]
    for u in unfiled[:5]:                                   # five topics gain a small batch saved together from one site
        host = u.url.split("/")[2]
        for k in range(1, 4):
            rows.append(f"https://{host}/more{k},{u.title} extra{k},,,{u.added + 60 * k}")
    return "\n".join(rows)


class Cli(CliCase):
    def setUp(self):
        super().setUp()
        self.run_cli("import", self.write("lib.csv", triage_csv()))

    def test_the_report_counts_decisions_not_bookmarks(self):
        r = self.js("triage")
        self.assertEqual(r["unfiled"], 12 + 1 + 15)
        self.assertLess(r["decisions"], r["unfiled"])
        self.assertEqual(r["multi_clusters"], 5)
        self.assertTrue(all(c["size"] >= 1 and c["members"] for c in r["clusters"]))
        top = r["clusters"][0]
        self.assertEqual((top["size"], top["kind"]), (4, "site"))
        text = self.run_cli("triage")
        self.assertIn("decisions (5 clusters", text)
        self.assertIn("interactively", text)

    def test_plan_out_is_reviewable_and_remembers_what_it_suggested(self):
        plan_file = self.dir / "t.json"
        self.run_cli("triage", "--plan-out", str(plan_file), "--min-size", "2")
        plan = json.loads(plan_file.read_text())
        self.assertEqual(len(plan), 20)                                     # five groups of four
        self.assertEqual({e["source"] for e in plan.values()}, {"triage"})
        self.assertEqual(self.lib().db.execute("SELECT COUNT(*) FROM suggestions WHERE status='open'").fetchone()[0], 20)
        out = self.js("apply-plan", str(plan_file), "--apply")
        self.assertEqual(out["feedback"]["accepted"], 20)                   # untouched plan file: recorded, not counted as a review
        self.assertEqual({r["explicit"] for r in self.lib().feedback_rows()}, {0})

    def test_apply_files_the_confident_groups_only(self):
        out = self.js("triage", "--apply", "--min-size", "2", "--min-support", "0.9")
        self.assertEqual(out["valid"], 20)
        self.assertEqual(sum(1 for b in self.lib().all() if not b.folder), 8)    # the singles are untouched

    def test_interactive_mode_needs_a_terminal(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_cli("triage", "--interactive")
        self.assertIn("needs a terminal", str(cm.exception.code))

    def test_walking_through_the_groups(self):
        script = iter([("", None), ("t", "Misc/Elsewhere"), ("x", "1"), ("r", None), ("q", None)])
        pending: list = []

        def answer(prompt=""):
            if pending:
                return pending.pop(0)
            cmd, extra = next(script)
            if cmd == "t":
                pending.append(extra)
            elif cmd == "x":
                pending.extend([extra, ""])        # set the first bookmark aside, then accept the suggestion for the others
            return cmd

        with mock.patch.object(sys.stdin, "isatty", return_value=True), mock.patch("builtins.input", side_effect=answer):
            text = self.run_cli("triage", "--interactive")
        self.assertIn("11 bookmarks filed in 4 decisions, 1 suggestions turned down", text)
        lib = self.lib()
        folders = [b.folder for b in lib.all() if b.folder == "Misc/Elsewhere"]
        self.assertEqual(len(folders), 4)
        rows = lib.feedback_rows()
        self.assertEqual({r["channel"] for r in rows}, {"triage"})
        self.assertEqual({r["explicit"] for r in rows}, {1})
        by_action = {a: sum(1 for r in rows if r["action"] == a) for a in ("accepted", "corrected", "rejected")}
        self.assertEqual(by_action["corrected"], 4)
        self.assertEqual(by_action["rejected"], 4)
        self.assertEqual(by_action["accepted"], 4 + 3)
        # the member that was set aside is still unfiled, and the rejected group stays unfiled too
        self.assertEqual(sum(1 for b in lib.all() if not b.folder), 28 - 11)
        self.run_cli("undo")                                                # the last decision (the rejection) is undoable on its own
        self.assertEqual(len(self.lib().feedback_rows()), len(rows) - 4)


class Web(UiCase):
    def setUp(self):
        super().setUp()
        lib = Library(self.tmp.name)
        extra = [Bookmark(url=f"https://brandnew.example/{i}", title=f"Kubernetes operator guide {i}", id=f"g{i}", added=5_000_000 + i) for i in range(3)]
        for b in extra:
            b.norm = b.url
        lib.upsert(extra)
        lib.close()

    def clusters(self):
        status, res = self.req("GET", "/api/triage")
        self.assertEqual(status, 200)
        return res

    def test_listing_and_deciding(self):
        res = self.clusters()
        group = next(c for c in res["clusters"] if c["size"] == 3)
        self.assertEqual((group["kind"], group["folder"]), ("site", "Dev"))
        self.assertLess(res["decisions"], res["unfiled"])
        status, out = self.req("POST", "/api/triage", {"cluster": group["id"], "action": "file", "folder": "Dev", "exclude": ["g2"]})
        self.assertEqual((status, out["filed"], out["errors"]), (200, 2, []))
        lib = Library(self.tmp.name)
        self.addCleanup(lib.close)
        self.assertEqual(([lib.get(f"g{i}").folder for i in range(3)]), ["Dev", "Dev", ""])
        self.assertEqual({(r["channel"], r["explicit"]) for r in lib.feedback_rows()}, {("ui", 1)})

    def test_stale_or_incomplete_decisions_are_refused_politely(self):
        status, out = self.req("POST", "/api/triage", {"cluster": "doesnotexist", "action": "file", "folder": "X"})
        self.assertEqual((status, out["filed"]), (200, 0))
        self.assertIn("reload", out["errors"][0])
        group = next(c for c in self.clusters()["clusters"] if c["size"] == 3)
        status, out = self.req("POST", "/api/triage", {"decisions": [{"cluster": group["id"], "action": "file", "folder": "   "},
                                                                   {"cluster": group["id"], "action": "explode", "folder": "X"}]})
        self.assertEqual(len(out["errors"]), 2)
        self.assertEqual(status, 200)
        self.assertEqual(out["filed"], 0)

    def test_the_token_is_required(self):
        self.assertEqual(self.req("GET", "/api/triage", token=False)[0], 403)
        self.assertEqual(self.req("POST", "/api/triage", {"cluster": "x"}, token=False)[0], 403)


@unittest.skipUnless(shutil.which("node") and Path("/opt/node22/lib/node_modules/playwright").exists(), "needs node + playwright")
class WebBrowser(Web):
    def test_the_triage_tab_in_a_real_browser(self):
        script = Path(self.tmp.name, "drive.js")
        script.write_text(f"""
const {{ chromium }} = require('/opt/node22/lib/node_modules/playwright');
(async () => {{
  const b = await chromium.launch({{ executablePath: '/opt/pw-browsers/chromium', args: ['--no-sandbox'] }});
  const p = await b.newPage(); const errs = []; p.on('pageerror', e => errs.push(String(e))); p.on('console', m => m.type() === 'error' && errs.push(m.text()));
  p.on('dialog', d => d.dismiss());
  await p.goto('http://127.0.0.1:{self.port}/#token={self.server.token}');
  await p.waitForSelector('.row');
  await p.click('#tTri');
  await p.waitForSelector('#triList .panel');
  const header = await p.textContent('#triList .meta');
  const cards = (await p.$$('#triList > .panel')).length;
  const card = await p.$('#triList > .panel:has-text("brandnew.example")');
  const boxes = await card.$$('input[type=checkbox]');
  await boxes[2].uncheck();                                     // set the third member aside
  const buttons = await card.$$('button');
  let go; for (const x of buttons) {{ if ((await x.textContent()).startsWith('File')) go = x; }}
  await go.click();
  await p.waitForTimeout(1200);
  const left = (await p.$$('#triList > .panel')).length;
  console.log(JSON.stringify({{ errs, header, cards, left }}));
  await b.close();
}})();
""")
        out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        res = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(res["errs"], [])
        self.assertIn("decisions", res["header"])
        self.assertEqual(res["left"], res["cards"] - 1)
        lib = Library(self.tmp.name)
        self.addCleanup(lib.close)
        self.assertEqual([lib.get(f"g{i}").folder for i in range(3)], ["Dev", "Dev", ""])
        self.assertEqual({(r["channel"], r["explicit"]) for r in lib.feedback_rows()}, {("ui", 1)})


class Synthetic(unittest.TestCase):
    """Floors on the synthetic fixture: grouping really does reduce the number of decisions and stays accurate."""

    def test_confident_groups_are_nearly_always_right_and_decisions_drop(self):
        import copy
        from pathlib import Path

        from mhbm.formats import read_file
        bms = list(read_file(Path(__file__).resolve().parent / "fixtures" / "messy-library.html"))
        assign_ids([b for b in bms if not b.id], set())
        filed = [b for b in bms if b.folder not in ("", "Bookmarks bar", "Other bookmarks")]
        fold = learn._fold_of(filed, 5, 1)
        held = [copy.copy(b) for b, x in zip(filed, fold) if x == 0]
        train = [b for b, x in zip(filed, fold) if x != 0]
        truth = {b.id: b.folder for b in held}
        for b in held:
            b.folder = ""
        model = learn.FolderModel(train)
        clusters = triage.build(held, model)
        self.assertLess(len(clusters), 0.65 * len(held))                      # measured: about 0.4-0.45
        confident = [c for c in clusters if len(c.members) > 1 and c.support >= 0.5]
        self.assertGreater(len(confident), 8)
        members = sum(len(c.members) for c in confident)
        right = sum(1 for c in confident for b in c.members if truth[b.id] == c.folder)
        self.assertGreaterEqual(right / members, 0.9)


if __name__ == "__main__":
    unittest.main()
