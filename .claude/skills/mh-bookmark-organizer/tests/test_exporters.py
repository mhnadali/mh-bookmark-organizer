import json
import unittest

import common  # noqa: F401
from mhbm import formats
from mhbm.model import Bookmark
from test_cli import CliCase


def sample():
    return [Bookmark(url="https://a.com/1", title="A \"q\" <b>", folder="Dev/Docs", tags=["x", "y"], notes="n", added=1600000000,
                     state="unread", favorite=True, id="1"),
            Bookmark(url="https://b.com/2", title="=cmd()", folder="", added=1600000100, state="read", id="2"),
            Bookmark(url="https://c.com/3", title="C", folder="Dev", tags=["z"], id="3")]


FOLDERS = {"Dev", "Dev/Docs"}


def rt(fmt, reader):
    return {b.url: b for b in reader(formats.render(sample(), fmt, FOLDERS))}


class Exporters(unittest.TestCase):
    def test_raindrop_columns_and_roundtrip(self):
        out = formats.render(sample(), "raindrop", FOLDERS)
        self.assertEqual(out.splitlines()[0], "id,title,note,excerpt,url,folder,tags,created,cover,highlights,favorite")
        back = rt("raindrop", formats.parse_csv)
        a = back["https://a.com/1"]
        self.assertEqual((a.title, a.folder, a.tags, a.favorite, a.added, a.notes), ('A "q" <b>', "Dev/Docs", ["x", "y"], True, 1600000000, "n"))
        self.assertNotIn(",=cmd", out)                                     # formula injection neutralised

    def test_pocket_layout_and_states(self):
        out = formats.render(sample(), "pocket", FOLDERS)
        self.assertIn("<h1>Unread</h1>", out)
        self.assertIn("<h1>Read Archive</h1>", out)
        back = rt("pocket", formats.parse_netscape)
        self.assertEqual((back["https://a.com/1"].state, back["https://b.com/2"].state), ("unread", "read"))
        self.assertEqual(back["https://a.com/1"].tags, ["x", "y", "docs"])   # no folders in Pocket: the folder becomes a tag
        self.assertNotIn("<b>", out.split("<h1>Unread</h1>")[1].split("</a>")[0])

    def test_linkwarden_nested_collections_roundtrip(self):
        data = json.loads(formats.render(sample(), "linkwarden", FOLDERS))
        by_name = {c["name"]: c for c in data["collections"]}
        self.assertEqual(by_name["Docs"]["parentId"], by_name["Dev"]["id"])
        self.assertIsNone(by_name["Dev"]["parentId"])
        self.assertIn("Unorganized", by_name)
        back = {b.url: b for b in formats.parse_json(data)}
        self.assertEqual((back["https://a.com/1"].folder, back["https://c.com/3"].folder, back["https://b.com/2"].folder), ("Dev/Docs", "Dev", ""))
        self.assertEqual(back["https://a.com/1"].tags, ["x", "y"])

    def test_karakeep_shape(self):
        data = json.loads(formats.render(sample(), "karakeep", FOLDERS))
        row = data["bookmarks"][0]
        self.assertEqual((row["content"], row["favourited"], row["createdAt"]), ({"type": "link", "url": "https://a.com/1"}, True, 1600000000))
        back = rt("karakeep", lambda t: formats.parse_json(json.loads(t)))
        self.assertEqual((back["https://a.com/1"].favorite, back["https://b.com/2"].state), (True, "read"))

    def test_instapaper_built_in_folders(self):
        out = formats.render(sample(), "instapaper", FOLDERS)
        self.assertEqual(out.splitlines()[0], "URL,Title,Selection,Folder,Timestamp")
        back = rt("instapaper", formats.parse_csv)
        a, b, c = back["https://a.com/1"], back["https://b.com/2"], back["https://c.com/3"]
        self.assertEqual((a.favorite, a.folder), (True, ""))               # Starred
        self.assertEqual((b.state, b.folder), ("read", ""))                 # Archive
        self.assertEqual(c.folder, "Dev")                                   # a custom folder survives as its leaf name


class Cli(CliCase):
    def test_export_formats_from_the_library(self):
        self.run_cli("import", self.write("in.csv", "url,title,folder,tags\nhttps://a.com,A,Dev,t1\n"))
        for fmt in ("raindrop", "pocket", "linkwarden", "karakeep", "instapaper"):
            out = self.dir / f"o.{fmt}"
            self.assertEqual(self.js("export", "--format", fmt, "--out", str(out))["written"], 1)
            self.assertIn("a.com", out.read_text())


if __name__ == "__main__":
    unittest.main()
