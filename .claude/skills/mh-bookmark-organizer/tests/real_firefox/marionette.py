"""Minimal stdlib Marionette client (protocol v3): enough to run chrome-privileged JS in a headless Firefox,
e.g. to call PlacesUtils / PlacesBackups, and to quit cleanly.  No geckodriver, no selenium."""
from __future__ import annotations

import json
import socket
import time


class MarionetteError(RuntimeError):
    pass


class Marionette:
    def __init__(self, port: int = 2828, host: str = "127.0.0.1", timeout: float = 90):
        self.addr, self.timeout = (host, port), timeout
        self.sock = None
        self._id = 0
        self.buf = b""

    def connect(self, wait: float = 60):
        end = time.time() + wait
        last = None
        while time.time() < end:
            try:
                self.sock = socket.create_connection(self.addr, timeout=self.timeout)
                hello = self._recv()
                if isinstance(hello, dict) and hello.get("marionetteProtocol") == 3:
                    return hello
                last = hello
            except OSError as e:
                last = e
                time.sleep(0.3)
        raise MarionetteError(f"cannot connect to Marionette: {last}")

    def _recv(self):
        while b":" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise MarionetteError("connection closed")
            self.buf += chunk
        head, rest = self.buf.split(b":", 1)
        n = int(head)
        while len(rest) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise MarionetteError("connection closed mid-message")
            rest += chunk
        self.buf = rest[n:]
        return json.loads(rest[:n].decode("utf-8"))

    def command(self, name: str, params: dict | None = None):
        self._id += 1
        body = json.dumps([0, self._id, name, params or {}], ensure_ascii=False).encode("utf-8")
        self.sock.sendall(str(len(body)).encode() + b":" + body)
        while True:
            msg = self._recv()
            if msg[0] == 1 and msg[1] == self._id:
                if msg[2]:
                    raise MarionetteError(f"{name}: {msg[2].get('error')}: {msg[2].get('message')}")
                return msg[3]

    def new_session(self):
        return self.command("WebDriver:NewSession", {"capabilities": {"alwaysMatch": {"acceptInsecureCerts": True}}})

    def chrome(self):
        self.command("Marionette:SetContext", {"value": "chrome"})

    def run(self, script: str, args: list | None = None):
        """Run a *function body* (use ``return`` / ``await``) with chrome privileges; returns its JSON value."""
        body = "return (async () => {\n" + script + "\n})();"
        r = self.command("WebDriver:ExecuteScript", {"script": body, "args": args or [], "sandbox": "system"})
        return r.get("value") if isinstance(r, dict) else r

    def quit(self, flags=("eForceQuit",)):
        """Clean application shutdown (Services.startup.quit): profile closed normally, WAL checkpointed."""
        try:
            self.command("Marionette:Quit", {"flags": list(flags)})
        except (MarionetteError, OSError):
            pass
