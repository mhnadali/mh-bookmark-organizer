"""Big libraries stay usable. The bounds are generous (several times what a laptop measures) so they catch a return to
quadratic behaviour, not a slow CI machine."""
import math
import os
import random
import time
import unittest

import common  # noqa: F401
from mhbm import learn, restructure, triage
from mhbm.model import Bookmark, assign_ids, is_unfiled


def library(n_filed, n_unfiled, n_folders, seed=1):
    """A hierarchical synthetic library with a heavy-tailed folder size distribution."""
    rnd = random.Random(seed)
    topics = [f"topic{t}" for t in range(40)]
    vocab = {t: [f"{t}w{j}" for j in range(60)] for t in topics}
    glob = [f"common{j}" for j in range(200)]
    folders = []
    for i in range(n_folders):
        t = rnd.choice(topics)
        path = "/".join([t.title()] + [f"Sub{rnd.randint(0, 30)}" for _ in range(rnd.randint(1, 5))]) + f"/F{i}"
        folders.append((path, t, rnd.sample(vocab[t], 10), [f"h{rnd.randint(0, 2000)}.example.com" for _ in range(rnd.randint(1, 4))]))
    weights = [1 / math.sqrt(i + 1) for i in range(n_folders)]
    out, ts = [], 1_400_000_000
    for k in range(n_filed + n_unfiled):
        path, t, words, hosts = folders[rnd.choices(range(n_folders), weights)[0]]
        ts += rnd.choice([30, 60, 600, 5000, 90000])
        b = Bookmark(url=f"https://{rnd.choice(hosts)}/p/{k}", title=" ".join(rnd.sample(words, 2) + rnd.sample(vocab[t], 1) + rnd.sample(glob, 2)),
                     folder=path if k < n_filed else "", added=ts)
        b.norm = b.url
        out.append(b)
    assign_ids(out, set())
    return out


class Fast(unittest.TestCase):
    def test_restructure_on_twenty_thousand_bookmarks(self):
        bms = library(20000, 0, 800)
        t = time.time()
        proposals = restructure.analyse(bms)
        took = time.time() - t
        self.assertGreater(len(proposals), 10)
        self.assertLess(took, 10, f"restructure took {took:.1f}s (measured about 0.2s)")

    def test_triage_grouping_on_ten_thousand_unfiled_bookmarks(self):
        bms = library(0, 10000, 300)

        class Guess:
            ready = True

            def distribution(self, b, exclude=()):
                r = random.Random(sum(map(ord, b.id)))
                return {f"F{r.randint(0, 200)}": r.random() / 3 for _ in range(3)}

        t = time.time()
        clusters = triage.build(bms, Guess())
        took = time.time() - t
        self.assertEqual(sum(len(c.members) for c in clusters), 10000)
        self.assertTrue(all(len(c.members) <= triage.MAX_CLUSTER for c in clusters))
        self.assertLess(took, 10, f"grouping took {took:.1f}s (measured about 0.4s)")

    def test_a_query_costs_the_same_however_big_the_library_is(self):
        bms = library(8000, 60, 400)
        filed = [b for b in bms if not is_unfiled(b)]
        unfiled = [b for b in bms if is_unfiled(b)]
        base = learn.Base(filed)
        t = time.time()
        for b in unfiled:
            base.candidate_rows(b)
        per = (time.time() - t) / len(unfiled)
        self.assertLess(per, 0.15, f"{per * 1000:.0f} ms per bookmark (measured about 10 ms)")

    def test_posting_lists_are_capped(self):
        filed = [b for b in library(9000, 0, 300) if not is_unfiled(b)]
        prep = learn._Prep(filed, learn._CFG_MAIN)
        df = {}
        for w in prep.unit:
            for t in w:
                df[t] = df.get(t, 0) + 1
        usable = [n for t, n in df.items() if t not in prep.common]
        self.assertLessEqual(max(usable), learn.COMMON_CAP)


@unittest.skipUnless(os.environ.get("MHBM_SLOW"), "slow: trains on 6,000 bookmarks; CI sets MHBM_SLOW=1")
class Slow(unittest.TestCase):
    def test_training_a_six_thousand_bookmark_library(self):
        bms = library(6000, 200, 300)
        filed = [b for b in bms if not is_unfiled(b)]
        t = time.time()
        model = learn.FolderModel(filed)
        took = time.time() - t
        self.assertEqual(model.mode, "stacked")
        self.assertGreater(model.report["top1"], 0.8)
        self.assertLess(took, 300, f"training took {took:.0f}s (measured about 40s with numpy and 4 cores)")

    def test_a_huge_library_judges_only_a_sample_out_of_sample(self):
        bms = library(7000, 0, 300)
        model = learn.FolderModel(bms)
        self.assertLessEqual(model.report["evaluated"], learn.OOF_MAX_ITEMS * 1.25)
        self.assertGreater(model.report["evaluated"], learn.OOF_MAX_ITEMS * 0.5)


if __name__ == "__main__":
    unittest.main()
