"""A small stdio client for the MCP server tests: starts ``python -m mhbm --home H mcp`` and speaks newline-delimited JSON-RPC to it.

Not a test module (the name does not match ``test*.py``).  The libraries the tests run against are built once per process from the
synthetic fixture ``fixtures/messy-library.html`` and copied for every test.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import common  # noqa: F401  (puts scripts/ on sys.path, sets MHBM_LANG)

TESTS = Path(__file__).resolve().parent
SCRIPTS = str(TESTS.parent / "scripts")
FIXTURE = TESTS / "fixtures" / "messy-library.html"

_ROOT = tempfile.mkdtemp(prefix="mhbm-mcp-tests-")
atexit.register(shutil.rmtree, _ROOT, True)
_templates: dict = {}


def scratch(prefix="t-") -> str:
    return tempfile.mkdtemp(prefix=prefix, dir=_ROOT)


def _run_cli(home: str, *args: str) -> None:
    env = dict(os.environ, PYTHONPATH=SCRIPTS, PYTHONDONTWRITEBYTECODE="1", MHBM_LANG="en")
    subprocess.run([sys.executable, "-B", "-m", "mhbm", "--home", home, "--quiet", *args], check=True, capture_output=True, env=env, timeout=120)


def _checkpointed_copy(src: str, dest: str) -> None:
    c = sqlite3.connect(src + "/library.db")
    try:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        c.close()
    os.makedirs(dest)
    shutil.copy(src + "/library.db", dest + "/library.db")


def _template(kind: str) -> str:
    if kind in _templates:
        return _templates[kind]
    built = scratch("template-") + "/built"
    if kind == "seed":                                      # 592 bookmarks, 99 folders, 6 duplicate pairs, 18 unfiled
        _run_cli(built, "import", str(FIXTURE))
    else:                                                   # "big": enough text that a 100-row answer exceeds a small size cap
        sys.path.insert(0, SCRIPTS)
        from mhbm.model import Bookmark
        from mhbm.store import Library
        with Library(built) as lib:
            lib.upsert([Bookmark(id=f"big{i:05d}", url=f"https://site{i % 40}.example/page/{i}", title=f"Notes about topic {i}",
                                 folder=f"Big/Area {i % 25}/Part {i % 7}", notes="notes " + "lorem ipsum dolor sit amet " * 12) for i in range(900)])
    out = scratch("template-") + "/lib"
    _checkpointed_copy(built, out)
    _templates[kind] = out
    return out


def new_home(seeded: bool = True, big: bool = False) -> str:
    """A fresh copy of the seed library (or an empty directory) inside the scratch area."""
    home = scratch() + "/lib"
    if big:
        shutil.copytree(_template("big"), home)
    elif seeded:
        shutil.copytree(_template("seed"), home)
    return home


def count(home: str) -> int:
    c = sqlite3.connect(home + "/library.db")
    try:
        return c.execute("select count(*) from bookmarks").fetchone()[0]
    finally:
        c.close()


class Server:
    """One ``mhbm mcp`` process.  ``recv`` returns '' on timeout and None at EOF."""

    def __init__(self, home, args=(), cwd=None, env_extra=None, pyflags=("-B", "-s"), command=None):
        """``command``: the argv to run instead of ``python -m mhbm --home H mcp ARGS`` (for the other entry points)."""
        env = dict(os.environ, PYTHONPATH=SCRIPTS, PYTHONDONTWRITEBYTECODE="1", MHBM_LANG="en")
        env.update(env_extra or {})
        self.home = home
        self.cmd = list(command) if command else [sys.executable, *pyflags, "-m", "mhbm", "--home", home, "mcp", *args]
        self.p = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd or scratch("cwd-"), env=env)
        self.out_q: queue.Queue = queue.Queue()
        self.err_chunks: list = []
        threading.Thread(target=self._pump_out, daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()

    def _pump_out(self):
        for raw in iter(self.p.stdout.readline, b""):
            self.out_q.put(raw)
        self.out_q.put(None)

    def _pump_err(self):
        for raw in iter(self.p.stderr.readline, b""):
            self.err_chunks.append(raw.decode("utf-8", "replace"))

    # ------------------------------------------------------------------ io
    def send_raw(self, data: bytes):
        try:
            self.p.stdin.write(data)
            self.p.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def send(self, obj):
        self.send_raw((json.dumps(obj) + "\n").encode())

    def send_line(self, text: str):
        self.send_raw((text + "\n").encode("utf-8"))

    def recv(self, timeout=10.0):
        try:
            raw = self.out_q.get(timeout=timeout)
        except queue.Empty:
            return ""
        return None if raw is None else raw.decode("utf-8", "replace").rstrip("\n")

    def recv_json(self, timeout=10.0):
        s = self.recv(timeout)
        if not s:
            return s
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return {"__not_json__": s[:200]}

    def rpc(self, method, params=None, id=1, timeout=30.0):
        msg = {"jsonrpc": "2.0", "id": id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        return self.recv_json(timeout)

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)

    def init(self, version="2025-06-18", capabilities=None):
        r = self.rpc("initialize", {"protocolVersion": version, "capabilities": capabilities or {}, "clientInfo": {"name": "tests", "version": "0"}}, id=0)
        self.notify("notifications/initialized")
        return r

    def close(self, wait=5.0):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
        for stream in (self.p.stdout, self.p.stderr, self.p.stdin):
            try:
                stream.close()
            except Exception:
                pass
        return self.p.returncode

    def stderr(self):
        return "".join(self.err_chunks)


def tree_state(root):
    """{relative path: (size, content hash)} of a library directory, to compare before and after."""
    out = {}
    for dp, _dn, fn in os.walk(root):
        for f in fn:
            p = os.path.join(dp, f)
            try:
                with open(p, "rb") as fh:
                    out[os.path.relpath(p, root)] = (os.path.getsize(p), hashlib.sha1(fh.read()).hexdigest()[:10])
            except OSError:
                pass
    return out


def diff_state(a, b):
    return {k: (a.get(k), b.get(k)) for k in set(a) | set(b) if a.get(k) != b.get(k)}

