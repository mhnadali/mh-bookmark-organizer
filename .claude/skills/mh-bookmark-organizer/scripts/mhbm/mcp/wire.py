"""Newline-delimited JSON over the process's real stdin/stdout, built so that nothing else can corrupt the protocol stream.

``Wire(claim=True)`` (the normal case) *claims* the standard descriptors, the way the reference Python SDK does:

* the protocol is spoken on private duplicates of fd 0 and fd 1,
* fd 0 is then pointed at the null device (a stray ``input()`` or a child process reads EOF instead of stealing a request),
* fd 1 is pointed at stderr (a stray ``print()``, a C library or a child process writing to "stdout" lands in the log, not on the wire).

Reads are bounded (``max_line`` bytes per message, the excess is discarded and reported), UTF-8 is strict, and every outgoing message is
one ASCII-only line (``ensure_ascii``).  Messages are queued and written by ONE writer thread, so concurrent workers never interleave
bytes and a client that stops reading cannot freeze the reader (the classic two-sided pipe deadlock); progress notifications are dropped
when the queue is long, answers never are.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading

EOF = "eof"
LINE = "line"
OVERSIZE = "oversize"


class WireClosed(Exception):
    """The peer went away (broken pipe)."""


class Wire:
    def __init__(self, stdin=None, stdout=None, max_line: int = 4 << 20, claim: bool = True):
        self.max_line = max_line
        self._q: queue.Queue = queue.Queue()
        self.closed = False
        self.claimed = False
        if stdin is not None or stdout is not None:            # tests inject pipes / BytesIO; nothing is claimed
            self._in, self._out = stdin, stdout
        else:
            self._in, self._out = sys.stdin.buffer, sys.stdout.buffer
            if claim:
                self._claim()
        self._writer = threading.Thread(target=self._drain, name="wire-writer", daemon=True)
        self._writer.start()

    def _claim(self) -> None:
        try:
            rfd, wfd = os.dup(0), os.dup(1)
        except OSError:
            return                                             # cannot duplicate: serve the std streams in place
        try:
            if sys.platform == "win32":                         # pragma: no cover - not exercised in the Linux scout
                import msvcrt
                msvcrt.setmode(rfd, os.O_BINARY)
                msvcrt.setmode(wfd, os.O_BINARY)
            devnull = os.open(os.devnull, os.O_RDONLY)
            os.dup2(devnull, 0)
            os.close(devnull)
            os.dup2(2, 1)
        except OSError:
            os.close(rfd)
            os.close(wfd)
            return
        self._in = os.fdopen(rfd, "rb", buffering=1 << 16)
        self._out = os.fdopen(wfd, "wb", buffering=0)
        self.claimed = True

    # ----------------------------------------------------------------- input
    def read(self):
        """Return ``(LINE, bytes)``, ``(OVERSIZE, size)`` or ``(EOF, None)``."""
        line = self._in.readline(self.max_line + 1)
        if not line:
            return EOF, None
        if len(line) > self.max_line and not line.endswith(b"\n"):
            size = len(line)
            while True:                                         # discard the rest of the oversized message
                chunk = self._in.readline(1 << 20)
                size += len(chunk)
                if not chunk or chunk.endswith(b"\n"):
                    break
            return OVERSIZE, size
        return LINE, line

    # ---------------------------------------------------------------- output
    @staticmethod
    def encode(message) -> bytes:
        return json.dumps(message, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii") + b"\n"

    def send(self, message) -> None:
        """Queue ``message`` (a dict or list) as one line; raises ``WireClosed`` once the peer is gone."""
        data = self.encode(message)
        if self.closed:
            raise WireClosed()
        self._q.put(data)

    def send_droppable(self, message) -> None:
        """For progress notifications: skipped when the peer is slow (more than 200 messages waiting)."""
        if self._q.qsize() <= 200:
            self.send(message)

    def _drain(self) -> None:
        while True:
            data = self._q.get()
            if data is None:
                return
            if self.closed:
                continue
            try:
                view = memoryview(data)
                while view:
                    n = self._out.write(view)
                    view = view[(n if n is not None else len(view)):]
                flush = getattr(self._out, "flush", None)
                if flush:
                    flush()
            except (BrokenPipeError, OSError, ValueError):
                self.closed = True

    def close(self, timeout: float = 5.0) -> None:
        """Write what is queued, then stop the writer."""
        self._q.put(None)
        self._writer.join(timeout)
