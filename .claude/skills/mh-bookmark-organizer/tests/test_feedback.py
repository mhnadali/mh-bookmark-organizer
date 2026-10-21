"""Learning from what you do with the suggestions: storage, capture on every channel, undo, calibration, rejection memory."""
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import common  # noqa: F401
import test_organize
from mhbm import feedback, learn, nativehost
from mhbm.model import Bookmark
from mhbm.store import Library
from test_cli import CliCase
from test_webui import UiCase


def library_csv(folders=12, per=14):
    filed, unfiled = test_organize.synthetic_library(folders, per)
    rows = ["url,title,folder,tags,added"]
    rows += [f"{b.url},{b.title},{b.folder},{b.tags[0]},{b.added}" for b in filed]
    rows += [f"{b.url},{b.title},,,{b.added}" for b in unfiled]
    return "\n".join(rows)


class Flow(CliCase):
    """A stacked-model library with a dozen unfiled bookmarks the model is sure about."""

    def setUp(self):
        super().setUp()
        self.run_cli("import", self.write("lib.csv", library_csv()))
        self.plan_path = str(self.dir / "plan.json")

    def issue(self):
        self.run_cli("organize", "--plan-out", self.plan_path)
        return json.loads(Path(self.plan_path).read_text())

    def rows(self, **where):
        lib = self.lib()
        return [r for r in lib.feedback_rows() if all(r[k] == v for k, v in where.items())]

    def test_a_plan_file_records_what_was_suggested_and_what_became_of_it(self):
        plan = self.issue()
        self.assertEqual(len(plan), 12)
        lib = self.lib()
        self.assertEqual(lib.db.execute("SELECT COUNT(*) FROM suggestions WHERE status='open'").fetchone()[0], 12)
        victim = next(iter(plan))
        other = next(v["folder"] for k, v in plan.items() if k != victim)
        plan[victim]["folder"] = other                                    # the user edits one entry before applying
        Path(self.plan_path).write_text(json.dumps(plan))
        out = self.js("apply-plan", self.plan_path, "--apply")
        self.assertEqual(out["feedback"], {"accepted": 11, "corrected": 1, "rejected": 0})
        rows = self.rows()
        self.assertEqual(len(rows), 12)
        self.assertEqual({r["channel"] for r in rows}, {"plan-file"})
        self.assertEqual({r["explicit"] for r in rows}, {0})              # nobody looked at them one by one: recorded, not counted
        corrected = next(r for r in rows if r["action"] == "corrected")
        self.assertEqual((corrected["final"], corrected["suggested"] != other), (other, True))
        self.assertEqual(self.lib().db.execute("SELECT COUNT(*) FROM suggestions WHERE status='open'").fetchone()[0], 0)
        self.assertEqual(feedback.outcomes(self.lib()), [])                # so the confidence check has nothing to learn from yet

    def test_reviewed_decisions_are_explicit_and_a_rejection_is_not_an_error(self):
        plan = self.issue()
        ids = list(plan)
        reviewed = {}
        for i in ids[:-1]:
            e = plan[i]
            reviewed[i] = {"folder": e["folder"], "suggested": e["folder"], "confidence": e["confidence"], "level": e["level"],
                           "source": e["source"], "reviewed": True}
        no = plan[ids[-1]]
        reviewed[ids[-1]] = {"rejected": True, "suggested": no["folder"], "confidence": no["confidence"], "level": no["level"],
                             "source": no["source"], "reviewed": True}
        Path(self.plan_path).write_text(json.dumps(reviewed))
        out = self.js("apply-plan", self.plan_path, "--apply")
        self.assertEqual((out["valid"], out["rejected"], out["feedback"]), (11, [], {"accepted": 11, "corrected": 0, "rejected": 1}))
        self.assertEqual({r["explicit"] for r in self.rows()}, {1})
        self.assertEqual({r["channel"] for r in self.rows()}, {"review"})
        self.assertEqual(self.lib().get(ids[-1]).folder, "")              # a turned-down suggestion changes nothing
        outcomes = feedback.outcomes(self.lib())
        self.assertEqual((len(outcomes), sum(ok for _, ok in outcomes)), (12, 11))

    def test_a_folder_you_turned_down_is_not_suggested_again_until_the_decision_is_undone(self):
        plan = self.issue()
        victim = next(iter(plan))
        refused = plan[victim]["folder"]
        Path(self.plan_path).write_text(json.dumps({victim: {"rejected": True, "suggested": refused, "confidence": plan[victim]["confidence"],
                                                              "level": "exact", "source": "learned", "reviewed": True}}))
        self.run_cli("apply-plan", self.plan_path, "--apply")
        again = self.issue()
        self.assertNotEqual(again.get(victim, {}).get("folder"), refused)  # absent, or somewhere else, but never the refused folder
        self.assertEqual(len(again), 11 + (1 if victim in again else 0))
        self.run_cli("undo")                                              # taking the decision back brings the suggestion back
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.issue()[victim]["folder"], refused)

    def test_undoing_an_applied_plan_forgets_its_outcomes_and_reopens_the_suggestions(self):
        self.issue()
        self.run_cli("apply-plan", self.plan_path, "--apply")
        self.assertEqual(len(self.rows()), 12)
        self.run_cli("undo")
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.lib().db.execute("SELECT COUNT(*) FROM suggestions WHERE status='open'").fetchone()[0], 12)

    def test_a_new_plan_supersedes_the_old_suggestion(self):
        self.issue()
        self.issue()
        db = self.lib().db
        self.assertEqual(db.execute("SELECT COUNT(*) FROM suggestions WHERE status='open'").fetchone()[0], 12)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM suggestions WHERE status='superseded'").fetchone()[0], 12)

    def test_the_feedback_command(self):
        text = self.run_cli("feedback")
        self.assertIn("no decisions recorded yet", text)
        plan = self.issue()
        victim = next(iter(plan))
        reviewed = {i: {"folder": e["folder"], "suggested": e["folder"], "confidence": e["confidence"], "level": "exact",
                        "source": "learned", "reviewed": True} for i, e in plan.items() if i != victim}
        reviewed[victim] = {"rejected": True, "suggested": plan[victim]["folder"], "confidence": plan[victim]["confidence"],
                            "level": "exact", "source": "learned", "reviewed": True}
        Path(self.plan_path).write_text(json.dumps(reviewed))
        self.run_cli("apply-plan", self.plan_path, "--apply")
        st = self.js("feedback")
        self.assertEqual((st["total"], st["explicit"], st["by_action"]), (12, 12, {"accepted": 11, "rejected": 1}))
        top = st["bands"][-1]
        self.assertEqual((top["decisions"], top["accepted"]), (12, 11))
        self.assertIn("accepted 92%", self.run_cli("feedback"))
        self.assertEqual(len(self.js("feedback", "list", "--limit", "5")["decisions"]), 5)
        self.assertEqual(self.js("feedback", "clear")["cleared"], 0)       # dry run
        self.assertEqual(len(self.rows()), 12)
        self.assertEqual(self.js("feedback", "clear", "--apply")["cleared"], 12)
        self.assertEqual(self.rows(), [])


class Outcomes(unittest.TestCase):
    def test_no_data_changes_nothing(self):
        self.assertEqual(learn.Outcomes([]).adjust(0.9), 0.9)

    def test_overconfidence_is_pulled_down(self):
        rnd = random.Random(1)
        records = [(0.9, rnd.random() < 0.5) for _ in range(300)]          # said 90%, was right half the time
        adjusted = learn.Outcomes(records).adjust(0.9)
        self.assertLess(adjusted, 0.62)
        self.assertGreater(adjusted, 0.4)
        self.assertLess(learn.Outcomes(records).adjust(0.9), learn.Outcomes(records).adjust(1.0) + 1e-9)   # still ordered

    def test_calibrated_outcomes_leave_the_confidence_alone(self):
        rnd = random.Random(2)
        records = [(c, rnd.random() < c) for c in (0.6, 0.7, 0.8, 0.9, 0.95) for _ in range(300)]
        o = learn.Outcomes(records)
        for c in (0.6, 0.7, 0.8, 0.9):
            self.assertAlmostEqual(o.adjust(c), c, delta=0.06)

    def test_underconfidence_is_lifted_but_slowly(self):
        few = learn.Outcomes([(0.725, True)] * 3).adjust(0.725)
        many = learn.Outcomes([(0.725, True)] * 400).adjust(0.725)
        self.assertLess(few, 0.8)                                          # three successes cannot move it far
        self.assertGreater(many, 0.9)

    def test_a_handful_of_failures_cannot_swing_it(self):
        self.assertGreater(learn.Outcomes([(0.9, False)] * 4).adjust(0.9), 0.75)
        self.assertGreater(learn.Outcomes([(0.98, False)]).adjust(0.7), 0.65)       # and one failure at the top barely touches the rest

    def test_the_map_is_monotone_and_stays_a_probability(self):
        rnd = random.Random(3)
        o = learn.Outcomes([(rnd.uniform(0.5, 1.0), rnd.random() < 0.7) for _ in range(500)])
        grid = [i / 200 for i in range(0, 201)]
        values = [o.adjust(x) for x in grid]
        self.assertTrue(all(0.0 <= v <= 1.0 for v in values))
        self.assertTrue(all(a <= b + 1e-12 for a, b in zip(values, values[1:])))
        self.assertEqual(o.adjust(0.3), 0.3)                               # below what is ever suggested: untouched

    def test_a_model_that_was_wrong_in_practice_stops_answering(self):
        filed, unfiled = test_organize.synthetic_library(12, 14)
        model = learn.FolderModel(filed + unfiled)
        probe = unfiled[0]
        first = model.predict(probe, 0.85)
        self.assertIsNotNone(first)
        model.set_outcomes([(first["confidence"], False)] * 400)           # every answer around that confidence was rejected
        self.assertIsNone(model.predict(probe, 0.85))
        model.set_outcomes([])
        self.assertIsNotNone(model.predict(probe, 0.85))                   # and clearing the history restores it


class Excluding(unittest.TestCase):
    def setUp(self):
        self.filed, self.unfiled = test_organize.synthetic_library(12, 14)
        self.model = learn.FolderModel(self.filed + self.unfiled)

    def test_an_excluded_folder_is_never_returned(self):
        probe = self.unfiled[0]
        best = self.model.predict(probe, 0.5)
        self.assertIsNotNone(best)
        for hit in (self.model.predict(probe, 0.0, exclude={best["folder"]}), ):
            if hit:
                self.assertNotEqual(hit["folder"], best["folder"])
        self.assertNotIn(best["folder"], [f for f, _ in self.model.rank(probe, 50, exclude={best["folder"]})])
        self.assertIn(best["folder"], [f for f, _ in self.model.rank(probe, 50)])

    def test_predict_many_applies_per_bookmark_exclusions(self):
        probe = self.unfiled[1]
        best = self.model.predict(probe, 0.5)["folder"]
        got = self.model.predict_many(self.unfiled[:3], 0.5, exclude={probe.id: {best}})
        self.assertNotEqual((got[probe.id] or {}).get("folder"), best)
        self.assertIsNotNone(got[self.unfiled[0].id])


class Storage(unittest.TestCase):
    def test_an_older_library_gains_the_new_tables(self):
        with tempfile.TemporaryDirectory() as d:
            db = sqlite3.connect(Path(d) / "library.db")
            db.executescript("""
                CREATE TABLE bookmarks(id TEXT PRIMARY KEY, url TEXT NOT NULL, norm TEXT, title TEXT, folder TEXT, tags TEXT,
                  notes TEXT, added INTEGER, status TEXT, http_code INTEGER, last_checked INTEGER, fail_count INTEGER DEFAULT 0,
                  first_failed INTEGER DEFAULT 0, wayback TEXT, archive_path TEXT, meta TEXT, state TEXT DEFAULT '', favorite INTEGER DEFAULT 0);
                INSERT INTO bookmarks(id, url, norm, title, folder, tags, added) VALUES('x1','https://a.com','a.com/','A','Dev','[]',5);
                PRAGMA user_version = 2;""")
            db.commit()
            db.close()
            lib = Library(d)
            try:
                self.assertEqual(lib.count(), 1)
                tables = {r[0] for r in lib.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({"suggestions", "feedback"} <= tables)
                self.assertEqual(lib.db.execute("PRAGMA user_version").fetchone()[0], 3)
                lib.record_suggestions([{"bm_id": "x1", "norm": "a.com/", "folder": "Dev", "confidence": 0.9}])
                self.assertEqual(lib.open_suggestion("x1")["folder"], "Dev")
            finally:
                lib.close()

    def test_feedback_recorded_outside_an_operation_is_not_tied_to_the_next_one(self):
        with tempfile.TemporaryDirectory() as d:
            lib = Library(d)
            try:
                lib.upsert([Bookmark(url="https://a.com", title="a", id="a1", norm="a.com/"), Bookmark(url="https://b.com", title="b", id="b1", norm="b.com/")])
                lib.add_feedback(bm_id="a1", norm="a.com/", host="a.com", suggested="X", final="X", action="accepted", channel="plan-file")
                with lib.op("something"):
                    lib.upsert([Bookmark(url="https://b.com", title="changed", id="b1", norm="b.com/")])
                lib.undo()
                self.assertEqual(len(lib.feedback_rows()), 1)               # the earlier decision is not an effect of that operation
            finally:
                lib.close()


class Determinism(unittest.TestCase):
    def test_training_does_not_depend_on_the_hash_seed(self):
        code = ("import sys, json; sys.path[:0] = [sys.argv[1], sys.argv[2]]\n"
                "import common, test_organize\n"
                "from mhbm import learn\n"
                "filed, unfiled = test_organize.synthetic_library(12, 14)\n"
                "m = learn.FolderModel(filed)\n"
                "print(json.dumps([m.report, [m.predict(b, 0.5) for b in unfiled]], sort_keys=True))\n")
        here = Path(__file__).resolve().parent
        outs = []
        for seed in ("1", "2", "3"):
            env = {**os.environ, "PYTHONHASHSEED": seed}
            r = subprocess.run([sys.executable, "-c", code, str(here), str(here.parent / "scripts")], capture_output=True, text=True, env=env, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr)
            outs.append(r.stdout)
        self.assertEqual(outs[0], outs[1])
        self.assertEqual(outs[1], outs[2])


class HostChannel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        filed, unfiled = test_organize.synthetic_library(12, 14)
        lib = Library(self.tmp.name)
        from mhbm.model import assign_ids
        everything = filed + unfiled
        for b in everything:
            b.norm = b.url
        assign_ids(everything, set())
        lib.upsert(everything)
        lib.add_folders({b.folder for b in filed})
        lib.close()
        self.host = nativehost.Host(self.tmp.name)
        self.addCleanup(self.host.close)
        self.unfiled = unfiled

    def test_saving_with_the_suggestion_records_acceptance_and_correction(self):
        url = "https://cooking1.example.com/fresh"
        guess = self.host.cmd_suggest({"url": url, "title": "pasta sauce tomato recipe cooking tip"})
        self.assertEqual(guess["folder"], "Hobbies/Cooking")
        self.host.cmd_save({"url": url, "title": "pasta sauce tomato recipe cooking tip", "folder": guess["folder"],
                            "suggested": guess["folder"], "confidence": guess["confidence"], "level": guess["level"], "source": guess["source"]})
        url2 = "https://cooking2.example.com/other"
        self.host.cmd_save({"url": url2, "title": "second", "folder": "Hobbies/Travel", "suggested": "Hobbies/Cooking", "confidence": 0.9,
                            "level": "exact", "source": "learned"})
        rows = {r["norm"]: r for r in self.host.lib.feedback_rows()}
        self.assertEqual(rows[url]["action"], "accepted")
        self.assertEqual((rows[url2]["action"], rows[url2]["final"], rows[url2]["suggested"]), ("corrected", "Hobbies/Travel", "Hobbies/Cooking"))
        self.assertEqual({r["channel"] for r in rows.values()}, {"extension"})
        self.assertEqual({r["explicit"] for r in rows.values()}, {1})

    def test_a_save_without_a_suggestion_records_nothing(self):
        self.host.cmd_save({"url": "https://x.example.com/p", "title": "p", "folder": "Hobbies/Travel"})
        self.assertEqual(self.host.lib.feedback_rows(), [])

    def test_a_turned_down_folder_is_not_offered_for_that_page_again(self):
        url = "https://cooking1.example.com/fresh2"
        title = "pasta sauce tomato recipe cooking tip"
        first = self.host.cmd_suggest({"url": url, "title": title})["folder"]
        self.host.cmd_save({"url": url, "title": title, "folder": "Hobbies/Music", "suggested": first, "confidence": 0.9, "level": "exact", "source": "learned"})
        self.host.lib.delete([r for r in self.host.lib.ids() if self.host.lib.get(r).norm == url])  # the page is saved again later
        self.host._invalidate()
        second = self.host.cmd_suggest({"url": url, "title": title})
        self.assertNotEqual(second["folder"], first)
        self.assertNotIn(first, [a["folder"] for a in second["alternatives"]])

    def test_moves_applied_in_bulk_are_recorded_but_do_not_count_as_a_review(self):
        plan = self.host.cmd_plan({"limit": 50})
        self.assertGreater(plan["total"], 5)
        self.host.cmd_apply_moves({"moves": [{"url": m["url"], "from": m["from"], "to": m["to"], "tags": m["tags"]} for m in plan["moves"]]})
        rows = self.host.lib.feedback_rows()
        self.assertEqual({r["channel"] for r in rows}, {"extension-moves"})
        self.assertEqual({r["explicit"] for r in rows}, {0})
        self.assertEqual({r["action"] for r in rows}, {"accepted"})


class WebChannel(UiCase):
    def setUp(self):
        super().setUp()
        lib = Library(self.tmp.name)
        lib.upsert([Bookmark(url="https://a9.example/9", title="Kubernetes operator 9", id="n1"),
                    Bookmark(url="https://food.example/9", title="Pasta recipe 9 | Site", id="n2")])
        lib.close()

    def rows(self):
        lib = Library(self.tmp.name)
        try:
            return lib.feedback_rows()
        finally:
            lib.close()

    def test_unticking_is_a_rejection_and_ticking_beside_it_is_a_reviewed_acceptance(self):
        status, res = self.req("GET", "/api/plan?kind=organize")
        self.assertEqual(status, 200)
        self.assertTrue({"n1", "n2"} <= set(res["plan"]))                  # (the library's own unfiled duplicate is in there too)
        self.assertEqual(self.rows(), [])
        status, out = self.req("POST", "/api/apply", {"kind": "organize", "plan": {"n1": res["plan"]["n1"]}, "declined": ["n2"], "reviewed": ["n1"]})
        self.assertEqual((status, out["feedback"]), (200, {"accepted": 1, "corrected": 0, "rejected": 1}))
        rows = {r["bm_id"]: r for r in self.rows()}
        self.assertEqual((rows["n1"]["action"], rows["n1"]["channel"], rows["n1"]["explicit"]), ("accepted", "ui", 1))
        self.assertEqual((rows["n2"]["action"], rows["n2"]["channel"], rows["n2"]["explicit"]), ("rejected", "ui", 1))
        lib = Library(self.tmp.name)
        self.addCleanup(lib.close)
        self.assertEqual((lib.get("n1").folder, lib.get("n2").folder), ("Dev", ""))

    def test_applying_everything_without_looking_is_recorded_but_does_not_count(self):
        _, res = self.req("GET", "/api/plan?kind=organize")
        status, out = self.req("POST", "/api/apply", {"kind": "organize", "plan": res["plan"]})
        self.assertEqual((status, out["feedback"]["accepted"]), (200, len(res["plan"])))
        self.assertEqual({(r["channel"], r["explicit"]) for r in self.rows()}, {("ui-unreviewed", 0)})

    def test_other_plan_kinds_record_nothing(self):
        _, res = self.req("GET", "/api/plan?kind=titles")
        status, _ = self.req("POST", "/api/apply", {"kind": "titles", "plan": res["plan"] or {"n1": {"title": "x"}}})
        self.assertEqual(status, 200)
        self.assertEqual(self.rows(), [])


class Machinery(unittest.TestCase):
    def test_pooling_blends_each_bookmark_with_its_nearest_sitting_mates(self):
        a, b, c = {"X": 1.0}, {"Y": 1.0}, {"Y": 1.0}
        mixed = learn.pool_probs([a, b, c], [0, 10, 20], own=0.5, k=2)
        self.assertEqual(mixed[0], {"X": 0.5, "Y": 0.5})                   # its mates both say Y
        self.assertEqual(mixed[1], {"X": 0.25, "Y": 0.75})                 # one mate says X, one says Y
        self.assertEqual(learn.pool_probs([a], [0]), [a])                  # alone: unchanged
        near = learn.pool_probs([{"X": 1.0}, {"Y": 1.0}, {"Z": 1.0}], [0, 5, 500], own=0.0, k=1)
        self.assertEqual((near[0].get("Y"), near[0].get("Z", 0.0)), (1.0, 0.0))   # only the nearest in time counts

    def test_candidates_include_the_neighbours_of_the_best_guess(self):
        train = [Bookmark(url=f"https://s{i}.example/{i}", title=f"alpha beta {i}", folder="A/B/C", id=f"c{i}", added=1000 + i) for i in range(6)]
        train += [Bookmark(url=f"https://t{i}.example/{i}", title=f"gamma delta {i}", folder="A/B/D", id=f"d{i}", added=900000 + i) for i in range(6)]
        train += [Bookmark(url=f"https://u{i}.example/{i}", title=f"epsilon zeta {i}", folder="A/B", id=f"p{i}", added=5000000 + i) for i in range(3)]
        train += [Bookmark(url=f"https://v{i}.example/{i}", title=f"eta theta {i}", folder="Z", id=f"z{i}", added=9000000 + i) for i in range(6)]
        base = learn.Base(train)
        probe = Bookmark(url="https://new.example/x", title="alpha beta new", id="q", added=1500)
        _, folders, rows = base.candidate_rows(probe)
        self.assertIn("A/B/C", folders)
        self.assertTrue({"A/B/D", "A/B"} <= set(folders))                  # sibling and parent of the best guess are candidates
        rel = dict(zip(folders, (r[-6:] for r in rows)))
        self.assertEqual(rel["A/B/C"][0], 1.0)                             # it is the leading candidate
        self.assertEqual((rel["A/B/D"][1], rel["A/B/D"][0]), (1.0, 0.0))   # a sibling
        self.assertEqual(rel["A/B"][2], 1.0)                               # an ancestor
        self.assertTrue(all(len(r) == len(rows[0]) for r in rows))
        old = learn.EXPAND
        learn.EXPAND = False
        try:
            _, plain, _ = base.candidate_rows(probe)
        finally:
            learn.EXPAND = old
        self.assertLessEqual(len(plain), len(folders))

    def test_isotonic_pools_equal_inputs_so_a_tied_lookup_sees_one_value(self):
        cal = learn.isotonic([0.5] * 4 + [0.9] * 4, [0, 0, 1, 1, 1, 1, 1, 0])
        self.assertEqual(learn.calibrated(cal, 0.5), 0.5)
        self.assertEqual(learn.calibrated(cal, 0.9), 0.75)
        self.assertEqual(learn.isotonic([], [])["pts"], [])


if __name__ == "__main__":
    unittest.main()
