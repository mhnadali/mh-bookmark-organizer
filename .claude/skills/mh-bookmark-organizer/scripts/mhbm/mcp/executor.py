"""Run one ``mhbm`` command in a child process: the isolation layer under every tool.

Why a process per call instead of calling ``cli.main`` in-process (what the old server did):

* **cancellation and timeouts are real** - a worker thread cannot be interrupted, a process can (SIGINT, then SIGTERM, then SIGKILL; the
  library is transactional, so an interrupted ``--apply`` leaves either the old or the new state);
* **nothing can reach the protocol stream** - the child's stdout/stderr are pipes, its stdin is the null device;
* **no shared mutable state** - ``redirect_stdout``, ``urlnorm.configure``, the language, the open-library registry are all process-global;
* **a crash is contained** - MemoryError, RecursionError, a failed assertion or a native crash costs one tool call, not the session;
* **least privilege** - the child receives an allow-listed environment (no tokens that happen to be exported in the shell).

Cost: about 0.3 s of interpreter start-up per call (measured), irrelevant next to a model round trip.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

# environment variables the child may see (everything else, including secrets, is dropped)
ENV_ALLOW = {"PATH", "PYTHONPATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "TZ", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "SYSTEMDRIVE",
             "COMSPEC", "PATHEXT", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "HOMEDRIVE", "HOMEPATH", "XDG_CONFIG_HOME",
             "XDG_DATA_HOME", "XDG_CACHE_HOME", "DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
             "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
             "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"}
ENV_PREFIXES = ("MHBM_", "LC_")

# "checking: 40% (240/600)" is what the commands print on stderr (when not --quiet) outside a terminal
PROGRESS_RE = re.compile(r"^(?P<label>[^:\n]{1,60}): (?P<pct>\d{1,3})% \((?P<i>\d+)/(?P<n>\d+)\)\s*$")


@dataclass
class ExecResult:
    code: int = 0                    # exit status; negative = killed by that signal
    stdout: bytes = b""
    stderr: str = ""                 # tail only
    timed_out: bool = False
    cancelled: bool = False
    overflow: bool = False           # stdout exceeded the cap and the child was stopped
    elapsed: float = 0.0
    argv: list = field(default_factory=list)


def _package_parent() -> str | None:
    """The directory the child must have on ``PYTHONPATH`` to import ``mhbm`` - None when its interpreter finds it by itself (installed).

    The skill runs from a source checkout (``scripts/``), where only the parent's ``sys.path`` knows where ``mhbm`` is."""
    import site
    parent = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))      # .../mhbm/mcp/executor.py -> ...
    try:
        installed = {os.path.realpath(p) for p in site.getsitepackages() + [site.getusersitepackages()]}
    except Exception:                                   # embedded / virtualenv builds without site helpers
        installed = set()
    return None if os.path.realpath(parent) in installed else parent


def child_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k in ENV_ALLOW or k.startswith(ENV_PREFIXES)}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    where = _package_parent()
    if where and where not in env.get("PYTHONPATH", "").split(os.pathsep):
        env["PYTHONPATH"] = os.pathsep.join(p for p in (where, env.get("PYTHONPATH", "")) if p)
    env.update(extra or {})
    return env


class SubprocessExecutor:
    def __init__(self, home: str, python: str | None = None, max_stdout: int = 16 << 20, grace: float = 2.0, module: str = "mhbm", read_only: bool = False,
                 no_network: bool = False):
        self.home, self.python, self.max_stdout, self.grace, self.module = home, python or sys.executable, max_stdout, grace, module
        self.read_only, self.no_network = read_only, no_network

    def command(self, argv, quiet: bool) -> list:
        return [self.python, "-X", "utf8", "-m", self.module, "--home", self.home, "--json", *(["--quiet"] if quiet else []), *argv]

    def run(self, argv, *, timeout: float, cancel: threading.Event | None = None, progress=None, env_extra=None, cwd=None) -> ExecResult:
        cancel = cancel or threading.Event()
        t0 = time.monotonic()
        res = ExecResult(argv=list(argv))
        proc = self._spawn(argv, quiet=progress is None, env_extra=env_extra, cwd=cwd)
        chunks: list = []
        tail: list = []
        overflow = threading.Event()
        threads = [threading.Thread(target=self._pump_stdout, args=(proc, chunks, overflow), daemon=True),
                   threading.Thread(target=self._pump_stderr, args=(proc, tail, progress), daemon=True)]
        for t in threads:
            t.start()
        deadline = t0 + timeout
        try:
            while proc.poll() is None:
                if cancel.is_set():
                    res.cancelled = True
                elif time.monotonic() > deadline:
                    res.timed_out = True
                elif overflow.is_set():
                    res.overflow = True
                else:
                    cancel.wait(0.05)
                    continue
                self._stop(proc)
                break
        finally:
            if proc.poll() is None:
                self._stop(proc)
            for t in threads:
                t.join(2.0)
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
        res.code = proc.returncode if proc.returncode is not None else -9
        res.stdout = b"".join(chunks)
        res.stderr = "".join(tail)[-8192:]
        res.elapsed = time.monotonic() - t0
        return res

    def _spawn(self, argv, *, quiet: bool, env_extra, cwd):
        kw: dict = {}
        if os.name == "posix":
            kw["start_new_session"] = True                      # own process group: one signal reaches the whole tree
        else:                                                   # pragma: no cover - Windows
            kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        workdir = cwd or self.home
        extra = dict(env_extra or {})
        if self.read_only:
            extra["MHBM_READONLY"] = "1"
        if self.no_network:
            extra["MHBM_NO_NETWORK"] = "1"
        env = child_env(extra)
        return subprocess.Popen(self.command(argv, quiet), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                cwd=workdir if os.path.isdir(workdir) else None, close_fds=True, **kw)

    def _pump_stdout(self, proc, chunks: list, overflow: threading.Event) -> None:
        size = 0
        for chunk in iter(lambda: proc.stdout.read(65536), b""):
            size += len(chunk)
            if size > self.max_stdout:
                overflow.set()
                return
            chunks.append(chunk)

    @staticmethod
    def _pump_stderr(proc, tail: list, progress) -> None:
        size = 0
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace")
            m = PROGRESS_RE.match(line)
            if m and progress is not None:
                progress(int(m["i"]), int(m["n"]), line.strip())
                continue
            tail.append(line)
            size += len(line)
            while size > 65536 and len(tail) > 1:
                size -= len(tail.pop(0))

    def _stop(self, proc) -> None:
        """SIGINT (clean unwind), then SIGTERM, then SIGKILL, each to the whole process group (Windows: CTRL_BREAK, terminate, kill)."""
        if os.name == "posix":
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except (ProcessLookupError, PermissionError):
                    return
                try:
                    proc.wait(self.grace)
                    return
                except subprocess.TimeoutExpired:
                    continue
            return
        for action in (lambda: proc.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM)), proc.terminate, proc.kill):   # pragma: no cover - Windows
            try:
                action()
                proc.wait(self.grace)
                return
            except subprocess.TimeoutExpired:
                continue
            except OSError:
                return
