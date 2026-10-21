"""End-to-end checks on the synthetic messy library (tests/fixtures/messy-library.html).

The fixture has the *shape* of a real neglected library (deep granular tree, trailing-space and slash folder names, empty
folders, folder dates, Persian titles, sittings, tracking parameters, empty titles, icons) but contains no real data.
"""
import os
import sys
import unittest
from pathlib import Path

import common  # noqa: F401
from mhbm import categorize, formats, learn
from mhbm.model import is_unfiled
from test_cli import CliCase

FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(FIXTURES))
import gen_messy_library  # noqa: E402

FIXTURE = FIXTURES / "messy-library.html"


def sessions(bms, gap=1800):
    """Group bookmarks into sittings: neighbours (by save time) closer than ``gap`` seconds belong together."""
    out, cur, last = [], [], None
    for b in sorted((b for b in bms if b.added), key=lambda b: b.added):
        if last is not None and b.added - last > gap:
            out.append(cur)
            cur = []
        cur.append(b)
        last = b.added
    return out + ([cur] if cur else [])


class Fixture(CliCase):
    def setUp(self):
        super().setUp()
        self.text = FIXTURE.read_text(encoding="utf-8")
        self.manifest = gen_messy_library.build().manifest()
        self.run_cli("import", str(FIXTURE))
        self.bms = self.lib().all(snippet=0)

    def test_committed_file_is_exactly_what_the_generator_produces(self):
        self.assertEqual(self.text, gen_messy_library.generate(), "run tests/fixtures/gen_messy_library.py to refresh the fixture")

    def test_import_keeps_every_quirk(self):
        m, lib = self.manifest, self.lib()
        self.assertEqual(len(self.bms), m["bookmarks"])
        self.assertEqual(sum(1 for b in self.bms if b.folder == "Bookmarks bar"), m["loose"])
        folders = lib.folders()
        self.assertEqual(len(folders), m["folders"] + 2)                       # + the two Chrome roots
        self.assertEqual(sum(1 for f in folders if any(p != p.strip() for p in f.split("/"))), m["trailing_space_folders"])
        self.assertEqual(sum(1 for f in folders if "∕" in f), m["slash_folders"])
        self.assertTrue({f"Other bookmarks/{name}" for name in ("Misc ideas", "Someday ")} <= folders)  # empty folders survive, spaces and all
        self.assertEqual(max(f.count("/") + 1 for f in folders), m["max_depth"])
        self.assertEqual(sum(1 for b in self.bms if not b.title), m["empty_titles"])
        self.assertEqual(sum(1 for b in self.bms if "?" in b.url), m["tracking_urls"])
        self.assertEqual(sum(1 for b in self.bms if any("؀" <= c <= "ۿ" for c in b.title)), m["persian_titles"])
        self.assertEqual(len(lib.icons()), self.text.count(' ICON="'))
        self.assertGreaterEqual(len(lib.folder_meta()), m["folders"] - len(gen_messy_library.EMPTY_FOLDERS) - 1)   # folder dates kept

    def test_html_export_and_reimport_loses_nothing(self):
        out = self.dir / "again.html"
        self.run_cli("export", "--format", "html", "--out", str(out))
        first = self.lib()
        again = formats.parse_netscape(out.read_text(encoding="utf-8"))
        row = lambda b: (b.url, b.title, b.folder, b.added)  # noqa: E731
        self.assertEqual(sorted(map(row, again)), sorted(map(row, first.all(snippet=0))))
        self.assertEqual(again.folders, first.folders())                       # including the empty ones and every trailing space
        self.assertEqual(again.folder_meta, first.folder_meta())
        self.assertEqual(sum(1 for b in again if b.icon), len(first.icons()))

    def test_chrome_export_verifies_and_reimports_identically(self):
        out = self.dir / "Bookmarks"
        self.run_cli("export", "--format", "chrome", "--out", str(out))
        report = self.js("verify-chrome", str(out))
        self.assertEqual(report.get("problems", report.get("issues", [])), [])
        back = formats.read_file(out)
        row = lambda b: (b.url, b.title, b.folder, b.added)  # noqa: E731
        self.assertEqual(sorted(map(row, back)), sorted(map(row, self.bms)))
        self.assertEqual(back.folders - {"Mobile bookmarks"}, self.lib().folders())   # Chrome always writes its three roots

    def test_cleanup_numbers_match_what_the_file_contains(self):
        m = self.manifest
        self.assertEqual(self.js("clean-urls")["count"], m["tracking_urls"])
        res = self.js("dedupe")
        self.assertEqual(res["duplicates"], m["same_page_twice"])
        self.run_cli("dedupe", "--apply")
        filed_twice = [b for b in self.lib().all(snippet=0) if "also filed in" in b.notes.lower()]
        self.assertEqual(len(filed_twice), m["same_page_twice"])              # no placement is lost silently
        self.run_cli("undo")
        self.assertEqual(self.lib().count(), m["bookmarks"])

    def test_reimporting_the_same_file_changes_nothing(self):
        self.assertEqual(self.js("import", str(FIXTURE))["imported"], 0)
        self.assertEqual(self.lib().count(), self.manifest["bookmarks"])


class Accuracy(CliCase):
    """Regression floors for the filing model on the synthetic tree.

    The fixture's vocabulary is far cleaner than a real person's bookmarks, so these numbers are much higher than what
    ``learn`` reports on a real library; they exist to catch a model that gets worse, not to describe how good it is.
    Measured when written: top-1 74%, 80%-target tier answers 75% of bookmarks at 96% precision."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from mhbm.formats import read_file
        cls.bms = list(read_file(FIXTURE))
        cls.report = learn.evaluate(cls.bms, seeds=1)

    def test_cross_validated_numbers(self):
        r = self.report
        self.assertEqual(r["mode"], "stacked")
        self.assertGreaterEqual(r["top1"], 0.68)
        self.assertGreaterEqual(r["top3"], r["top1"])
        t = r["targets"]["0.8"]
        self.assertGreaterEqual(t["overall"]["precision"], 0.92)               # when it answers at 80%+, it is right at least that often
        self.assertGreaterEqual(t["overall"]["share"], 0.68)                   # ...and it answers for most bookmarks
        self.assertGreaterEqual(t["exact"]["precision"], 0.92)
        self.assertGreaterEqual(r["targets"]["0.9"]["overall"]["precision"], t["overall"]["precision"] - 0.01)   # asking for more certainty never hurts

    def test_whole_sittings_held_out_then_organized(self):
        """Hide entire saving sessions (the honest test), file them with the model, and compare with where they really were."""
        import copy
        bms = copy.deepcopy(self.bms)
        held = {}
        for n, sitting in enumerate(sessions(bms)):
            if n % 5 == 0:
                for b in sitting:
                    if not is_unfiled(b):
                        held[b.id] = b.folder
                        b.folder = ""
        self.assertGreater(len(held), 80)
        model = learn.FolderModel(bms)
        plan = categorize.propose(bms, [], model, min_conf=0.85, allow_new_folders=False)
        answered = {i: p for i, p in plan.items() if i in held}
        right = sum(1 for i, p in answered.items() if held[i] == p["folder"] or held[i].startswith(p["folder"] + "/"))
        self.assertGreaterEqual(len(answered) / len(held), 0.5, "coverage fell")
        self.assertGreaterEqual(right / len(answered), 0.93, "precision fell")
        exact = [i for i, p in answered.items() if p["level"] == "exact"]
        self.assertGreaterEqual(sum(held[i] == answered[i]["folder"] for i in exact) / max(1, len(exact)), 0.93)


@unittest.skipUnless(os.environ.get("MHBM_SLOW"), "slow: trains many times; CI sets MHBM_SLOW=1")
class AccuracySlow(unittest.TestCase):
    """The multi-split numbers behind the claims in the README: several seeds, and accuracy growing with the data."""

    @classmethod
    def setUpClass(cls):
        from mhbm.formats import read_file
        cls.bms = list(read_file(FIXTURE))

    def test_three_splits_agree_and_clear_the_floors(self):
        r = learn.evaluate(self.bms, seeds=3)
        lo, hi = r["top1_range"]
        self.assertLess(hi - lo, 0.06, "the splits disagree a lot: the number is not trustworthy")
        self.assertGreaterEqual(r["top1"], 0.68)
        self.assertGreaterEqual(r["top1_pooled"], r["top1"] - 0.01)            # judging sitting-mates together never hurts the ranking
        self.assertGreaterEqual(r["targets"]["0.8"]["overall"]["precision"], 0.92)
        self.assertGreaterEqual(r["targets"]["0.9"]["overall"]["precision"], 0.93)

    def test_more_filed_bookmarks_do_not_make_it_worse(self):
        rows = learn.curve(self.bms, fractions=(0.4, 0.7, 1.0))
        self.assertEqual(len(rows), 3)
        self.assertGreaterEqual(rows[-1]["top1"], rows[0]["top1"] - 0.02)


if __name__ == "__main__":
    unittest.main()
