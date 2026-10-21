"""Command-level tests for the user-facing features that the workflow tests only touch in passing."""
import json
import sys
import unittest
from unittest import mock

import common  # noqa: F401
from mhbm import rules as rules_mod
from mhbm.model import Bookmark
from test_cli import CliCase


def csv(*rows, header="url,title,folder,tags,added"):
    return "\n".join([header, *rows])


class Rules(CliCase):
    def fails(self, *args):
        with self.assertRaises(SystemExit) as cm:
            self.run_cli(*args)
        return str(cm.exception.code)

    def seed(self):
        rows = [f"https://news.example.com/a/{i},Daily story {i},Reading/News,news,{1_600_000_000 + i}" for i in range(4)]
        rows += ["https://news.example.com/new,Fresh story,,,1700000000", "https://other.example.org/x,Something else,,,1700000001"]
        self.run_cli("import", self.write("s.csv", csv(*rows)))

    def test_add_list_test_remove(self):
        self.seed()
        self.assertEqual(self.js("rules", "list")["rules"], [])
        added = self.js("rules", "add", "--host", "https://News.Example.com/", "--folder", "reading/news", "--tags", "Daily", "Feed")["added"]
        self.assertEqual((added["id"], added["when"], added["folder"]), (1, {"host": "news.example.com"}, "Reading/News"))  # resolved onto the existing spelling
        self.assertEqual(added["tags"], ["daily", "feed"])
        hit = self.js("rules", "test", "--url", "https://sub.news.example.com/x")["match"]
        self.assertEqual((hit["folder"], hit["source"], hit["confidence"]), ("Reading/News", "rule", 1.0))
        self.assertIsNone(self.js("rules", "test", "--url", "https://notnews.example.com/x")["match"])  # a look-alike suffix is not a subdomain
        self.assertEqual(self.js("rules", "add", "--keyword", "recipe", "--folder", "Food")["added"]["id"], 2)
        self.assertEqual([r["id"] for r in self.js("rules", "list")["rules"]], [1, 2])
        self.run_cli("rules", "remove", "--id", "1")
        self.assertEqual([r["id"] for r in self.js("rules", "list")["rules"]], [2])
        self.assertIn("no rule #9", self.fails("rules", "remove", "--id", "9"))

    def test_bad_rules_are_refused_with_a_reason(self):
        self.assertIn("exactly one", self.fails("rules", "add", "--host", "a.com", "--keyword", "x", "--folder", "F"))
        self.assertIn("exactly one", self.fails("rules", "add", "--host", "a.com"))
        self.run_cli("rules", "add", "--title-regex", "(unclosed", "--folder", "F", expect=2)       # RuleError -> "error: ..." and exit 2
        self.run_cli("rules", "add", "--keyword", "  ", "--folder", "F", expect=2)
        self.assertEqual(self.js("rules", "list")["rules"], [])

    def test_a_regex_that_can_take_forever_is_refused(self):
        import time

        for bad in ["(a+)+$", "(a*)*b", r"(\w+\s?)*$", "(.*a){5}", "(a|b+)*c", "([a-z]+)*x", r"(a)\1", "x" * 300]:
            self.run_cli("rules", "add", "--title-regex", bad, "--folder", "F", expect=2)
            with self.assertRaises(rules_mod.RuleError):
                rules_mod.validate({"id": 1, "when": {"title_regex": bad}, "folder": "F"})
        for good in [r"^\[Docs\]", "k8s|kubernetes", r"\bGPT-?\d+\b", r"(?:foo|bar)\s+\d+", r"(\d+)\.(\d+)", "(ab)?c+"]:
            self.assertEqual(rules_mod.validate({"id": 1, "when": {"title_regex": good}, "folder": "F"})["when"], {"title_regex": good})
        started = time.monotonic()                       # and a long title is only read up to a point
        rule = rules_mod.add([], "title_regex", "^(?:a|b)+x", "F")
        self.assertFalse(rules_mod.matches(Bookmark(url="https://a.com", title="ab" * 100_000), rule))
        self.assertLess(time.monotonic() - started, 2.0)

    def test_every_kind_matches_what_it_says(self):
        def hit(kind, value, url, title=""):
            rule = rules_mod.add([], kind, value, "F")
            return rules_mod.matches(Bookmark(url=url, title=title), rule)
        self.assertTrue(hit("host", "github.com", "https://gist.github.com/x"))
        self.assertFalse(hit("host", "github.com", "https://mygithub.com/x"))
        self.assertTrue(hit("url_prefix", "https://a.com/docs/", "HTTPS://A.COM/DOCS/intro"))
        self.assertFalse(hit("url_prefix", "https://a.com/docs/", "https://a.com/blog/"))
        self.assertTrue(hit("title_regex", r"^how to\b", "https://a.com", "How to bake"))
        self.assertFalse(hit("title_regex", r"^how to\b", "https://a.com", "Why to bake"))
        self.assertTrue(hit("keyword", "rust", "https://a.com", "Learning Rust today"))
        self.assertFalse(hit("keyword", "rust", "https://a.com", "Trustworthy systems"))   # whole words only
        self.assertTrue(hit("keyword", "rust", "https://a.com/rust-lang"))                  # the URL counts too

    def test_suggest_and_apply_then_organize_uses_the_rule(self):
        self.seed()
        found = self.js("rules", "suggest")["suggestions"]
        self.assertEqual([(s["host"], s["folder"], s["filed"], s["unfiled"]) for s in found], [("news.example.com", "Reading/News", 4, 1)])
        self.run_cli("rules", "suggest", "--apply")
        self.assertEqual(self.js("rules", "suggest")["suggestions"], [])                    # nothing left to suggest
        plan = self.dir / "p.json"
        self.run_cli("organize", "--plan-out", str(plan))
        entry = next(v for v in json.loads(plan.read_text()).values() if v["folder"] == "Reading/News")
        self.assertEqual(entry["source"], "rule")
        self.assertIn("your rule #1", entry["reason"])

    def test_rules_file_survives_unicode(self):
        self.js("rules", "add", "--keyword", "نان", "--folder", "غذا/نان")
        text = (self.dir / "lib" / "rules.json").read_text(encoding="utf-8")
        self.assertIn("غذا/نان", text)                                                      # stored as text, not \u escapes
        self.assertEqual(self.js("rules", "list")["rules"][0]["folder"], "غذا/نان")


class Reading(CliCase):
    def seed(self):
        rows = [f"https://r{i}.example.com/p,Article {i},,,{1_600_000_000 + i * 100}" for i in range(5)]
        self.run_cli("import", self.write("r.csv", csv(*rows)))
        return {b.title: b.id for b in self.lib().all()}

    def test_mark_state_favorite_and_undo(self):
        ids = self.seed()
        self.assertEqual(self.js("reading-list")["items"], [])
        self.run_cli("mark", ids["Article 0"], ids["Article 3"], "--state", "unread")
        self.run_cli("mark", ids["Article 1"], "--state", "reading", "--favorite")
        self.assertEqual([i["title"] for i in self.js("reading-list")["items"]], ["Article 0", "Article 1", "Article 3"])   # oldest first
        self.assertEqual([i["title"] for i in self.js("reading-list", "--newest", "--limit", "2")["items"]], ["Article 3", "Article 1"])
        self.assertEqual([i["title"] for i in self.js("reading-list", "--which", "reading")["items"]], ["Article 1"])
        self.assertTrue(self.js("mark", ids["Article 1"], "--unfavorite")["updated"][0]["favorite"] is False)
        self.run_cli("mark", ids["Article 0"], "--state", "none")
        self.assertEqual(self.lib().get(ids["Article 0"]).state, "")
        self.run_cli("undo")
        self.assertEqual(self.lib().get(ids["Article 0"]).state, "unread")

    def test_mark_refuses_nonsense(self):
        ids = self.seed()
        with self.assertRaises(SystemExit) as cm:
            self.run_cli("mark", ids["Article 0"])
        self.assertIn("nothing to do", str(cm.exception.code))
        with self.assertRaises(SystemExit) as cm:
            self.run_cli("mark", "nope", "--state", "read")
        self.assertIn("no bookmark with id nope", str(cm.exception.code))
        self.assertEqual({b.state for b in self.lib().all()}, {""})


class Folders(CliCase):
    def test_listing_rename_and_prune(self):
        rows = ["https://a.com/1,A1,Work/Old,,1", "https://a.com/2,A2,Work/Old/Deep,,2", "https://b.com/1,B1,,,3"]
        self.run_cli("import", self.write("f.csv", csv(*rows)))
        listing = self.js("folders")["folders"]
        self.assertEqual((listing["Work/Old"], listing["Work/Old/Deep"], listing["(unfiled)"]), (1, 1, 1))
        dry = self.js("folders", "--rename", "Work/Old", "Work/New")
        self.assertEqual(dry["moved"], 2)
        self.assertEqual(self.lib().get(next(b.id for b in self.lib().all() if b.title == "A1")).folder, "Work/Old")
        self.run_cli("folders", "--rename", "Work/Old", "Work/New", "--apply")
        self.assertEqual(sorted(b.folder for b in self.lib().all() if b.folder), ["Work/New", "Work/New/Deep"])
        self.assertTrue(self.lib().folders() >= {"Work/New", "Work/New/Deep"})

    def test_prune_empty_is_a_dry_run_unless_applied_and_undoable(self):
        self.run_cli("import", self.write("h.html", '<DL><DT><H3>Full</H3><DL><DT><A HREF="https://x.com/">x</A></DL>'
                                                    "<DT><H3>Empty</H3><DL></DL></DL>"))
        self.assertIn("Empty", self.js("folders", "--prune-empty")["empty"])
        self.assertIn("Empty", self.lib().folders())
        self.run_cli("folders", "--prune-empty", "--apply")
        self.assertNotIn("Empty", self.lib().folders())
        self.run_cli("undo")
        self.assertIn("Empty", self.lib().folders())


class LearnCommand(CliCase):
    def test_not_enough_data_is_said_plainly(self):
        self.run_cli("import", self.write("t.csv", csv("https://a.com/1,One,F,,1", "https://a.com/2,Two,G,,2")))
        info = self.js("learn")
        self.assertFalse(info["ready"])
        self.assertIn("not enough filed bookmarks", self.run_cli("learn"))

    def test_small_library_reports_the_blend_mode(self):
        rows = [f"https://recipes{i % 3}.com/d/{i},Easy pasta recipe tomato sauce {i},Food/Cooking,recipe,{i}" for i in range(8)]
        rows += [f"https://k8s{i % 3}.dev/d/{i},Kubernetes operator controller guide {i},Work/Platform,k8s,{100 + i}" for i in range(8)]
        self.run_cli("import", self.write("b.csv", csv(*rows)))
        info = self.js("learn")
        self.assertEqual((info["ready"], info["mode"]), (True, "blend"))
        self.assertIn("filed bookmarks in 2 folders", self.run_cli("learn"))

    def test_stacked_model_prints_the_accuracy_table_and_caches(self):
        import test_organize
        bms, _ = test_organize.synthetic_library(folders=12, per=14)
        rows = [f"{b.url},{b.title},{b.folder},{b.tags[0]},{b.added}" for b in bms]
        self.run_cli("import", self.write("big.csv", csv(*rows)))
        info = self.js("learn")
        self.assertEqual(info["mode"], "stacked")
        self.assertTrue(0 < info["top1"] <= info["top3"] <= 1)
        text = self.run_cli("learn")
        self.assertIn("90%", text)
        self.assertTrue((self.dir / "lib" / "model.json").exists())
        self.assertEqual(self.js("learn")["top1"], info["top1"])                      # the cache gives the same numbers
        self.assertEqual(self.js("learn", "--retrain")["top1"], info["top1"])         # and so does retraining (seeded)


    def test_seeds_averages_several_splits_without_touching_the_cached_model(self):
        import test_organize
        bms, _ = test_organize.synthetic_library(folders=12, per=14)
        rows = [f"{b.url},{b.title},{b.folder},{b.tags[0]},{b.added}" for b in bms]
        self.run_cli("import", self.write("big.csv", csv(*rows)))
        r = self.js("learn", "--seeds", "2")
        self.assertEqual((r["mode"], r["runs"]), ("stacked", 2))
        lo, hi = r["top1_range"]
        self.assertTrue(lo <= r["top1"] <= hi)
        self.assertFalse((self.dir / "lib" / "model.json").exists())                   # an evaluation, not a training run
        self.assertIn("averaged over 2 different splits", self.run_cli("learn", "--seeds", "2"))
        self.assertEqual(self.js("learn", "--seeds", "1")["mode"], "stacked")          # 1 = the normal, cached path
        self.assertTrue((self.dir / "lib" / "model.json").exists())


    def test_curve_shows_how_accuracy_grows_with_the_data(self):
        import test_organize
        bms, _ = test_organize.synthetic_library(folders=12, per=14)
        rows = [f"{b.url},{b.title},{b.folder},{b.tags[0]},{b.added}" for b in bms]
        self.run_cli("import", self.write("big.csv", csv(*rows)))
        r = self.js("learn", "--curve")["curve"]
        self.assertEqual([x["fraction"] for x in r], [0.25, 0.5, 0.75, 1.0])
        self.assertEqual([x["filed"] for x in r], sorted(x["filed"] for x in r))
        self.assertGreaterEqual(r[-1]["top1"], r[0]["top1"] - 0.02)                       # more data never makes it clearly worse
        self.assertIn("learning from", self.run_cli("learn", "--curve"))
        self.assertFalse((self.dir / "lib" / "model.json").exists())                      # a study, not a training run

    def test_curve_needs_enough_data(self):
        self.run_cli("import", self.write("t.csv", csv("https://a.com/1,One,F,,1", "https://a.com/2,Two,G,,2")))
        self.assertEqual(self.js("learn", "--curve")["curve"], [])

    def test_text_effect_tells_you_what_the_page_text_adds(self):
        import random

        from mhbm.model import Bookmark
        rnd = random.Random(4)
        topics = {f"Topic{i}": [f"term{i}a", f"term{i}b", f"term{i}c", f"term{i}d", f"term{i}e"] for i in range(10)}
        lib = self.lib()
        bms = []
        for name, vocab in topics.items():
            for k in range(16):
                b = Bookmark(url=f"https://site{k % 3}.example/p/{rnd.randint(10**5, 10**6)}", title=f"Resource {k}", folder=f"Lib/{name}", id=f"{name}-{k}",
                             added=1_600_000_000 + rnd.randint(0, 10**7))
                b.content = " ".join(rnd.sample(vocab, 4)) + " background text"
                bms.append(b)
        lib.upsert(bms)
        lib.save_pages(bms)
        r = self.js("learn", "--text-effect")
        self.assertEqual(r["pages"], len(bms))
        self.assertGreater(r["with"]["top1"], r["without"]["top1"] + 0.3)                  # titles say nothing; the text says everything
        self.assertIn("plus the page text", self.run_cli("learn", "--text-effect"))

    def test_text_effect_without_any_archived_page(self):
        self.run_cli("import", self.write("t.csv", csv("https://a.com/1,One,F,,1")))
        self.assertIn("run `archive`", self.run_cli("learn", "--text-effect"))


class DedupeReview(CliCase):
    def seed(self):
        rows = ["https://example.com/post?ref=twitter,Great Article About Cats,Pets,,1",
                "https://example.com/post?ref=newsletter,Great Article About Cats,Reading,,2",
                "https://unrelated.org/page,Something else entirely,Misc,,3"]
        self.run_cli("import", self.write("d.csv", csv(*rows)))

    def test_fuzzy_groups_are_reported_but_never_merged(self):
        self.seed()
        r = self.js("dedupe", "--fuzzy")
        self.assertEqual(r["duplicates"], 0)
        self.assertEqual(len(r["review_groups"]), 1)
        self.assertEqual(len(r["review_groups"][0]["members"]), 2)
        self.run_cli("dedupe", "--fuzzy", "--apply")
        self.assertEqual(self.lib().count(), 3)

    def test_interactive_needs_a_terminal(self):
        self.seed()
        with self.assertRaises(SystemExit) as cm:
            self.run_cli("dedupe", "--interactive")
        self.assertIn("needs a terminal", str(cm.exception.code))

    def test_interactive_merges_the_chosen_keeper_and_can_skip_or_quit(self):
        rows = ["https://example.com/post?ref=twitter,Great Article About Cats,Pets,t1,1",
                "https://example.com/post?ref=newsletter,Great Article About Cats,Reading,t2,2",
                "https://other.org/one?ref=a,Quarterly report of the finance team,Work,,3",
                "https://other.org/one?ref=b,Quarterly report of the finance team,Misc,,4"]
        self.run_cli("import", self.write("i.csv", csv(*rows)))
        with mock.patch.object(sys.stdin, "isatty", return_value=True), mock.patch("builtins.input", side_effect=["x", "9", "2", "q"]):
            self.run_cli("dedupe", "--interactive")                              # junk answers re-ask, "2" merges, "q" stops
        left = self.lib().all()
        self.assertEqual(len(left), 3)
        self.assertEqual(len([b for b in left if "example.com" in b.url]), 1)
        with mock.patch.object(sys.stdin, "isatty", return_value=True), mock.patch("builtins.input", side_effect=["s"]):
            self.run_cli("dedupe", "--interactive")                              # skipping changes nothing
        self.assertEqual(self.lib().count(), 3)
        self.run_cli("undo")
        self.assertEqual(self.lib().count(), 4)

    def test_plan_out_marks_losers_for_deletion_and_keeps_the_best(self):
        self.run_cli("import", self.write("e.csv", csv("https://dup.com/a,Title,Work,t1,5", "https://dup.com/a,Title,Misc,t2,9",
                                                       "https://solo.com,Solo,Work,,1")))
        plan = self.dir / "dd.json"
        self.run_cli("dedupe", "--level", "exact", "--plan-out", str(plan), "--same-folder-only")
        self.assertFalse(any(v.get("delete") for v in json.loads(plan.read_text()).values()))   # different folders: not merged
        self.run_cli("dedupe", "--level", "exact", "--plan-out", str(plan))
        data = json.loads(plan.read_text())
        self.assertEqual(sum(1 for v in data.values() if v.get("delete")), 1)
        keeper = next(v for v in data.values() if not v.get("delete") and "kept" in v.get("reason", ""))
        self.assertEqual(sorted(keeper["tags"]), ["t1", "t2"])                               # tags are merged into the keeper


class Schedule(CliCase):
    def test_prints_cron_and_task_scheduler_lines_for_this_library(self):
        r = self.js("schedule")
        self.assertIn("-m mhbm --home", r["command"])
        self.assertIn(str((self.dir / "lib").resolve()), r["command"])
        text = self.run_cli("schedule")
        self.assertIn("0 3 * * 0", text)
        self.assertIn("schtasks /Create", text)


if __name__ == "__main__":
    unittest.main()
