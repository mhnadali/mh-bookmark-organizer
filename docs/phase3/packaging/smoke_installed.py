#!/usr/bin/env python3
"""Smoke test of an INSTALLED mh-bookmark-organizer (wheel or sdist), run with the interpreter of the venv it was installed into.

    <venv>/bin/python smoke_installed.py --fixture messy-library.html [--expect-version 1.6.0] [--numpy yes|no|any]

Rules it keeps (they are the point of the test):
  * run it from a directory that is NOT a checkout (the caller's cwd is irrelevant: it works in its own temp dir and
    uses ``python -I`` for the module run), so it can only see what was installed;
  * standard library only, Python 3.9+, works on Linux, macOS and Windows;
  * every step is reported; the exit status is 1 when any step failed (all steps still run, so one run shows every problem).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

RESULTS: list = []


def step(name):
    def deco(fn):
        def run(*a, **k):
            t = time.time()
            try:
                detail = fn(*a, **k)
                RESULTS.append((name, True, f"{detail or ''} ({time.time() - t:.1f}s)"))
            except Exception as e:  # noqa: BLE001 - a smoke test reports every failure
                RESULTS.append((name, False, f"{type(e).__name__}: {e}"))
        return run
    return deco


def script(name: str) -> str:
    """Path of a console script installed next to this interpreter."""
    bindir = Path(sys.executable).parent
    for cand in (bindir / name, bindir / f"{name}.exe", bindir / "Scripts" / f"{name}.exe"):
        if cand.exists():
            return str(cand)
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError(f"console script {name!r} not installed (looked in {bindir})")
    return found


def run(argv, env, timeout=300, cwd=None, input_=None):
    p = subprocess.run(argv, env=env, cwd=cwd, input=input_, capture_output=True, text=True, timeout=timeout)
    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", required=True, help="synthetic Netscape HTML bookmark file")
    ap.add_argument("--expect-version")
    ap.add_argument("--numpy", choices=["yes", "no", "any"], default="any")
    ap.add_argument("--skip-learn", action="store_true")
    ap.add_argument("--assets", action="store_true", help="also check the proposed extension-path / install-skill commands")
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="mhbm-smoke-"))
    os.chdir(work)  # never inside a checkout
    home = work / "lib"
    env = dict(os.environ, MHBM_HOME=str(home), PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", MHBM_WORKERS="1")
    for k in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
        env.pop(k, None)
    py = sys.executable
    mhbm = None
    info: dict = {}

    @step("import mhbm from site-packages (not from a checkout)")
    def _import():
        out = run([py, "-I", "-c", "import mhbm,sys,json;print(json.dumps([mhbm.__version__, mhbm.__file__]))"], env)
        assert out.returncode == 0, out.stderr
        ver, where = json.loads(out.stdout)
        info["version"] = ver
        assert "site-packages" in where or "dist-packages" in where, f"imported from {where}"
        if args.expect_version:
            assert ver == args.expect_version, f"version {ver} != expected {args.expect_version}"
        return f"{ver} at {where}"
    _import()

    @step("mhbm --version (console script)")
    def _version():
        nonlocal mhbm
        mhbm = script("mhbm")
        out = run([mhbm, "--version"], env)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == info.get("version"), f"printed {out.stdout.strip()!r}"
        return out.stdout.strip()
    _version()

    @step("python -I -m mhbm --version")
    def _module():
        out = run([py, "-I", "-m", "mhbm", "--version"], env)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == info.get("version")
    _module()

    @step("mhbm --json doctor (exit 0, no FAIL)")
    def _doctor():
        out = run([mhbm, "--home", str(home), "--json", "doctor"], env)
        data = json.loads(out.stdout)
        fails = [c for c in data["checks"] if c["status"] == "fail"]
        assert out.returncode == 0 and not fails, f"rc={out.returncode} fails={fails}"
        return f"summary={data['summary']}"
    _doctor()

    @step("packaged data file (taxonomy.json) is readable")
    def _taxonomy():
        out = run([py, "-I", "-c", "from mhbm import categorize as c;t=c.load_taxonomy();print(len(t))"], env)
        assert out.returncode == 0 and int(out.stdout) > 0, out.stderr
        return f"{out.stdout.strip()} categories"
    _taxonomy()

    @step("import the synthetic HTML export")
    def _import_html():
        out = run([mhbm, "--home", str(home), "--json", "import", args.fixture], env)
        assert out.returncode == 0, out.stderr + out.stdout
        d = json.loads(out.stdout)
        info["imported"] = d
        return json.dumps(d)[:120]
    _import_html()

    @step("search")
    def _search():
        out = run([mhbm, "--home", str(home), "--json", "search", "tutorial"], env)
        assert out.returncode == 0, out.stderr
        d = json.loads(out.stdout)
        n = len(d.get("results", d.get("items", d if isinstance(d, list) else [])))
        assert n > 0, f"no hits: {out.stdout[:200]}"
        return f"{n} hits"
    _search()

    @step("mhbm learn on the synthetic fixture")
    def _learn():
        if args.skip_learn:
            return "skipped"
        out = run([mhbm, "--home", str(home), "--json", "learn"], env, timeout=900)
        assert out.returncode == 0, out.stderr[-500:] + out.stdout[-300:]
        d = json.loads(out.stdout)
        return f"keys={sorted(d)[:6]}"
    _learn()

    @step("numpy extra is " + args.numpy)
    def _numpy():
        out = run([py, "-I", "-c", "import mhbm.learn as l;print(l.numpy_active())"], env)
        assert out.returncode == 0, out.stderr
        active = out.stdout.strip() == "True"
        if args.numpy == "yes":
            assert active, "numpy_active() is False"
        if args.numpy == "no":
            assert not active, "numpy_active() is True"
        return f"numpy_active={active}"
    _numpy()

    @step("MCP: initialize + tools/list over stdio, clean exit on EOF")
    def _mcp():
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "0"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        inp = "".join(json.dumps(m) + "\n" for m in msgs)
        out = run([mhbm, "--home", str(home), "mcp", "--read-only"], env, timeout=60, input_=inp)
        assert out.returncode == 0, f"rc={out.returncode} stderr={out.stderr[-400:]}"
        replies = [json.loads(x) for x in out.stdout.splitlines() if x.strip()]
        by_id = {r.get("id"): r for r in replies}
        assert by_id[1]["result"]["serverInfo"]["name"] == "mh-bookmark-organizer", by_id[1]
        names = [t["name"] for t in by_id[2]["result"]["tools"]]
        assert "bm_search" in names, names
        return f"{len(names)} tools (read-only)"
    _mcp()

    @step("mhbm ui starts on 127.0.0.1 and answers")
    def _ui():
        p = subprocess.Popen([mhbm, "--home", str(home), "ui", "--no-browser"], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        try:
            url = None
            t0 = time.time()
            while time.time() - t0 < 30:
                line = p.stdout.readline()
                if "http://127.0.0.1:" in line:
                    url = line.split("UI:")[1].strip()
                    break
                if p.poll() is not None:
                    raise RuntimeError(f"ui exited early rc={p.returncode}: {p.stderr.read()[-300:]}")
            assert url, "no URL printed"
            base = url.split("#")[0]
            with urllib.request.urlopen(base, timeout=10) as r:
                assert r.status == 200 and b"<" in r.read(100)
            return base
        finally:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
            for f in (p.stdout, p.stderr):
                if f:
                    f.close()
    _ui()

    if args.assets:
        @step("mhbm extension-path points at a loadable extension")
        def _extpath():
            out = run([mhbm, "extension-path"], env)
            assert out.returncode == 0, out.stderr
            d = Path(out.stdout.strip())
            m = json.loads((d / "manifest.json").read_text())
            assert m["version"] == info["version"], f"manifest {m['version']} != {info['version']}"
            for f in ("background.js", "lib.js", "popup.html", "popup.js", "popup.css"):
                assert (d / f).is_file(), f"missing {f}"
            return str(d)
        _extpath()

        @step("mhbm install-skill --dest <tmp> copies SKILL.md + agent")
        def _skill():
            dest = work / "claude-home"
            out = run([mhbm, "install-skill", "--dest", str(dest)], env)
            assert out.returncode == 0, out.stderr + out.stdout
            assert (dest / "skills" / "mh-bookmark-organizer" / "SKILL.md").is_file()
            assert (dest / "agents" / "mh-bookmark-organizer.md").is_file()
            return out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
        _skill()

    width = max(len(n) for n, _, _ in RESULTS)
    bad = 0
    for name, ok, detail in RESULTS:
        bad += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail}")
    shutil.rmtree(work, ignore_errors=True)
    print(f"\n{len(RESULTS) - bad}/{len(RESULTS)} steps passed (python {sys.version.split()[0]})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
