import unittest
from collections import Counter

import common
from mhbm import autotag, canonical, categorize, model, synonyms, titles
from mhbm.model import Bookmark, clean_tags
from test_cli import CliCase


class Synonyms(unittest.TestCase):
    def tearDown(self):
        model.set_tag_synonyms({})
        model.set_folder_synonyms({})

    def test_aliases_collapse_into_the_canonical_tag(self):
        data = {"tags": {}, "folders": {}}
        synonyms.add(data, "tags", "Machine Learning", ["ML", "machinelearning"])
        self.assertEqual(data["tags"], {"machine-learning": ["machinelearning", "ml"]})
        model.set_tag_synonyms(synonyms.reverse(data["tags"], synonyms.tag_norm))
        self.assertEqual(clean_tags(["ML", "machine-learning", "AI"]), ["machine-learning", "ai"])

    def test_an_alias_moves_between_canonicals_and_removal_works(self):
        data = {"tags": {}, "folders": {}}
        synonyms.add(data, "tags", "a", ["x"])
        synonyms.add(data, "tags", "b", ["x"])
        self.assertEqual(data["tags"], {"a": [], "b": ["x"]})
        self.assertTrue(synonyms.remove(data, "tags", "x"))
        self.assertFalse(synonyms.remove(data, "tags", "nothing"))

    def test_folder_synonym_used_when_resolving(self):
        model.set_folder_synonyms({"work/docs": "Work/Documentation"})
        self.assertEqual(categorize.resolve_folder("Work/Docs", {"Work/Documentation"}), "Work/Documentation")

    def test_merge_suggestions(self):
        found = synonyms.suggest_tag_merges(Counter({"recipes": 9, "recipe": 4, "machine-learning": 7, "ml": 3, "web_dev": 5,
                                                     "web-dev": 2, "kubernetes": 8, "kubernetse": 1, "python": 20, "rust": 5}))
        pairs = {(s["keep"], s["merge"]) for s in found}
        self.assertEqual(pairs, {("recipes", "recipe"), ("machine-learning", "ml"), ("web_dev", "web-dev"), ("kubernetes", "kubernetse")})


def library():
    bms = []
    for i in range(10):
        bms.append(Bookmark(url=f"https://site{i}.com/p/{i}", title=f"Kubernetes operator tutorial part {i}", folder="Dev/K8s", id=f"k{i}"))
        bms.append(Bookmark(url=f"https://other{i}.org/{i}", title=f"Sourdough bread baking recipe {i}", folder="Food", id=f"f{i}"))
        bms.append(Bookmark(url=f"https://trip{i}.net/{i}", title=f"Lisbon hostel budget itinerary {i}", folder="Travel", id=f"t{i}"))
        bms.append(Bookmark(url=f"https://gym{i}.net/{i}", title=f"Kettlebell workout routine {i}", folder="Fitness", id=f"g{i}"))
    bms.append(Bookmark(url="https://new.example/k", title="Kubernetes operator upgrade notes", id="new-k"))
    bms.append(Bookmark(url="https://new.example/q", title="Zxqv unique snowflake", id="new-q"))
    return bms


class AutoTag(unittest.TestCase):
    def test_topic_tags_not_one_off_words(self):
        plan = autotag.propose(library())
        self.assertIn("kubernetes", plan["new-k"]["tags"])
        self.assertNotIn("new-q", plan)                                  # a word seen once is not a tag
        for p in plan.values():
            self.assertLessEqual(len(p["tags"]), 3)
            self.assertNotIn("tutorial", p["tags"] + [])                 # filler words are excluded
        self.assertTrue(all(t not in ("part", "notes") for p in plan.values() for t in p["tags"]))

    def test_existing_vocabulary_is_preferred_and_tagged_bookmarks_are_left_alone(self):
        bms = library()
        for b in bms[:6]:
            b.tags = ["k8s"]
        plan = autotag.propose(bms)
        self.assertTrue(all(i not in plan for i in [b.id for b in bms[:6]]))
        self.assertTrue(plan)
        everything = autotag.propose(bms, only_untagged=False)
        self.assertGreater(len(everything), len(plan))

    def test_a_page_with_known_words_that_were_never_adjacent_does_not_crash(self):
        """Regression: log(0) on a never-seen word pair raised ValueError and broke the extension's suggest call."""
        bms = [Bookmark(url=f"https://k{i}.example/p{i}", title=f"alpha lane {i}", id=f"a{i}") for i in range(5)]
        bms += [Bookmark(url=f"https://m{i}.example/p{i}", title=f"beta road {i}", id=f"b{i}") for i in range(5)]
        vocab = autotag.Vocab(bms)
        self.assertEqual(vocab.pmi("alpha-road"), 0.0)                     # both words known, never side by side
        probe = Bookmark(url="https://new.example/x", title="alpha road trip", id="q")
        self.assertIsInstance(autotag.suggest(probe, vocab), list)


class Titles(unittest.TestCase):
    def test_counters_brand_suffixes_and_empty_titles(self):
        bms = [Bookmark(url=f"https://news.example.com/{i}", title=f"Story number {i} | Example News", id=f"n{i}") for i in range(4)]
        bms += [Bookmark(url="https://social.test/feed", title="(6) Feed | Social", id="c1"),
                Bookmark(url="https://shop.test/", title="Great Deals - shop", id="c2"),
                Bookmark(url="https://x.example.org/some/page-name_here.html", title="", id="e1"),
                Bookmark(url="https://same.example/", title="https://same.example/", id="e2"),
                Bookmark(url="https://err.example/", title="522: Connection timed out", id="e3"),
                Bookmark(url="https://err2.example/", title="404 Not Found", id="e4", meta={"page_title": "The real article"}),
                Bookmark(url="https://fine.example/", title="Already fine", id="ok")]
        plan = titles.propose(bms)
        self.assertEqual(plan["n0"]["title"], "Story number 0")
        self.assertEqual(plan["c1"]["title"], "Feed")                          # counter gone, and "Social" is the site's own name
        self.assertEqual(plan["c2"]["title"], "Great Deals")                   # brand equals the site's own name
        self.assertEqual(plan["e1"]["title"], "x.example.org › page name here")
        self.assertEqual(plan["e2"]["title"], "same.example")
        self.assertNotIn("e3", plan)                                           # nothing better known: archive first
        self.assertEqual(plan["e4"]["title"], "The real article")
        self.assertNotIn("ok", plan)
        keep = titles.propose(bms, keep_counters=True)
        self.assertEqual(keep["c1"]["title"], "(6) Feed")                      # the counter stays when asked
        self.assertNotIn("n0", titles.propose(bms, strip_brand=False))

    def test_never_empties_a_title_or_strips_informative_parts(self):
        bms = [Bookmark(url=f"https://a.example.com/{i}", title="Example | Home", id=f"h{i}") for i in range(4)]
        for b in bms:
            self.assertNotEqual(titles.clean_title(b.title, "a.example.com", {"example", "home"}), "")
        self.assertEqual(titles.clean_title("Rust - Wikipedia", "en.wikipedia.org", set()), "Rust")


class Canonical(unittest.TestCase):
    def test_only_cosmetic_changes_are_applied(self):
        j = canonical.judge
        self.assertEqual(j("http://www.example.com/post/", "https://example.com/post")[0], "update")
        self.assertEqual(j("https://m.example.com/a?utm_source=x", "https://example.com/a")[0], "update")
        self.assertEqual(j("https://blog.example.com/p/123", "https://blog.example.com/p/123-readable-slug")[0], "update")
        self.assertEqual(j("https://example.com/a", "https://example.com/a")[0], "same")
        self.assertEqual(j("https://example.com/search?q=cats", "https://example.com/")[0], "review")     # home page canonical
        self.assertEqual(j("https://example.com/a?id=7", "https://example.com/a")[0], "review")          # would drop ?id
        self.assertEqual(j("https://example.com/a", "https://other.com/a")[0], "review")
        self.assertEqual(j("https://example.com/a", "javascript:x")[0], "review")
        self.assertEqual(j("https://example.com/a", "https://example.com/totally/different")[0], "review")

    def test_plan_url_is_validated(self):
        a, b = Bookmark(url="https://a.com/x", id="a"), Bookmark(url="https://b.com/x", id="b")
        res = categorize.apply_plan([a, b], {"a": {"url": "https://a.com/x-slug"}, "b": {"url": "javascript:alert(1)"}})
        self.assertEqual((a.url, b.url, len(res.rejected)), ("https://a.com/x-slug", "https://b.com/x", 1))
        self.assertEqual(a.norm, "https://a.com/x-slug")


class Cli(CliCase):
    def test_tags_rename_normalize_and_undo(self):
        self.run_cli("import", self.write("in.csv", "url,title,tags\nhttps://a.com,A,ml\nhttps://b.com,B,machine-learning\nhttps://c.com,C,ML;ai\n"))
        self.run_cli("synonyms", "add", "--tag", "machine-learning", "ml")
        self.assertEqual(self.js("tags", "--normalize")["to_normalise"], 2)       # stored 'ml' differs from the map
        self.run_cli("tags", "--normalize", "--apply")
        self.assertEqual(self.js("tags")["tags"], {"machine-learning": 3, "ai": 1})
        self.run_cli("tags", "--rename", "ai", "artificial-intelligence", "--apply")
        self.assertIn("artificial-intelligence", self.js("tags")["tags"])
        self.run_cli("undo")
        self.assertIn("ai", self.js("tags")["tags"])
        found = self.js("tags", "--suggest-merge")["suggestions"]
        self.assertEqual(found, [])                                              # already merged

    def test_suggest_merge_apply_saves_synonyms(self):
        self.run_cli("import", self.write("in.csv", "url,title,tags\nhttps://a.com,A,recipes\nhttps://b.com,B,recipes\nhttps://c.com,C,recipe\n"))
        self.run_cli("tags", "--suggest-merge", "--apply")
        self.assertEqual(self.js("synonyms", "list")["tags"], {"recipes": ["recipe"]})
        self.run_cli("tags", "--normalize", "--apply")
        self.assertEqual(self.js("tags")["tags"], {"recipes": 3})

    def test_autotag_titles_flow_with_plans(self):
        rows = ["url,title,folder"] + [f"https://s{i}.com/{i},Kubernetes operator tutorial {i},Dev" for i in range(10)]
        rows += [f"https://t{i}.com/{i},Lisbon hostel budget {i},Travel" for i in range(10)]
        rows += [f"https://g{i}.com/{i},Kettlebell workout routine {i},Fitness" for i in range(10)]
        rows += ["https://n.com/k,Kubernetes operator upgrade,", "https://e.com/,,"]
        self.run_cli("import", self.write("in.csv", "\n".join(rows)))
        plan = self.dir / "t.json"
        self.run_cli("autotag", "--plan-out", str(plan))
        self.assertTrue(plan.exists())
        self.run_cli("autotag", "--apply")
        self.assertIn("kubernetes", self.js("tags")["tags"])
        self.run_cli("titles", "--apply")
        self.assertTrue(all(b.title for b in self.lib().all()))                  # the empty title got a readable one
        self.run_cli("undo")
        self.assertTrue(any(not b.title for b in self.lib().all()))

    def test_canonical_command(self):
        self.run_cli("import", self.write("in.csv", "url,title\nhttp://www.a.com/post/,A\nhttps://b.com/s?q=x,B\n"))
        lib = self.lib()
        a, b = lib.all()
        a.meta, b.meta = {"canonical": "https://a.com/post"}, {"canonical": "https://b.com/"}
        lib.upsert([a, b])
        r = self.js("canonical")
        self.assertEqual((r["count"], len(r["needs_review"])), (1, 1))
        self.run_cli("canonical", "--apply")
        self.assertEqual(sorted(x.url for x in self.lib().all()), ["http://www.a.com/post/".replace("http://www.", "https://") and "https://a.com/post", "https://b.com/s?q=x"])

    def test_archive_records_page_title_and_canonical(self):
        from mhbm import archive
        with common.Server() as s:
            import tempfile
            with tempfile.TemporaryDirectory() as d:
                b = Bookmark(url=s.base + "/ok", id="x", title="404 Not Found")
                list(archive.archive_all([b], d, interval=0))
                self.assertEqual(b.meta["page_title"], "Operators Guide")
                self.assertEqual(b.meta["canonical"], "https://example.com/guide")
                self.assertEqual(titles.propose([b])["x"]["title"], "Operators Guide")


if __name__ == "__main__":
    unittest.main()
