"""The speed-ups are only worth having if they change nothing but the time: same numbers with and without numpy, with and
without worker processes, with and without the feature cache."""
import contextlib
import json
import os
import random
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import common  # noqa: F401
import test_organize
from mhbm import learn


@contextlib.contextmanager
def env(**values):
    old = {k: os.environ.get(k) for k in values}
    os.environ.update({k: v for k, v in values.items() if v is not None})
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def problem(n=1500, d=40, seed=5):
    rnd = random.Random(seed)
    w = [rnd.gauss(0, 1) for _ in range(d)]
    X = [[rnd.gauss(0, 1) + (2 if j % 5 == 0 else 0) for j in range(d)] for _ in range(n)]
    y = [1.0 if sum(a * b for a, b in zip(w, x)) + rnd.gauss(0, 2) > 0 else 0.0 for x in X]
    return X, y


@unittest.skipUnless(learn._np is not None, "numpy is not installed")
class NumpyParity(unittest.TestCase):
    def test_the_vectorised_fit_matches_the_pure_python_one(self):
        X, y = problem()
        with env(MHBM_NO_NUMPY="1"):
            self.assertFalse(learn.numpy_active())
            pure = learn.fit_logistic(X, y)
        with env(MHBM_NO_NUMPY=None):
            self.assertTrue(learn.numpy_active())
            fast = learn.fit_logistic(X, y)
        for name, a, b in zip(("mu", "sd", "w"), pure[:3], fast[:3]):
            self.assertLess(max(abs(p - q) for p, q in zip(a, b)), 1e-9, name)
        self.assertAlmostEqual(pure[3], fast[3], places=9)
        self.assertLess(max(abs(learn.logit(pure, r) - learn.logit(fast, r)) for r in X[:200]), 1e-9)

    def test_the_result_is_plain_python_so_it_can_be_cached_as_json(self):
        X, y = problem(n=400)
        model = learn.fit_logistic(X, y)
        self.assertTrue(all(isinstance(v, list) and all(type(x) is float for x in v) for v in model[:3]))
        self.assertIs(type(model[3]), float)
        json.dumps(model)

    def test_tiny_problems_stay_in_pure_python(self):
        X, y = problem(n=20, d=5)
        with mock.patch.object(learn, "_fit_logistic_numpy", side_effect=AssertionError("should not be used")):
            learn.fit_logistic(X, y)


class SmallLibraries(unittest.TestCase):
    def test_a_small_library_fits_the_same_whatever_the_batch_order(self):
        """Regression: with few mini-batches the combiner used to stop early, and one probe's confidence moved from 0.7 to
        1.0 depending only on the order the batches came in. Now small problems get a minimum number of steps."""
        filed, unfiled = test_organize.synthetic_library(12, 14)
        model = learn.FolderModel(filed)
        items = model._oof_items(1, 5, 1, lambda _msg: None)
        X = [r for it in items for r in it[2]]
        y = [1.0 if f == it[0] else 0.0 for it in items for f in it[1]]
        probe = next(b for b in unfiled if b.id == "u-health")
        _, _, rows = model._base().candidate_rows(probe)
        tops = []
        for seed in (0, 1, 2, 3):
            fitted = learn.fit_logistic(X, y, seed=seed)
            tops.append(max(learn.folder_probs([learn.logit(fitted, r) for r in rows])))
        self.assertLess(max(tops) - min(tops), 0.05, tops)
        self.assertGreater(min(tops), 0.9)

    def test_epochs_scale_with_the_problem_size(self):
        seen = []
        real = learn._fit_logistic_numpy

        def spy(rows, labels, epochs, *rest):
            seen.append(epochs)
            return real(rows, labels, epochs, *rest)

        small = problem(n=300, d=10)
        big = problem(n=20000, d=10)
        with mock.patch.object(learn, "_fit_logistic_numpy", spy), env(MHBM_NO_NUMPY=None):
            if learn._np is None:
                self.skipTest("numpy is not installed")
            learn.fit_logistic(*small)
            learn.fit_logistic(*big)
        self.assertGreater(seen[0], learn.STACK_EPOCHS)                            # a small problem gets extra passes
        self.assertEqual(seen[1], learn.STACK_EPOCHS)                              # a big one the normal number
        self.assertLessEqual(seen[0], learn.MAX_EPOCHS)


class Shuffle(unittest.TestCase):
    def test_it_is_a_seeded_permutation(self):
        a = list(range(1000))
        b = list(range(1000))
        learn._shuffled(a, random.Random(3))
        learn._shuffled(b, random.Random(3))
        self.assertEqual(a, b)
        self.assertEqual(sorted(a), list(range(1000)))
        self.assertNotEqual(a, list(range(1000)))
        c = list(range(1000))
        learn._shuffled(c, random.Random(4))
        self.assertNotEqual(a, c)


class Caches(unittest.TestCase):
    def setUp(self):
        self.filed, self.unfiled = test_organize.synthetic_library(12, 14)

    def test_the_feature_cache_returns_what_features_would(self):
        cache = learn.FeatureCache()
        b = self.filed[0]
        first = cache(b, learn._CFG_MAIN, "main")
        self.assertIs(cache(b, learn._CFG_MAIN, "main"), first)                    # remembered
        self.assertEqual(first, learn.features(b, learn._CFG_MAIN))
        self.assertNotEqual(cache(b, learn._CFG_TEXT, "text"), first)             # keyed by feature set as well
        cache.close()
        self.assertEqual(cache.store, {})
        again = cache(b, learn._CFG_MAIN, "main")
        self.assertEqual(again, first)
        self.assertEqual(cache.store, {})                                           # closed: computes, never stores

    def test_a_cache_does_not_change_the_model(self):
        with mock.patch.object(learn, "FEATURE_CACHE_MAX", 0):
            plain = learn.FolderModel(self.filed)
        cached = learn.FolderModel(self.filed)
        self.assertEqual(plain.report, cached.report)
        self.assertEqual([plain.predict(b, 0.5) for b in self.unfiled], [cached.predict(b, 0.5) for b in self.unfiled])
        self.assertEqual(cached._feats.store, {})                                   # emptied once training is over

    def test_shared_preparation_gives_the_same_scores_as_separate(self):
        train = self.filed
        shared = learn._Prep(train, learn._CFG_MAIN)
        a = learn.KNNScorer(train, learn._CFG_MAIN, 10, prep=shared)
        b = learn.KNNScorer(train, learn._CFG_MAIN, 10)
        ca = learn.CentroidScorer(train, learn._CFG_MAIN, prep=shared)
        cb = learn.CentroidScorer(train, learn._CFG_MAIN)
        for probe in self.unfiled[:6]:
            self.assertEqual(a.neighbors(probe), b.neighbors(probe))
            self.assertEqual(dict(ca.scores(probe)), dict(cb.scores(probe)))


class NeighbourMemo(unittest.TestCase):
    def test_the_latest_query_is_remembered_but_never_stale(self):
        filed, unfiled = test_organize.synthetic_library(12, 14)
        knn = learn.KNNScorer(filed, learn._CFG_MAIN, 10)
        probe = unfiled[0]
        calls = []
        real = knn._neighbors
        knn._neighbors = lambda b: calls.append(b.id) or real(b)
        first = knn.neighbors(probe)
        self.assertIs(knn.neighbors(probe), first)                                  # explaining an answer repeats nothing
        self.assertEqual(calls, [probe.id])
        probe.title = "completely different words about kubernetes operators"      # edited in between: recomputed
        again = knn.neighbors(probe)
        self.assertEqual(len(calls), 2)
        self.assertNotEqual([d.id for d, _ in first], [d.id for d, _ in again])
        knn.neighbors(unfiled[1])
        self.assertEqual(len(calls), 3)


class Workers(unittest.TestCase):
    def test_how_many(self):
        with env(MHBM_WORKERS=None):
            self.assertEqual(learn.worker_count(100, 5), 1)                         # too small to be worth starting processes
            self.assertEqual(learn.worker_count(10_000, 1), 1)
            self.assertLessEqual(learn.worker_count(10_000, 5), 5)
        with env(MHBM_WORKERS="1"):
            self.assertEqual(learn.worker_count(10_000, 5), 1)
        with env(MHBM_WORKERS="3"):
            self.assertEqual(learn.worker_count(10, 5), 3)
            self.assertEqual(learn.worker_count(10, 2), 2)                          # never more than there are folds

    def test_parallel_folds_give_the_same_answer_as_serial_ones(self):
        filed, unfiled = test_organize.synthetic_library(12, 14)
        with env(MHBM_WORKERS="1"):
            serial = learn.FolderModel(filed)
        with env(MHBM_WORKERS="2"):
            parallel = learn.FolderModel(filed)
        self.assertEqual(serial.report, parallel.report)
        self.assertEqual([serial.predict(b, 0.5) for b in unfiled], [parallel.predict(b, 0.5) for b in unfiled])

    def test_trouble_with_the_pool_falls_back_to_one_process(self):
        filed, _ = test_organize.synthetic_library(12, 14)

        class Broken:
            def __init__(self, *a, **k):
                raise OSError("no processes in this sandbox")

        with env(MHBM_WORKERS="1"):
            expected = learn.FolderModel(filed).report
        with env(MHBM_WORKERS="3"), mock.patch("concurrent.futures.ProcessPoolExecutor", Broken):
            self.assertEqual(learn.FolderModel(filed).report, expected)

    def test_the_command_line_is_not_run_again_inside_a_worker(self):
        here = Path(__file__).resolve().parent.parent / "scripts"
        r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import mhbm.__main__; print('imported')", str(here)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "imported"), r.stderr)

    def test_a_big_library_trains_through_the_real_command_line_with_workers(self):
        import tempfile
        rnd = random.Random(8)
        topics = [f"topic{i}" for i in range(14)]
        rows = ["url,title,folder,tags,added"]
        for i, t in enumerate(topics):
            vocab = [f"{t}w{j}" for j in range(8)]
            for k in range(46):
                rows.append(f"https://s{rnd.randint(0, 5)}.example/{t}/{k},{' '.join(rnd.sample(vocab, 3))} item,Lib/{t},{t},{1_600_000_000 + i * 10**6 + k * 5000}")
        with tempfile.TemporaryDirectory() as d:
            csv = Path(d, "big.csv")
            csv.write_text("\n".join(rows), encoding="utf-8")
            scripts = str(Path(__file__).resolve().parent.parent / "scripts")
            base = [sys.executable, "-m", "mhbm", "--home", str(Path(d, "lib")), "--quiet"]
            envv = {**os.environ, "PYTHONPATH": scripts, "MHBM_WORKERS": "2", "MHBM_LANG": "en"}
            self.assertEqual(subprocess.run(base + ["import", str(csv)], capture_output=True, env=envv, timeout=120).returncode, 0)
            r = subprocess.run(base + ["--json", "learn"], capture_output=True, text=True, env=envv, timeout=300)
            self.assertEqual(r.returncode, 0, r.stderr)
            report = json.loads(r.stdout)
            self.assertEqual((report["mode"], report["training"]), ("stacked", 14 * 46))
            self.assertGreater(report["top1"], 0.5)


class Doctor(unittest.TestCase):
    def test_the_diagnosis_says_whether_numpy_is_used(self):
        from mhbm.commands import diag
        (name, status, detail, _), = diag._check_speed()
        self.assertEqual((name, status), ("training speed", "info"))
        self.assertIn("numpy", detail)


if __name__ == "__main__":
    unittest.main()
