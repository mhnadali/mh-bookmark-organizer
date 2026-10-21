#!/usr/bin/env python3
"""Smoke test of an INSTALLED mh-bookmark-organizer (wheel or sdist), run with the interpreter of the venv it was installed into.

    python smoke_installed.py ARTIFACT --fixture messy-library.html [--expect-version 1.6.0] [--extra fast]   # makes the venv, installs, tests
    <venv>/bin/python smoke_installed.py --fixture messy-library.html [--expect-version 1.6.0] [--numpy yes|no|any]   # tests what is installed

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
            except Exception as e:      # a smoke test reports every failure
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


def install_and_rerun(args) -> int:
    """Make a virtual environment in a temporary folder, install the artifact into it, and run this script with that environment's Python."""
    venv = Path(tempfile.mkdtemp(prefix="mhbm-venv-"))
    try:
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        py = venv / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
        target = str(Path(args.artifact).resolve()) + (f"[{args.extra}]" if args.extra else "")
        print(f"installing {target} into {venv}", flush=True)
        subprocess.run([str(py), "-m", "pip", "install", "--disable-pip-version-check", "--quiet", target], check=True)
        cmd = [str(py), str(Path(__file__).resolve()), "--fixture", args.fixture, "--numpy", "yes" if args.extra else args.numpy]
        if args.expect_version:
            cmd += ["--expect-version", args.expect_version]
        if args.skip_learn:
            cmd += ["--skip-learn"]
        return subprocess.run(cmd, cwd=tempfile.gettempdir()).returncode
    finally:
        shutil.rmtree(venv, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("artifact", nargs="?", help="a wheel or an sdist: install it (with --extra) into a fresh virtual environment, then test that")
    ap.add_argument("--fixture", required=True, help="synthetic Netscape HTML bookmark file")
    ap.add_argument("--expect-version")
    ap.add_argument("--numpy", choices=["yes", "no", "any"], default="any")
    ap.add_argument("--skip-learn", action="store_true")
    ap.add_argument("--extra", help="install this extra with the artifact (it implies --numpy yes)")
    args = ap.parse_args()
    if args.artifact:
        return install_and_rerun(args)

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

    def mcp_session(command, calls):
        """Speak newline-delimited JSON-RPC to an MCP server: initialize, then each (id, method, params); stdin stays open until every answer is in."""
        p = subprocess.Popen(command, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        replies: dict = {}
        try:
            def send(msg):
                p.stdin.write(json.dumps(msg) + "\n")
                p.stdin.flush()

            def wait_for(rid):
                t0 = time.time()
                while time.time() - t0 < 60:
                    line = p.stdout.readline()
                    if not line:
                        raise RuntimeError(f"server closed its output; stderr: {p.stderr.read()[-400:]}")
                    msg = json.loads(line)
                    if msg.get("id") == rid:
                        return msg
                raise TimeoutError(f"no answer to request {rid}")
            send({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "0"}}})
            replies[0] = wait_for(0)
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            for rid, method, params in calls:
                send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
                replies[rid] = wait_for(rid)
        finally:
            p.stdin.close()
            try:
                code = p.wait(30)
            except subprocess.TimeoutExpired:
                p.kill()
                code = p.wait()
            err = p.stderr.read()
            for f in (p.stdout, p.stderr):
                f.close()
        assert code == 0, f"exit {code}; stderr: {err[-400:]}"
        assert not err.strip(), f"the server wrote to stderr: {err[-400:]}"
        return replies

    @step("MCP: `mhbm-mcp --read-only` lists only read tools and answers bm_stats")
    def _mcp():
        replies = mcp_session([script("mhbm-mcp"), "--home", str(home), "--read-only"],
                              [(1, "tools/list", {}), (2, "tools/call", {"name": "bm_stats", "arguments": {}})])
        assert replies[0]["result"]["serverInfo"]["name"] == "mh-bookmark-organizer", replies[0]
        tools = replies[1]["result"]["tools"]
        names = {t["name"] for t in tools}
        assert "bm_search" in names and "bm_apply_plan" not in names and "bm_undo" not in names, sorted(names)
        assert all(t["annotations"]["readOnlyHint"] for t in tools), "a read-only server offered a tool that is not read-only"
        res = replies[2]["result"]
        assert not res["isError"] and res["structuredContent"]["total"] == info["imported"]["library"], res
        return f"{len(names)} tools, library of {res['structuredContent']['total']}"
    _mcp()

    @step("MCP: `mhbm mcp` and `python -I -m mhbm.mcp` serve all tools; a plan can be made and is not applied")
    def _mcp_full():
        for command in ([mhbm, "--home", str(home), "mcp"], [py, "-I", "-m", "mhbm.mcp", "--home", str(home)]):
            replies = mcp_session(command, [(1, "tools/list", {}), (2, "tools/call", {"name": "bm_dedupe", "arguments": {}})])
            n = len(replies[1]["result"]["tools"])
            assert n >= 29, f"{n} tools"
            res = replies[2]["result"]
            assert not res["isError"], res
            assert res["structuredContent"]["plan_id"] is None or res["structuredContent"]["plan_id"].startswith("p-"), res
        out = run([mhbm, "--home", str(home), "--json", "stats"], env)
        assert json.loads(out.stdout)["total"] == info["imported"]["library"], "a plan tool changed the library"
        return f"{n} tools"
    _mcp_full()

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
