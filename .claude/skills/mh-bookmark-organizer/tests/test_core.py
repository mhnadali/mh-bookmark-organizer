import json
import tempfile
import unittest
from pathlib import Path

import common  # noqa: F401
from mhbm import dedupe, formats
from mhbm.model import Bookmark, assign_ids
from mhbm.store import Library, parse_query
from mhbm.urlnorm import normalize_url, strip_tracking, variant_key

NETSCAPE = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<DL><p>
  <DT><H3>Bookmarks bar</H3>
  <DL><p>
    <DT><A HREF="https://github.com/a/b" ADD_DATE="1600000000" TAGS="Dev, Git">Repo B</A>
    <DD>my note
    <DT><H3>Reading</H3>
    <DL><p>
      <DT><A HREF="https://www.example.com/post/?utm_source=x">Post</A>
    </DL><p>
    <DT><H3>Empty</H3>
    <DL><p></DL><p>
  </DL><p>
  <DT><A HREF="http://example.com/post">Post again</A>
</DL><p>
"""


class UrlNorm(unittest.TestCase):
    def test_equivalents(self):
        keys = {normalize_url(u) for u in [
            "http://www.Example.com/post/?utm_source=x&b=2&a=1",
            "https://example.com:443/post?a=1&b=2#frag",
            "https://example.com/post/?a=1&b=2&fbclid=zzz"]}
        self.assertEqual(len(keys), 1)

    def test_distinct_queries_and_ref_param_kept(self):
        self.assertNotEqual(normalize_url("https://x.com/?id=1"), normalize_url("https://x.com/?id=2"))
        self.assertNotEqual(normalize_url("https://g.com/t?ref=main"), normalize_url("https://g.com/t?ref=dev"))

    def test_spa_routes_stay_distinct_plain_anchors_merge(self):
        self.assertNotEqual(normalize_url("https://a.com/#/inbox"), normalize_url("https://a.com/#/settings"))
        self.assertEqual(normalize_url("https://a.com/p#section"), normalize_url("https://a.com/p"))

    def test_meaningful_params_are_never_called_tracking(self):
        # seen on real data: Google Ads uses ?ocid= for the account, GitHub uses ?ref=
        for u in ["https://ads.google.com/aw/overview?ocid=335612072&authuser=0", "https://github.com/a/b/tree?ref=dev",
                  "https://x.com/p?spm=a2&trk=public"]:
            self.assertEqual(strip_tracking(u), u)

    def test_strip_tracking_changes_nothing_else(self):
        self.assertEqual(strip_tracking("https://A.com/p?b=2&utm_source=x&a=1#sec"), "https://A.com/p?b=2&a=1#sec")
        for u in ["https://a.com/p?b=2&a=1", "https://a.com/dir/", "https://a.com/x#frag"]:
            self.assertEqual(strip_tracking(u), u)

    def test_variants(self):
        self.assertEqual(variant_key("https://en.m.wikipedia.org/wiki/X"), variant_key("https://en.wikipedia.org/wiki/X"))
        self.assertEqual(variant_key("https://s.com/a/amp/"), variant_key("https://s.com/a"))
        self.assertEqual(variant_key("https://amp.s.com/a?amp=1"), variant_key("https://s.com/a"))
        self.assertEqual(variant_key("https://other.com/x", "https://s.com/a"), variant_key("https://s.com/a"))


class Formats(unittest.TestCase):
    def test_netscape_parse(self):
        bms = formats.parse_netscape(NETSCAPE)
        self.assertEqual([b.folder for b in bms], ["Bookmarks bar", "Bookmarks bar/Reading", ""])
        self.assertEqual((bms[0].tags, bms[0].notes, bms[0].added), (["dev", "git"], "my note", 1600000000))
        self.assertIn("Bookmarks bar/Empty", bms.folders)

    def test_empty_folders_and_toolbar_roundtrip(self):
        bms = formats.parse_netscape(NETSCAPE)
        out = formats.write_netscape(bms, bms.folders)
        self.assertIn('PERSONAL_TOOLBAR_FOLDER="true"', out)
        self.assertIn("Bookmarks bar/Empty", formats.parse_netscape(out).folders)
        chrome = formats.parse_chrome(json.loads(formats.write_chrome(bms, bms.folders)))
        self.assertIn("Bookmarks bar/Empty", chrome.folders)

    def test_folder_names_with_slash_survive(self):
        html = '<DL><DT><H3>Work/Home</H3><DL><DT><A HREF="https://a.com">a</A></DL></DL>'
        bms = formats.parse_netscape(html)
        self.assertEqual(len(bms[0].folder.split("/")), 1)
        self.assertIn("<H3>Work/Home</H3>", formats.write_netscape(bms, bms.folders))
        again = formats.parse_netscape(formats.write_netscape(bms, bms.folders))
        self.assertEqual(again[0].folder, bms[0].folder)
        chrome = json.loads(formats.write_chrome(bms, bms.folders))
        self.assertEqual(chrome["roots"]["other"]["children"][0]["name"], "Work/Home")

    def test_folder_names_are_preserved_exactly(self):
        html = '<DL><DT><H3>Work </H3><DL><DT><H3> Ad Exchange </H3><DL><DT><A HREF="https://a.com">a</A></DL></DL></DL>'
        bms = formats.parse_netscape(html)
        self.assertEqual(bms[0].folder, "Work / Ad Exchange ")
        again = formats.parse_netscape(formats.write_netscape(bms, bms.folders))
        self.assertEqual((again[0].folder, again.folders), (bms[0].folder, bms.folders))
        csv_in = formats.parse_csv("url,folder\nhttps://a.com, Work / Home \n")
        self.assertEqual(csv_in[0].folder, "Work/Home")  # typed paths are tidied; structured exports are not

    def test_root_level_bookmarks_in_a_chrome_html_export_stay_unfiled(self):
        html = ('<DL><DT><H3 PERSONAL_TOOLBAR_FOLDER="true">Bookmarks bar</H3><DL><DT><A HREF="https://a.com">a</A></DL>'
                '<DT><A HREF="https://other.com">o</A></DL>')
        self.assertEqual([b.folder for b in formats.parse_netscape(html)], ["Bookmarks bar", ""])

    def test_roundtrips(self):
        src = formats.parse_netscape(NETSCAPE)
        for fmt, reader in [("html", formats.parse_netscape), ("csv", formats.parse_csv),
                            ("json", lambda t: formats.parse_json(json.loads(t))),
                            ("chrome", lambda t: formats.parse_chrome(json.loads(t)))]:
            back = reader(formats.render(src, fmt, src.folders))
            self.assertEqual(sorted(b.url for b in back), sorted(b.url for b in src), fmt)

    def test_html_escaping(self):
        out = formats.write_netscape([Bookmark(url="https://a.com/?x=1&y=<2>", title='A "q" <b>')])
        self.assertNotIn("<b>", out)
        self.assertEqual(formats.parse_netscape(out)[0].title, 'A "q" <b>')

    def test_dates(self):
        self.assertEqual(formats.to_unix("13200000000000000"), 13200000000 - formats.WEBKIT_EPOCH_OFFSET)
        self.assertEqual(formats.to_unix(1600000000000), 1600000000)
        self.assertEqual(formats.to_unix("2020-09-13T12:26:40Z"), 1600000000)

    def test_csv_formula_injection_neutralised_and_reversible(self):
        evil = Bookmark(url="https://a.com", title='=HYPERLINK("http://evil","x")', notes="+cmd|' /C calc'!A0")
        out = formats.write_csv([evil])
        for line in out.splitlines()[1:]:
            self.assertNotRegex(line, r'(^|,)"?[=+@-]')
        back = formats.parse_csv(out)[0]
        self.assertEqual((back.title, back.notes), (evil.title, evil.notes))

    def test_markdown_escapes(self):
        md = formats.write_markdown([Bookmark(url="https://a.com/(x) y", title="a](http://evil) [b")])
        self.assertIn("a\\](http://evil)", md)  # the closing bracket is backslash-escaped, so no second link forms
        self.assertEqual(md.count("](http"), 2)
        self.assertIn("%28x%29%20y", md)

    def test_csv_aliases_and_services(self):
        bms = formats.parse_csv("Link,Name,Collection,Labels\nhttps://a.com,A,Work,x;y\n,skipme,,\n")
        self.assertEqual((bms[0].title, bms[0].folder, bms[0].tags, len(bms)), ("A", "Work", ["x", "y"], 1))
        raindrop = "id,title,note,excerpt,url,folder,tags,created,cover,highlights,favorite\n1,T,mynote,,https://r.com,Read,\"a, b\",2020-09-13T12:26:40.000Z,,,false\n"
        r = formats.parse_csv(raindrop)[0]
        self.assertEqual((r.folder, r.tags, r.notes, r.added), ("Read", ["a", "b"], "mynote", 1600000000))
        insta = "URL,Title,Selection,Folder,Timestamp\nhttps://i.com,T,sel,Unread,1600000000\n"
        self.assertEqual(formats.parse_csv(insta)[0].added, 1600000000)

    def test_pocket_html(self):
        html = ('<h1>Unread</h1><ul><li><a href="https://p.com/a" time_added="1600000000" tags="x,y">Pocket A</a></li></ul>'
                '<h1>Read Archive</h1><ul><li><a href="https://p.com/b" time_added="1600000001" tags="">Pocket B</a></li></ul>')
        a, b = formats.parse_netscape(html)
        self.assertEqual((a.folder, a.tags, a.added, b.folder), ("Pocket/Unread", ["x", "y"], 1600000000, "Pocket/Read Archive"))

    def test_karakeep_linkwarden_omnivore_json(self):
        kara = {"bookmarks": [{"createdAt": "2024-01-02T00:00:00Z", "title": "K", "tags": ["a", "b"],
                               "content": {"type": "link", "url": "https://k.com"}, "note": "n"}]}
        k = formats.parse_json(kara)[0]
        self.assertEqual((k.url, k.tags, k.notes), ("https://k.com", ["a", "b"], "n"))
        lw = {"collections": [{"name": "Dev", "links": [{"url": "https://l.com", "name": "L", "tags": [{"name": "t1"}], "description": "d"}]}]}
        lk = formats.parse_json(lw)[0]
        self.assertEqual((lk.folder, lk.tags, lk.title), ("Dev", ["t1"], "L"))
        omni = [{"url": "https://o.com", "title": "O", "labels": [{"name": "x"}], "savedAt": "2024-01-02T00:00:00Z"}]
        self.assertEqual(formats.parse_json(omni)[0].tags, ["x"])

    def test_firefox_json(self):
        data = {"children": [{"root": "toolbarFolder", "title": "toolbar", "children": [
            {"typeCode": 1, "uri": "https://a.com", "title": "A", "tags": "t1", "dateAdded": 1600000000000000}]}]}
        b = formats.parse_firefox(data)[0]
        self.assertEqual((b.folder, b.tags, b.added), ("Bookmarks bar", ["t1"], 1600000000))

    def test_obsidian_paths_stay_short_for_deep_trees(self):
        deep = "/".join(f"A long folder name number {i} for testing" for i in range(10))
        with tempfile.TemporaryDirectory() as d:
            formats.write_obsidian([Bookmark(url="https://a.com", title="T" * 200, folder=deep, id="x")], Path(d, "v"))
            longest = max(len(str(p.relative_to(Path(d, "v")))) for p in Path(d, "v").rglob("*.md"))
            self.assertLess(longest, 200)

    def test_obsidian_export_is_contained(self):
        with tempfile.TemporaryDirectory() as d:
            bms = [Bookmark(url="https://a.com", title="../../escape", folder="../../up/Dev", tags=["x"], notes="hello"),
                   Bookmark(url="https://b.com", title="CON: *weird?", folder="A/B")]
            n = formats.write_obsidian(bms, Path(d, "vault"))
            self.assertEqual(n, 2)
            files = list(Path(d).rglob("*.md"))
            self.assertTrue(all(Path(d, "vault").resolve() in f.resolve().parents for f in files))
            note = next(f for f in files if "escape" in f.name).read_text()
            self.assertIn('url: "https://a.com"', note)
            self.assertTrue(Path(d, "vault", "_index.md").exists())


class Dedupe(unittest.TestCase):
    def test_exact_merge_keeps_best_and_unions_tags(self):
        a = Bookmark(url="http://www.example.com/p/?utm_medium=x", title="", tags=["a"], added=200, id="a")
        b = Bookmark(url="https://example.com/p", title="Good title", tags=["b"], notes="n", added=100, id="b")
        c = Bookmark(url="https://other.com", title="Other", id="c")
        kept, removed, groups = dedupe.dedupe([a, b, c])
        self.assertEqual((len(groups), removed), (1, [a]))
        keeper = next(x for x in kept if x.title == "Good title")
        self.assertEqual((keeper.tags, keeper.added), (["b", "a"], 100))

    def test_variant_level(self):
        a = Bookmark(url="https://en.m.wikipedia.org/wiki/X", id="a")
        b = Bookmark(url="https://en.wikipedia.org/wiki/X", id="b")
        c = Bookmark(url="https://other.com/x", id="c", meta={"canonical": "https://en.wikipedia.org/wiki/X"})
        self.assertEqual(len(dedupe.find_groups([a, b, c], "exact")), 0)
        self.assertEqual(len(dedupe.find_groups([a, b, c], "variant")[0]), 3)

    def test_dead_copy_loses(self):
        alive = Bookmark(url="https://a.com/x", id="1", status="ok")
        dead = Bookmark(url="http://a.com/x", id="2", title="Nicer title", notes="n", status="dead")
        _, removed, _ = dedupe.dedupe([dead, alive])
        self.assertEqual(removed, [dead])

    def test_fuzzy_finds_referral_variants_and_ignores_distinct_entities(self):
        mk = lambda i, url, title="Some Page Title That Is Long Enough": Bookmark(url=url, title=title, id=i)  # noqa: E731
        bms = [mk("a", "https://site.com/"), mk("b", "https://site.com/?ref=xranks"),                       # referral variant: group
               mk("g1", "https://www.google.com/search?q=cats", "cats - Google Search"),                       # state in the query: distinct
               mk("g2", "https://www.google.com/search?q=dogs", "dogs - Google Search"),
               mk("t1", "https://tagmanager.google.com/#/container/accounts/111111/workspaces/3", "Google Tag Manager"),
               mk("t2", "https://tagmanager.google.com/#/container/accounts/222222/workspaces/1", "Google Tag Manager"),
               mk("p1", "https://shop.com/product/335583", "Wireless Mouse Model BXS 2024 Edition"),         # different ids: distinct
               mk("p2", "https://shop.com/product/335687", "Wireless Mouse Model BXS 2024 Edition"),
               mk("s1", "https://blog.com/2020/intro-to-kubernetes-operators", "Intro to Kubernetes Operators | Blog"),
               mk("s2", "https://blog.com/p/intro-to-kubernetes-operators-2", "Intro to Kubernetes Operators - Blog")]
        groups = dedupe.fuzzy_groups(bms)
        ids = sorted(sorted(x.id for x in g) for _, g in groups)
        self.assertEqual(ids, [["a", "b"], ["s1", "s2"]])
        self.assertEqual(dedupe.find_groups(bms), [])  # exact matching leaves all of them alone

    def test_fuzzy_content_simhash(self):
        text = ("operators extend kubernetes with custom controllers that reconcile state " * 20)
        d = Bookmark(url="https://x.com/1", id="d", content=text + " alpha")
        e = Bookmark(url="https://y.com/2", id="e", content=text + " beta")
        reason, g = dedupe.fuzzy_groups([d, e])[0]
        self.assertEqual((reason, sorted(x.id for x in g)), ("near-identical page text", ["d", "e"]))

    def test_cross_folder_merge_leaves_a_trace_and_same_folder_mode(self):
        a = Bookmark(url="https://a.com/x", title="T", folder="Work/Tools", id="a", added=1)
        b = Bookmark(url="https://a.com/x", title="T", folder="Home", id="b", added=2)
        c = Bookmark(url="https://a.com/x", title="T", folder="Work/Tools", id="c", added=3)
        kept, removed, _ = dedupe.dedupe([a, b, c])
        keeper = kept[0]
        self.assertEqual(len(removed), 2)
        self.assertIn("also filed in: Home", keeper.notes)
        self.assertNotIn("Work/Tools", keeper.notes)
        a2, b2, c2 = (Bookmark(url="https://a.com/x", folder=f, id=i) for f, i in [("Work/Tools", "a"), ("Home", "b"), ("Work/Tools", "c")])
        _, removed, _ = dedupe.dedupe([a2, b2, c2], same_folder_only=True)
        self.assertEqual(sorted(x.id for x in removed), ["c"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = Library(self.tmp.name)
        self.addCleanup(self.lib.close)
        self.addCleanup(self.tmp.cleanup)

    def test_identical_rows_get_distinct_stable_ids(self):
        a, b = Bookmark(url="https://a.com", title="x"), Bookmark(url="https://a.com", title="x")
        assign_ids([a, b], set())
        self.assertNotEqual(a.id, b.id)
        a2, b2 = Bookmark(url="https://a.com"), Bookmark(url="https://a.com")
        assign_ids([a2, b2], set())
        self.assertEqual((a.id, b.id), (a2.id, b2.id))
        self.lib.upsert([a, b])
        self.assertEqual(self.lib.count(), 2)

    def test_search_filters_and_operators(self):
        a = Bookmark(url="https://a.com/x", title="Kubernetes operators", tags=["dev"], folder="Dev", added=1700000000, id="a")
        b = Bookmark(url="https://sub.b.com/y", title="Cooking pasta", added=1600000000, id="b", status="dead")
        self.lib.upsert([a, b])
        a.content = "deep dive on controllers"
        self.lib.save_pages([a])
        q = lambda s: [h[0].id for h in self.lib.search(s)]  # noqa: E731
        self.assertEqual(q("kubernetes tag:dev"), ["a"])
        self.assertEqual(q("is:unfiled"), ["b"])
        self.assertEqual(q("controller"), ["a"])  # stemming over page text
        self.assertEqual(q('"deep dive"'), ["a"])
        self.assertEqual(q("-kubernetes pasta"), ["b"])
        self.assertEqual(q("site:b.com"), ["b"])
        self.assertEqual(q("status:dead"), ["b"])
        self.assertEqual(q("before:2023-01-01"), ["b"])
        self.assertEqual(q("after:2023-01-01"), ["a"])
        self.assertEqual(q("folder:dev"), ["a"])
        self.assertEqual(q("nothingmatches"), [])
        self.assertEqual(parse_query('x "a b" -n tag:d')["phrases"], ["a b"])

    def test_incremental_index_and_delete(self):
        bms = [Bookmark(url=f"https://s.com/{i}", title=f"alpha {i}", id=str(i)) for i in range(5)]
        self.lib.upsert(bms)
        bms[0].title = "zebra"
        self.lib.upsert([bms[0]])
        self.assertEqual([h[0].id for h in self.lib.search("zebra")], ["0"])
        self.assertEqual(len(self.lib.search("alpha")), 4)
        self.lib.delete(["1"])
        self.assertEqual(len(self.lib.search("alpha")), 3)

    def test_undo_and_history(self):
        a = Bookmark(url="https://a.com", title="one", id="a", folder="X")
        with self.lib.op("import"):
            self.lib.upsert([a])
        a.folder = "Y"
        with self.lib.op("move"):
            self.lib.upsert([a])
        with self.lib.op("remove"):
            self.lib.delete(["a"])
        self.assertEqual(self.lib.count(), 0)
        self.assertEqual(self.lib.undo()["command"], "remove")
        self.assertEqual(self.lib.get("a").folder, "Y")
        self.lib.undo()
        self.assertEqual(self.lib.get("a").folder, "X")
        self.lib.undo()
        self.assertEqual(self.lib.count(), 0)
        with self.assertRaises(ValueError):
            self.lib.undo()
        self.assertEqual([h["command"] for h in self.lib.history()], ["remove", "move", "import"])

    def test_undo_restores_the_folder_set_including_empty_folders(self):
        self.lib.add_folders(["Keep/Empty", "Keep"])
        a = Bookmark(url="https://a.com", id="a", folder="Keep/Full")
        self.lib.upsert([a])
        before = (self.lib.folders(), self.lib.get("a").folder)
        with self.lib.op("restructure"):
            a.folder = "Elsewhere"
            self.lib.upsert([a])
            self.lib.set_folders(self.lib.used_folders())      # drops the empty folders
        self.assertEqual(self.lib.folders(), {"Elsewhere"})
        with self.lib.op("folders-only"):                      # an op that changes nothing but the folder set is journaled too
            self.lib.add_folders(["Extra"])
        self.lib.undo()
        self.assertEqual(self.lib.folders(), {"Elsewhere"})
        self.lib.undo()
        self.assertEqual((self.lib.folders(), self.lib.get("a").folder), before)

    def test_snapshot_restore_keeps_pages_and_folders(self):
        a = Bookmark(url="https://a.com", title="one", id="a", content="searchable text")
        self.lib.upsert([a])
        self.lib.save_pages([a])
        self.lib.add_folders(["Empty"])
        snap = self.lib.snapshot("t")
        self.lib.delete(["a"])
        self.lib.set_folders([])
        self.lib.restore(snap)
        self.assertEqual(self.lib.count(), 1)
        self.assertEqual(self.lib.folders(), {"Empty"})
        self.assertEqual([h[0].id for h in self.lib.search("searchable")], ["a"])

    def test_migrates_v1_library(self):
        import sqlite3
        d = tempfile.mkdtemp()
        c = sqlite3.connect(Path(d, "library.db"))
        c.executescript("""CREATE TABLE bookmarks(id TEXT PRIMARY KEY, url TEXT NOT NULL, norm TEXT, title TEXT, folder TEXT,
          tags TEXT, notes TEXT, added INTEGER, status TEXT, http_code INTEGER, last_checked INTEGER, archive_path TEXT, content TEXT);
          INSERT INTO bookmarks VALUES('a','https://a.com','','Old title','Dev','["x"]','',5,'ok',200,9,'','legacy page text');""")
        c.commit()
        c.close()
        lib = Library(d)
        self.addCleanup(lib.close)
        b = lib.get("a")
        self.assertEqual((b.title, b.tags, b.folder, b.fail_count, b.content), ("Old title", ["x"], "Dev", 0, "legacy page text"))
        self.assertEqual([h[0].id for h in lib.search("legacy")], ["a"])

    def test_search_folder_matches_any_name_along_the_path(self):
        a = Bookmark(url="https://a.com/x", title="premiere tip", folder="Work /Video Editing/Premiere", id="a")
        b = Bookmark(url="https://b.com/y", title="other", folder="Work /Video Editing", id="b")
        self.lib.upsert([a, b])
        q = lambda s: sorted(h[0].id for h in self.lib.search(s))  # noqa: E731
        self.assertEqual(q("folder:premiere"), ["a"])
        self.assertEqual(q('folder:"video editing"'), ["a", "b"])
        self.assertEqual(q("folder:video"), [])  # whole names only, not substrings

    def test_iter_all_batches(self):
        self.lib.upsert([Bookmark(url=f"https://s.com/{i}", id=str(i)) for i in range(25)])
        self.assertEqual(len(list(self.lib.iter_all(batch=7))), 25)


if __name__ == "__main__":
    unittest.main()
