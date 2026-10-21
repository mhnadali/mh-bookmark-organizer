"""The refactored, indexed ``restructure.analyse`` must propose exactly what the original did, on awkward libraries too."""
import random
import unittest
from pathlib import Path

import common  # noqa: F401
import reference_restructure
from mhbm import restructure
from mhbm.formats import read_file
from mhbm.model import Bookmark, assign_ids

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "messy-library.html"


def messy(seed):
    """The synthetic library with look-alike sibling folders (Sub/sub, F/f), bloated folders and random tags added."""
    rnd = random.Random(seed)
    bms = list(read_file(FIXTURE))
    assign_ids([b for b in bms if not b.id], set())
    for b in bms:
        if rnd.random() < 0.15 and b.folder:
            b.folder = b.folder.swapcase() if rnd.random() < 0.5 else b.folder.rstrip() + "s"
        if rnd.random() < 0.6:
            b.tags = [rnd.choice(["alpha", "beta", "gamma", "delta", "kiln", "glaze"])]
    big = [Bookmark(url=f"https://bulk{k % 7}.example/{k}", title=f"bulk {k}", folder="Bulk/Dump", id=f"bulk{k}", tags=[rnd.choice(["x1", "x2", "x3"])])
           for k in range(120)]
    return bms + big


class Restructure(unittest.TestCase):
    def test_same_proposals_as_the_original_on_varied_options(self):
        for seed in (1, 2, 3):
            bms = messy(seed)
            for kw in ({}, {"max_depth": 3}, {"max_size": 12, "min_size": 3}, {"min_group": 3, "max_size": 20}):
                with self.subTest(seed=seed, **kw):
                    self.assertEqual(restructure.analyse(bms, **kw), reference_restructure.analyse(bms, **kw))

    def test_the_fixture_exercises_every_kind_of_proposal(self):
        kinds = set()
        for seed in (1, 2, 3):
            for kw in ({}, {"max_depth": 3}, {"max_size": 12, "min_size": 3}):
                kinds |= {p["kind"] for p in restructure.analyse(messy(seed), **kw)}
        self.assertEqual(kinds, {"flatten", "merge-similar", "split", "fold"})


if __name__ == "__main__":
    unittest.main()
