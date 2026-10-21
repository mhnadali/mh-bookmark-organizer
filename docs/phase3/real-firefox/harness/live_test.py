#!/usr/bin/env python3
"""What happens when mhbm reads a profile WHILE REAL FIREFOX IS RUNNING and writing?

usage: MHBM_FIREFOX_DIR=... MHBM_SCRIPTS=<skill>/scripts python3 -I live_test.py <profile-template-dir> <workdir>
           [--seconds 20] [--ckpt-ms 40] [--variants baseline,shipped]

Phase A  static facts about a running Gecko profile (files present, lock, direct read-only open attempts, mhbm.load on it)
Phase B  stress: Gecko inserts bookmarks one commit at a time and force-checkpoints (`PRAGMA wal_checkpoint(TRUNCATE)`)
         every --ckpt-ms while this script copies+parses the live profile over and over.  A consistent snapshot must
         contain bookmarks race-0..race-k with no holes.  `baseline` = the old unverified db+wal copy; `shipped` = the
         `shipped` = whatever mhbm.browsers._copy does in the tree under test (the copy-and-verify of the Gecko patch).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True
import ffenv  # noqa: E402
import marionette  # noqa: E402

START_JS = r"""
const { PlacesUtils } = ChromeUtils.importESModule("resource://gre/modules/PlacesUtils.sys.mjs");
const B = PlacesUtils.bookmarks;
const folder = await B.insert({parentGuid: B.unfiledGuid, type: B.TYPE_FOLDER, title: "Race"});
const st = globalThis.__race = {i: 0, stop: false, errors: [], ckpts: 0};
const ckptMs = arguments[0], writeMs = arguments[1];
(async () => { while (!st.stop) { try { await B.insert({parentGuid: folder.guid, url: `https://race.example.org/${st.i}`, title: `race-${st.i}`}); st.i++; if (writeMs) await new Promise(r => setTimeout(r, writeMs)); } catch (e) { st.errors.push(String(e)); } } })();
(async () => { while (!st.stop) { try { await PlacesUtils.withConnectionWrapper("ckpt", db => db.execute("PRAGMA wal_checkpoint(TRUNCATE)")); st.ckpts++; } catch (e) { st.errors.push("ckpt " + e); } await new Promise(r => setTimeout(r, ckptMs)); } })();
return folder.guid;
"""
STOP_JS = "const st = globalThis.__race; st.stop = true; return {inserted: st.i, ckpts: st.ckpts, errors: st.errors.slice(0, 5)};"


def snapshot_check(parsed) -> tuple:
    """(ok, n) - the Race folder must hold race-0..race-(n-1) with no holes."""
    nums = sorted(int(b.title.split("-")[1]) for b in parsed if b.folder.endswith("/Race") or b.folder == "Race")
    return (nums == list(range(len(nums))), len(nums))


def fast_check(dest: Path) -> tuple:
    """integrity_check + 'no holes in race-N' on a copy.  Returns (ok, detail)."""
    c = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    try:
        integ = c.execute("pragma integrity_check").fetchall()
        if integ != [("ok",)]:
            return False, "integrity_check: " + "; ".join(str(r[0])[:60] for r in integ[:2])
        n, mx = c.execute("select count(*), max(cast(substr(title, 6) as integer)) from moz_bookmarks where title like 'race-%'").fetchone()
        if n and n != mx + 1:
            return False, f"hole: {n} rows but max index {mx}"
        return True, str(n)
    finally:
        c.close()


def run_variant_fast(name, copy_fn, prof: Path, copies: int) -> dict:
    res = {"variant": name, "copies": 0, "ok": 0, "bad": 0, "details": {}}
    for _ in range(copies):
        try:
            dest = copy_fn(prof / "places.sqlite")
        except Exception:
            res["copies"] += 1
            res["refused"] = res.get("refused", 0) + 1
            continue
        try:
            good, detail = fast_check(dest)
        except sqlite3.Error as e:
            good, detail = False, f"{type(e).__name__}: {str(e)[:70]}"
        finally:
            shutil.rmtree(dest.parent, ignore_errors=True)
        res["copies"] += 1
        res["ok" if good else "bad"] += 1
        if not good:
            res["details"][detail] = res["details"].get(detail, 0) + 1
    return res


def run_variant(browsers, name, copy_fn, prof: Path, seconds: float, work: Path) -> dict:
    res = {"variant": name, "copies": 0, "ok": 0, "holes": 0, "errors": {}, "max_n": 0, "retries_total": 0, "copy_ms": []}
    end = time.time() + seconds
    orig = browsers._copy
    browsers._copy = copy_fn
    try:
        while time.time() < end:
            t = time.time()
            try:
                parsed = browsers.load("firefox", profile=str(prof / "places.sqlite"))
                good, n = snapshot_check(parsed)
                res["copies"] += 1
                res["ok" if good else "holes"] += 1
                res["max_n"] = max(res["max_n"], n)
            except Exception as e:
                res["copies"] += 1
                k = f"{type(e).__name__}: {str(e)[:80]}"
                res["errors"][k] = res["errors"].get(k, 0) + 1
            res["copy_ms"].append((time.time() - t) * 1000)
    finally:
        browsers._copy = orig
    ms = sorted(res.pop("copy_ms"))
    res["load_ms_p50"], res["load_ms_p95"] = round(ms[len(ms) // 2], 1), round(ms[int(len(ms) * 0.95)], 1)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("template")
    ap.add_argument("work")
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--ckpt-ms", type=int, default=40)
    ap.add_argument("--variants", default="baseline", help="comma list of: baseline, shipped")
    ap.add_argument("--probe", action="store_true", help="only Phase A (no writer, no stress) plus a comparison with <template>.truth.json")
    ap.add_argument("--write-ms", type=int, default=0, help="pause between Gecko-side inserts (0 = flat out)")
    ap.add_argument("--fast", type=int, default=0, help="N copies per variant checked with integrity_check/holes instead of full parse")
    ap.add_argument("--slow", default="", help="chunk_bytes,sleep_ms to widen the copy window, e.g. 262144,2")
    args = ap.parse_args()
    ffenv.ensure_netns()
    sys.path.insert(0, os.environ["MHBM_SCRIPTS"])
    from mhbm import browsers

    work = Path(args.work).resolve()
    shutil.rmtree(work, ignore_errors=True)
    prof = work / "profile"
    shutil.copytree(args.template, prof, ignore=shutil.ignore_patterns("lock", ".parentlock", "*.log"))
    ffenv.write_user_js(prof, {})
    proc = ffenv.launch(prof, work / "home", log=work / "ff.log", netns=False, extra_args=["-marionette", "--remote-allow-system-access"])
    report: dict = {}
    try:
        m = marionette.Marionette()
        m.connect(90)
        m.new_session()
        m.chrome()
        time.sleep(2)
        if not args.probe:
            m.run(START_JS, [args.ckpt_ms, args.write_ms])
            time.sleep(1.0)
        # ---------------- Phase A
        a: dict = {"files": {f: os.lstat(prof / f).st_size for f in sorted(os.listdir(prof)) if f.startswith(("places", "lock", ".parentlock"))}}
        a["lock_target"] = os.readlink(prof / "lock") if (prof / "lock").is_symlink() else None
        db = prof / "places.sqlite"
        for label, uri in (("ro", f"file:{db}?mode=ro"), ("immutable", f"file:{db}?mode=ro&immutable=1")):
            try:
                c = sqlite3.connect(uri, uri=True, timeout=0.5)
                a[f"direct_{label}"] = f"opened; bookmarks={c.execute('select count(*) from moz_bookmarks').fetchone()[0]}"
                c.close()
            except sqlite3.Error as e:
                a[f"direct_{label}"] = f"{type(e).__name__}: {e}"
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
            a["direct_ro_user_version"] = c.execute("pragma user_version").fetchone()[0]
            c.close()
        except sqlite3.Error as e:
            a["direct_ro_user_version"] = f"{type(e).__name__}: {e}"
        t = time.time()
        parsed = browsers.load("firefox", profile=str(db))
        a["mhbm_load_live"] = f"{len(parsed)} bookmarks in {(time.time() - t) * 1000:.0f} ms"
        truth_file = Path(str(Path(args.template).resolve()) + ".truth.json")
        if args.probe and truth_file.exists():
            import compare
            want = sorted(compare.truth_items(json.loads(truth_file.read_text(encoding="utf-8")), include_place_queries=False))
            got = sorted(compare.mhbm_items(parsed))
            a["live_load_equals_gecko_truth"] = got == want
            a["direct_ro_blocked"] = "database is locked" in str(a.get("direct_ro"))
        report["phase_A"] = a
        if args.probe:
            m.quit(("eAttemptQuit",))
            proc.wait(60)
            print(json.dumps(report, ensure_ascii=False, indent=1))
            return 0
        # ---------------- Phase B
        import copy_variants
        variants = {"baseline": copy_variants.instrumented_naive, "shipped": browsers._copy}
        report["phase_B"] = []
        for v in args.variants.split(","):
            for k in copy_variants.STATS:
                copy_variants.STATS[k] = 0
            if args.slow:
                copy_variants.install_slow(int(args.slow.split(",")[0]), float(args.slow.split(",")[1]) / 1000)
            r = run_variant_fast(v, variants[v], prof, args.fast) if args.fast else run_variant(browsers, v, variants[v], prof, args.seconds, work)
            r["copy_stats"] = dict(copy_variants.STATS) if v == "baseline" else None
            report["phase_B"].append(r)
        report["writer"] = m.run(STOP_JS)
        m.quit(("eAttemptQuit",))
        proc.wait(60)
    finally:
        ffenv.kill_group(proc)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
