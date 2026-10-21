"""Tiny stdio MCP driver used by the audit.  Spawns `python -m mhbm --home H mcp` and records every line in both directions."""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

SCRATCH = "/tmp/claude-0/-home-user-adding-skills/e94ea44a-2022-55bc-9ae0-034b4c065efe/scratchpad/phase3/mcp-audit"
PKG_ORIG = "/home/user/adding-skills/.claude/skills/mh-bookmark-organizer/scripts"   # unmodified repo (mhbm mcp is broken here)
PKG_FIXED = SCRATCH + "/pkg_fixed"                                                      # copy with `from .. import mcp_server`
SEED_LIB = SCRATCH + "/lib-seed"


def fresh_home(seeded=True):
    d = tempfile.mkdtemp(prefix="home-", dir=SCRATCH + "/tmp")
    h = d + "/lib"
    if seeded:
        shutil.copytree(SEED_LIB, h)
    return h


class Server:
    def __init__(self, home, args=(), cwd=None, pkg=os.environ.get("AUDIT_PKG", PKG_FIXED), env_extra=None, quiet_stderr=False, pyflags=("-B", "-s")):
        env = dict(os.environ, PYTHONPATH=pkg, PYTHONDONTWRITEBYTECODE="1", MHBM_LANG="en")
        env.update(env_extra or {})
        self.cmd = [sys.executable, *pyflags, "-m", "mhbm", "--home", home, "mcp", *args]
        self.p = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  cwd=cwd or SCRATCH + "/work", env=env)
        self.out_q: "queue.Queue" = queue.Queue()
        self.err_chunks: list = []
        self.log: list = []
        self.t0 = time.time()
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
        self.log.append((round(time.time() - self.t0, 3), ">>", data[:300].decode("utf-8", "replace") + ("...[%d bytes]" % len(data) if len(data) > 300 else "")))
        try:
            self.p.stdin.write(data)
            self.p.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            self.log.append((round(time.time() - self.t0, 3), "!!", f"stdin write failed: {e!r}"))

    def send(self, obj):
        self.send_raw((json.dumps(obj) + "\n").encode())

    def send_line(self, text: str):
        self.send_raw((text + "\n").encode("utf-8"))

    def recv(self, timeout=10.0):
        """Next stdout line as str, '' if none within timeout, None at EOF."""
        try:
            raw = self.out_q.get(timeout=timeout)
        except queue.Empty:
            return ""
        if raw is None:
            self.log.append((round(time.time() - self.t0, 3), "<<", "<EOF>"))
            return None
        s = raw.decode("utf-8", "replace").rstrip("\n")
        self.log.append((round(time.time() - self.t0, 3), "<<", s[:300] + ("...[%d bytes]" % len(s) if len(s) > 300 else "")))
        return s

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

    def init(self, version="2025-06-18"):
        r = self.rpc("initialize", {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "audit", "version": "0"}}, id=0)
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

    def dump(self, title=""):
        lines = [f"--- {title} (cmd: {' '.join(self.cmd[:3])} ... mcp {' '.join(self.cmd[self.cmd.index('mcp') + 1:])}) ---"]
        for t, d, s in self.log:
            lines.append(f"[{t:7.3f}s] {d} {s}")
        err = self.stderr().strip()
        if err:
            lines.append("[stderr] " + err[:1500].replace("\n", "\n         "))
        lines.append(f"[exit] {self.p.poll()}")
        return "\n".join(lines)


def tree_state(root):
    """{relative path: (size, sha1-ish mtime)} for diffing a library directory before/after."""
    import hashlib
    out = {}
    for dp, dn, fn in os.walk(root):
        for f in fn:
            p = os.path.join(dp, f)
            try:
                with open(p, "rb") as fh:
                    h = hashlib.sha1(fh.read()).hexdigest()[:10]
                out[os.path.relpath(p, root)] = (os.path.getsize(p), h)
            except OSError:
                pass
    return out


def diff_state(a, b):
    ch = {k: (a.get(k), b.get(k)) for k in set(a) | set(b) if a.get(k) != b.get(k)}
    return ch
