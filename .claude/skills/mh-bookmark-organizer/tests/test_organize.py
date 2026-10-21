import json
import plistlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

import common  # noqa: F401
from mhbm import browsers, categorize, formats, learn, restructure
from mhbm.model import Bookmark


def corpus():
    """Two well-populated user folders plus unfiled look-alikes."""
    bms = []
    for i in range(8):
        bms.append(Bookmark(url=f"https://recipes{i % 3}.com/dish/{i}", title=f"Easy pasta recipe with tomato sauce {i}",
                            folder="Food/Cooking", tags=["recipe"], id=f"f{i}"))
        bms.append(Bookmark(url=f"https://k8s{i % 3}.dev/docs/{i}", title=f"Kubernetes operator controller guide {i}",
                            folder="Work/Platform", tags=["k8s"], id=f"w{i}"))
    return bms


def synthetic_library(folders=12, per=14, seed=5):
    """A library with topic folders, sittings (sessions) and a few unfiled bookmarks per topic."""
    import random

    rnd = random.Random(seed)
    topics = [("cooking", "recipe pasta tomato sauce kitchen baking"), ("kubernetes", "kubernetes operator controller cluster pod"),
              ("travel", "hotel flight itinerary beach passport"), ("finance", "stock invest budget loan bank"),
              ("music", "guitar album concert playlist chord"), ("gardening", "plant soil seed compost garden"),
              ("photo", "camera lens aperture portrait editing"), ("fitness", "workout running protein muscle training"),
              ("python", "python function decorator generator asyncio"), ("health", "sleep nutrition vitamin doctor therapy"),
              ("cars", "engine tire hybrid sedan mileage"), ("books", "novel author chapter library fiction")][:folders]
    bms, t = [], 1_600_000_000
    for name, vocab in topics:
        words_ = vocab.split()
        for i in range(per):
            t += rnd.choice([60, 120, 4000, 90000])        # some saved together, some days apart
            title = " ".join(rnd.sample(words_, 3)) + f" {name} tip {i}"
            bms.append(Bookmark(url=f"https://{name}{i % 4}.example.com/a/{i}", title=title, folder=f"Hobbies/{name.title()}",
                                added=t, tags=[name], id=f"{name}{i}"))
    unfiled = []
    for name, vocab in topics:
        t += 5000
        unfiled.append(Bookmark(url=f"https://{name}1.example.com/new", title=" ".join(rnd.sample(vocab.split(), 3)) + f" {name} guide",
                                added=t, id=f"u-{name}"))
    unfiled.append(Bookmark(url="https://zzz.example.org/x", title="completely unrelated qwxz plmk", added=t + 99, id="u-odd"))
    return bms, unfiled


class Learn(unittest.TestCase):
    def test_small_library_uses_a_fixed_blend(self):
        m = learn.FolderModel(corpus())
        self.assertEqual((m.ready, m.mode), (True, "blend"))
        p = m.predict(Bookmark(url="https://recipes1.com/new", title="Quick tomato pasta recipe", id="n1"))
        self.assertEqual((p["folder"], p["level"]), ("Food/Cooking", "exact"))
        self.assertIn("recipe", p["tags"])
        q = m.predict(Bookmark(url="https://k8s2.dev/x", title="Writing a Kubernetes controller", id="n2"))
        self.assertEqual(q["folder"], "Work/Platform")

    def test_unsure_returns_none_and_tiny_library_not_ready(self):
        m = learn.FolderModel(corpus())
        self.assertIsNone(m.predict(Bookmark(url="https://zzz.org/q", title="Quantum gardening", id="n3")))
        self.assertFalse(learn.FolderModel(corpus()[:4]).ready)

    def test_stacked_model_is_trained_calibrated_and_reports_honest_numbers(self):
        filed, unfiled = synthetic_library()
        m = learn.FolderModel(filed)
        self.assertEqual(m.mode, "stacked")
        r = m.report
        self.assertGreater(r["top1"], 0.6)
        self.assertGreaterEqual(r["top3"], r["top1"])
        t = r["targets"]["0.8"]
        self.assertGreater(t["overall"]["share"], 0.3)
        self.assertGreater(t["overall"]["precision"], 0.8)           # calibrated: answers are mostly right
        right = 0
        for b in unfiled[:-1]:
            p = m.predict(b, 0.8)
            self.assertIsNotNone(p, b.id)
            right += p["folder"] == "Hobbies/" + b.id[2:].title()
        self.assertGreaterEqual(right, 10)
        self.assertIsNone(m.predict(unfiled[-1], 0.8))               # nonsense -> no answer
        alts = m.rank(unfiled[0], 3)
        self.assertEqual(len(alts), 3)
        self.assertAlmostEqual(sum(p for _, p in m.rank(unfiled[0], 100)), 1.0, places=3)

    def test_model_is_cached_and_invalidated_when_filing_changes(self):
        filed, _ = synthetic_library(folders=8, per=12)
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d, "model.json")
            seen = []
            learn.FolderModel(filed, cache=cache, progress=seen.append, stack_min=60)
            self.assertIn("calibrating", seen)
            seen.clear()
            m2 = learn.FolderModel(filed, cache=cache, progress=seen.append, stack_min=60)
            self.assertNotIn("calibrating", seen)                        # loaded, not retrained
            self.assertEqual(m2.mode, "stacked")
            filed[0].folder = "Elsewhere"
            seen.clear()
            learn.FolderModel(filed, cache=cache, progress=seen.append, stack_min=60)
            self.assertIn("calibrating", seen)                           # the filing changed -> retrain
            cache.write_text("not json")
            seen.clear()
            learn.FolderModel(filed, cache=cache, progress=seen.append, stack_min=60)  # corrupt cache is ignored
            self.assertIn("calibrating", seen)

    def test_answers_one_level_up_when_siblings_look_alike(self):
        filed, _ = synthetic_library(folders=6, per=16)
        for b in filed:                                                  # two near-identical sub-folders per topic
            b.folder += "/A" if int(b.id[-1]) % 2 == 0 else "/B"
            b.title = b.title.rsplit(" tip", 1)[0] + " tip"
        m = learn.FolderModel(filed)
        probe = Bookmark(url="https://cooking1.example.com/new", title="recipe pasta kitchen cooking tip", added=filed[-1].added + 4000, id="pz")
        p = m.predict(probe, 0.7, max_up=1)
        self.assertIsNotNone(p)
        self.assertTrue(p["folder"].startswith("Hobbies/Cooking"))

    def test_root_wrapper_folders_are_not_training_labels(self):
        bms = corpus() + [Bookmark(url=f"https://dump.com/{i}", title="x", folder="Other bookmarks", id=f"o{i}") for i in range(12)]
        self.assertNotIn("Other bookmarks", learn.FolderModel(bms).folders)


class Degenerate(unittest.TestCase):
    def test_a_library_where_no_folder_can_be_judged_falls_back_to_the_blend(self):
        """Every bookmark in a folder of its own: nothing can be held out and judged. It used to end in an IndexError."""
        bms = [Bookmark(url=f"https://site{i}.example/p{i}", title=f"title {i}", folder=f"Folder {i}", added=1_600_000_000 + i, id=f"s{i}")
               for i in range(200)]
        model = learn.FolderModel(bms)
        self.assertTrue(model.ready)
        self.assertEqual(model.mode, "blend")
        self.assertIn("held-out", model.report["note"])
        result = model.predict(Bookmark(url="https://site3.example/other", title="title 3", id="new"))
        self.assertIsInstance(result, dict)          # it answers (uncalibrated) instead of crashing


class Primitives(unittest.TestCase):
    def test_isotonic_is_monotone_and_calibrated_lookup(self):
        cal = learn.isotonic([0.1, 0.2, 0.3, 0.4, 0.5, 0.6], [0, 1, 0, 1, 1, 1])
        vals = [v for _, v in cal["pts"]]
        self.assertEqual(vals, sorted(vals))
        self.assertEqual(learn.calibrated(cal, 0.55), 1.0)
        self.assertEqual(learn.calibrated({}, 0.5), 0.0)
        all_right = learn.isotonic([0.9, 0.95, 1.0], [1, 1, 1])
        self.assertEqual(learn.calibrated(all_right, 0.95), 1.0)
        self.assertLess(learn.calibrated(all_right, 0.3), 0.4)        # far below anything seen: confidence shrinks

    def test_logistic_regression_separates_classes(self):
        import random

        rnd = random.Random(1)
        rows = [[rnd.gauss(2 if i % 2 else -2, 1), rnd.gauss(0, 1)] for i in range(400)]
        labels = [float(i % 2) for i in range(400)]
        m = learn.fit_logistic(rows, labels, epochs=15)
        correct = sum((learn.logit(m, r) > 0) == bool(y) for r, y in zip(rows, labels))
        self.assertGreater(correct / 400, 0.93)
        self.assertAlmostEqual(sum(learn.softmax([1.0, 2.0, 3.0])), 1.0)

    def test_sessions_group_saves_made_close_together(self):
        bms = [Bookmark(url=f"https://a.com/{i}", id=str(i), added=t) for i, t in enumerate([100, 400, 900, 100000, 100100, 0])]
        sid = learn.session_ids(bms)
        self.assertEqual(sid[0], sid[1])
        self.assertEqual(sid[1], sid[2])
        self.assertNotEqual(sid[2], sid[3])
        self.assertEqual(sid[3], sid[4])
        self.assertNotIn(sid[5], sid[:5])                                # undated bookmarks stand alone

    def test_text_normalisation(self):
        from mhbm.textnorm import normalize_text, words

        self.assertEqual(normalize_text("كتاب ٣ يك"), normalize_text("کتاب 3 یک"))
        self.assertEqual(normalize_text("می\u200cخواهم"), "می خواهم")
        self.assertEqual(words("ＡＢＣ and ۱۲۳ تحلیل"), ["abc", "تحلیل"])


class Propose(unittest.TestCase):
    tax = categorize.load_taxonomy()

    def library(self):
        return corpus() + [Bookmark(url="https://recipes1.com/new", title="Quick tomato pasta recipe", id="n1"),
                           Bookmark(url="https://github.com/x/y", title="x", id="n2"),
                           Bookmark(url="https://zzz.org/q", title="Quantum gardening", id="n3"),
                           Bookmark(url="https://k8s1.dev/dest", title="Mine", folder="Mine", id="n4")]

    def test_learned_wins_and_generic_rules_never_sprout_new_folders_in_an_established_tree(self):
        bms = self.library()
        plan = categorize.propose(bms, self.tax, learn.FolderModel(bms))
        self.assertEqual((plan["n1"]["source"], plan["n1"]["folder"]), ("learned", "Food/Cooking"))
        self.assertIn("similar to", plan["n1"]["reason"])
        self.assertNotIn("n2", plan)   # github rule would create Dev/Code Hosting, a folder this library does not have
        self.assertNotIn("n3", plan)
        self.assertNotIn("n4", plan)   # already filed -> untouched

    def test_rules_may_create_folders_when_asked_or_when_there_is_no_tree(self):
        bms = self.library()
        plan = categorize.propose(bms, self.tax, learn.FolderModel(bms), allow_new_folders=True)
        self.assertEqual((plan["n2"]["source"], plan["n2"]["folder"]), ("rules", "Dev/Code Hosting"))
        fresh = [Bookmark(url="https://github.com/x/y", title="x", id="g")]
        self.assertEqual(categorize.propose(fresh, self.tax, learn.FolderModel(fresh))["g"]["folder"], "Dev/Code Hosting")

    def test_taxonomy_reuses_an_existing_folder_only_on_an_exact_path_match(self):
        exact = corpus() + [Bookmark(url="https://git.com/a", title="a", folder="dev/code hosting", id="e1"),
                            Bookmark(url="https://github.com/x/y", title="x", id="n2")]
        self.assertEqual(categorize.propose(exact, self.tax, learn.FolderModel(exact))["n2"]["folder"], "dev/code hosting")
        suffix = corpus() + [Bookmark(url="https://git.com/a", title="a", folder="Stuff/Dev/Code Hosting", id="e1"),
                             Bookmark(url="https://github.com/x/y", title="x", id="n2")]
        self.assertNotIn("n2", categorize.propose(suffix, self.tax, learn.FolderModel(suffix)))   # no trailing-name guesses

    def test_ai_batch_holds_everything_nothing_could_place(self):
        bms = self.library()
        batch = categorize.ai_batch(bms, self.tax, learn.FolderModel(bms))
        self.assertEqual(sorted(i["id"] for i in batch["items"]), ["n2", "n3"])
        self.assertIn("Food/Cooking", batch["existing_folders"])

    def test_apply_plan_validates(self):
        a, b, c = (Bookmark(url=f"https://a.com/{i}", id=i) for i in "abc")
        res = categorize.apply_plan([a, b, c], {
            "a": {"folder": "A/B", "tags": ["Cool Tag"], "title": "Nice", "notes": "n", "added": 5},
            "b": {"delete": True}, "c": {"folder": "a/b/c/d"}, "zz": {"folder": "X"}, "a ": "bad"})
        self.assertEqual((len(res.changed), len(res.deleted), len(res.rejected)), (1, 1, 3))
        self.assertEqual((a.folder, a.tags, a.title, a.notes, a.added), ("A/B", ["cool-tag"], "Nice", "n", 5))
        self.assertEqual(c.folder, "")

    def test_depth_limit_applies_to_new_folders_only(self):
        deep = "Root/Level1/Level2/Level3/Level4/Level5"
        a, b = Bookmark(url="https://a.com", id="a"), Bookmark(url="https://b.com", id="b")
        existing = Bookmark(url="https://c.com", id="c", folder=deep)
        res = categorize.apply_plan([a, b, existing], {"a": {"folder": deep}, "b": {"folder": deep + "/Brand/New/Deep"}})
        self.assertEqual((a.folder, b.folder, len(res.rejected)), (deep, "", 1))
        self.assertIn("new folder deeper", res.rejected[0])

    def test_plans_land_in_the_users_exact_folder_never_a_twin(self):
        # folder names may carry stray spaces ("Work "); stripping them in a plan used to create a duplicate "Work" tree
        known = {"Archive/Work /Marketing", "Archive/Work /Marketing/SEO"}
        a, b, c = (Bookmark(url=f"https://{i}.com", id=i) for i in "abc")
        categorize.apply_plan([a, b, c], {"a": {"folder": "Archive/Work /Marketing"},          # exact, as the model emits
                                          "b": {"folder": "archive/work/marketing/seo"},       # as an LLM would type it
                                          "c": {"folder": "Archive/Work/Brand New"}},          # genuinely new
                              known_folders=known)
        self.assertEqual((a.folder, b.folder), ("Archive/Work /Marketing", "Archive/Work /Marketing/SEO"))
        self.assertEqual(c.folder, "Archive/Work/Brand New")
        self.assertEqual(categorize.resolve_folder("a/b", {"A /B"}), "A /B")
        self.assertEqual(categorize.resolve_folder("A /B", {"A/B", "A /B"}), "A /B")      # verbatim wins when both exist

    def test_traversal_in_folder_names_neutralised(self):
        b = Bookmark(url="https://a.com", id="b")
        categorize.apply_plan([b], {"b": {"folder": "../../etc/x"}})
        self.assertNotIn("..", b.folder)


class Restructure(unittest.TestCase):
    def library(self):
        bms = [Bookmark(url=f"https://py.org/{i}", title=f"t{i}", folder="Dev", tags=["python"], id=f"p{i}") for i in range(30)]
        bms += [Bookmark(url=f"https://js{i}.org/", title=f"j{i}", folder="Dev", tags=["javascript"], id=f"j{i}") for i in range(20)]
        bms += [Bookmark(url="https://deep.com/1", folder="A/B/C/D/E", id="d1"), Bookmark(url="https://deep.com/2", folder="A/B/C/D/E", id="d2"),
                Bookmark(url="https://r1.com", folder="Recipe", id="r1"), Bookmark(url="https://r2.com", folder="Recipes", id="r2"),
                Bookmark(url="https://r3.com", folder="Recipes", id="r3"), Bookmark(url="https://r4.com", folder="Recipes", id="r4"),
                Bookmark(url="https://t1.com", folder="Dev/Tiny", id="t1")]
        return bms

    def test_split_fold_merge_by_default_and_deep_trees_left_alone(self):
        props = restructure.analyse(self.library())
        self.assertEqual({p["kind"] for p in props}, {"merge-similar", "split", "fold"})
        plan = restructure.to_plan(props)
        self.assertEqual(plan["r1"]["folder"], "Recipes")
        self.assertEqual(plan["p0"]["folder"], "Dev/Python")
        self.assertEqual(plan["j0"]["folder"], "Dev/Javascript")
        self.assertEqual(plan["t1"]["folder"], "Dev")          # single-bookmark leaf folds into its parent
        self.assertNotIn("d1", plan)                           # a deliberately deep tree is not "fixed"
        self.assertNotIn("d2", plan)

    def test_flatten_is_opt_in(self):
        plan = restructure.to_plan(restructure.analyse(self.library(), max_depth=3))
        self.assertEqual((plan["d1"]["folder"], plan["d2"]["folder"]), ("A/B/C", "A/B/C"))
        self.assertTrue(all(f["folder"].count("/") < 3 for f in plan.values()))

    def test_folds_do_not_cascade(self):
        bms = [Bookmark(url="https://a.com/1", folder="Top/Mid/Leaf", id="1"), Bookmark(url="https://a.com/2", folder="Top/Mid", id="2"),
               Bookmark(url="https://a.com/3", folder="Top/Other", id="3"), Bookmark(url="https://a.com/4", folder="Top/Other", id="4")]
        plan = restructure.to_plan(restructure.analyse(bms))
        self.assertEqual(plan, {"1": plan["1"]})
        self.assertEqual(plan["1"]["folder"], "Top/Mid")      # Mid (now 2 items) is NOT folded again into Top


class Browsers(unittest.TestCase):
    def test_firefox_places(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d, "places.sqlite")
            c = sqlite3.connect(db)
            c.executescript("""
            CREATE TABLE moz_places(id INTEGER PRIMARY KEY, url TEXT);
            CREATE TABLE moz_bookmarks(id INTEGER PRIMARY KEY, type INT, fk INT, parent INT, title TEXT, dateAdded INT, guid TEXT);
            INSERT INTO moz_places VALUES (1,'https://a.com'),(2,'place:type=6');
            INSERT INTO moz_bookmarks VALUES
              (1,2,NULL,0,'','0','root________'),(2,2,NULL,1,'menu','0','menu________'),(3,2,NULL,1,'toolbar','0','toolbar_____'),
              (4,2,NULL,1,'tags','0','tags________'),(5,2,NULL,1,'unfiled','0','unfiled_____'),
              (6,2,NULL,3,'Work','0','g6'),(7,1,1,6,'A site',1600000000000000,'g7'),
              (8,2,NULL,4,'py','0','g8'),(9,1,1,8,NULL,'0','g9'),(10,1,2,5,'smart','0','g10'),(11,2,NULL,3,'EmptyF','0','g11');
            """)
            c.commit()
            c.close()
            got = browsers.load("x", profile=str(db))
            self.assertEqual([(b.url, b.folder, b.tags, b.added) for b in got],
                             [("https://a.com", "Bookmarks bar/Work", ["py"], 1600000000)])
            self.assertIn("Bookmarks bar/EmptyF", got.folders)

    def test_safari_plist(self):
        data = {"WebBookmarkType": "WebBookmarkTypeList", "Title": "", "Children": [
            {"WebBookmarkType": "WebBookmarkTypeList", "Title": "BookmarksBar", "Children": [
                {"WebBookmarkType": "WebBookmarkTypeList", "Title": "News", "Children": [
                    {"WebBookmarkType": "WebBookmarkTypeLeaf", "URLString": "https://n.com", "URIDictionary": {"title": "N"}}]}]},
            {"WebBookmarkType": "WebBookmarkTypeLeaf", "URLString": "https://top.com", "URIDictionary": {"title": "T"}}]}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "Bookmarks.plist")
            p.write_bytes(plistlib.dumps(data))
            got = browsers.load("safari", profile=str(p))
            self.assertEqual(sorted((b.url, b.folder) for b in got), [("https://n.com", "Bookmarks bar/News"), ("https://top.com", "")])

    def test_chromium_profile_discovery(self):
        import os
        import platform
        if platform.system() != "Linux":
            self.skipTest("path layout checked on Linux")
        with tempfile.TemporaryDirectory() as d:
            prof = Path(d, ".config/BraveSoftware/Brave-Browser/Default")
            prof.mkdir(parents=True)
            (prof / "Bookmarks").write_text(json.dumps({"roots": {"bookmark_bar": {"type": "folder", "name": "Bookmarks bar", "children": [
                {"type": "url", "name": "B", "url": "https://b.com", "date_added": "13200000000000000"}]}}}))
            old = os.environ.get("HOME")
            os.environ["HOME"] = d
            try:
                self.assertIn("brave", browsers.detect())
                self.assertEqual([b.url for b in browsers.load("brave")], ["https://b.com"])
                with self.assertRaises(formats.FormatError):
                    browsers.load("vivaldi")
            finally:
                os.environ["HOME"] = old


if __name__ == "__main__":
    unittest.main()
