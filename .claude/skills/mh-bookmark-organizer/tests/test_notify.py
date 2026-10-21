import http.server
import json
import os
import socket
import threading
import unittest
from unittest import mock

import common
from mhbm import notify
from test_cli import CliCase


class FakeSMTP:
    """Just enough SMTP (no TLS, no auth) to capture one message."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.messages = []
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn, conn.makefile("rwb") as f:
                def send(line):
                    f.write((line + "\r\n").encode())
                    f.flush()
                send("220 fake")
                data, in_data = [], False
                while True:
                    line = f.readline()
                    if not line:
                        break
                    text = line.decode().rstrip("\r\n")
                    if in_data:
                        if text == ".":
                            in_data = False
                            self.messages.append("\n".join(data))
                            send("250 queued")
                        else:
                            data.append(text)
                    elif text.upper().startswith("EHLO"):
                        send("250 fake")
                    elif text.upper().startswith(("MAIL", "RCPT")):
                        send("250 ok")
                    elif text.upper() == "DATA":
                        in_data = True
                        send("354 go")
                    elif text.upper() == "QUIT":
                        send("221 bye")
                        break
                    else:
                        send("250 ok")

    def close(self):
        self.sock.close()


class Hook(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        Hook.seen.append((self.path, dict(self.headers), self.rfile.read(n)))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


class Providers(unittest.TestCase):
    def test_when_policy(self):
        quiet = {"checked": 5, "dead": 0, "suspect": 0, "proposals": 0, "duplicates": 0}
        self.assertFalse(notify.should_send("problems", quiet))
        self.assertTrue(notify.should_send("always", quiet))
        self.assertFalse(notify.should_send("changes", quiet))
        self.assertTrue(notify.should_send("changes", {**quiet, "proposals": 2}))
        self.assertTrue(notify.should_send("problems", {**quiet, "dead": 1}))
        self.assertTrue(notify.should_send("problems", {**quiet, "offline": True}))

    def test_message_text(self):
        subject, body = notify.message({"dead": 2, "proposals": 5, "checked": 40, "report": "/r.md"})
        self.assertEqual(subject, "mhbm: 2 dead links, 5 filing proposals")
        self.assertIn("links checked: 40", body)
        self.assertEqual(notify.message({})[0], "mhbm: all quiet")

    def test_email_through_a_real_smtp_conversation(self):
        smtp = FakeSMTP()
        self.addCleanup(smtp.close)
        cfg = {"on": ["email"], "when": "always", "email": {"host": "127.0.0.1", "port": smtp.port, "starttls": False,
                                                           "from": "bm@example.com", "to": ["me@example.com"]}}
        out = notify.send({"dead": 1, "checked": 3}, cfg)
        self.assertEqual([(n, ok) for n, ok, _ in out], [("email", True)])
        self.assertIn("Subject: mhbm: 1 dead links", smtp.messages[0])
        self.assertIn("To: me@example.com", smtp.messages[0])

    def test_email_login_needs_the_password_from_the_environment_not_the_config(self):
        smtp = FakeSMTP()
        self.addCleanup(smtp.close)
        cfg = {"on": ["email"], "when": "always", "email": {"host": "127.0.0.1", "port": smtp.port, "starttls": False,
                                                           "from": "a@b.c", "to": "d@e.f", "username": "u", "password": "NOT-READ-FROM-CONFIG"}}
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MHBM_SMTP_PASSWORD", None)
            out = notify.send({"dead": 1}, cfg)
        self.assertFalse(out[0][1])
        self.assertIn("MHBM_SMTP_PASSWORD", out[0][2])

    def test_webhook_formats(self):
        Hook.seen = []
        srv = http.server.HTTPServer(("127.0.0.1", 0), Hook)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        url = f"http://127.0.0.1:{srv.server_address[1]}/hook"
        for fmt, key in (("slack", "text"), ("discord", "content"), ("json", "summary")):
            with mock.patch.dict(os.environ, {"MHBM_WEBHOOK_URL": url}):
                out = notify.send({"dead": 2}, {"on": ["webhook"], "when": "always", "webhook": {"format": fmt}})
            self.assertTrue(out[0][1], out)
            self.assertIn(key, json.loads(Hook.seen[-1][2]))
        with mock.patch.dict(os.environ, {"MHBM_WEBHOOK_URL": url}):
            notify.send({"dead": 2}, {"on": ["webhook"], "when": "always", "webhook": {"format": "ntfy"}})
        self.assertIn("2 dead links", Hook.seen[-1][2].decode())
        self.assertIn("Title", Hook.seen[-1][1])
        with mock.patch.dict(os.environ, {"MHBM_WEBHOOK_URL": "file:///etc/passwd"}):
            self.assertFalse(notify.send({"dead": 2}, {"on": ["webhook"], "when": "always"})[0][1])

    def test_desktop_uses_argument_lists_never_a_shell(self):
        with mock.patch("platform.system", return_value="Linux"), mock.patch("shutil.which", return_value="/usr/bin/notify-send"), \
                mock.patch("subprocess.run") as run:
            out = notify.send({"dead": 1, "checked": 9}, {"on": ["desktop"], "when": "always"})
        self.assertTrue(out[0][1])
        cmd = run.call_args[0][0]
        self.assertIsInstance(cmd, list)
        self.assertEqual(cmd[0], "notify-send")
        with mock.patch("platform.system", return_value="Linux"), mock.patch("shutil.which", return_value=None):
            self.assertFalse(notify.send({"dead": 1}, {"on": ["desktop"], "when": "always"})[0][1])

    def test_bad_configuration_is_reported(self):
        with self.assertRaises(notify.NotifyError):
            notify.send({}, {"on": ["pigeon"]})
        with self.assertRaises(notify.NotifyError):
            notify.send({}, {"on": ["desktop"], "when": "sometimes"})
        self.assertEqual(notify.send({}, {}), [])
        self.assertTrue(notify.send({"dead": 0}, {"on": ["desktop"], "when": "problems"})[0][1])   # quiet run: nothing sent, no error

    def test_one_failing_provider_does_not_stop_the_others(self):
        smtp = FakeSMTP()
        self.addCleanup(smtp.close)
        cfg = {"on": ["webhook", "email"], "when": "always", "email": {"host": "127.0.0.1", "port": smtp.port, "starttls": False, "from": "a@b.c", "to": "d@e.f"}}
        with mock.patch.dict(os.environ, {"MHBM_WEBHOOK_URL": "http://127.0.0.1:1/none"}):
            out = notify.send({"dead": 1}, cfg)
        self.assertEqual([(n, ok) for n, ok, _ in out], [("webhook", False), ("email", True)])


class Cli(CliCase):
    def test_notify_command_and_maintain_flag(self):
        from pathlib import Path
        smtp = FakeSMTP()
        self.addCleanup(smtp.close)
        Path(self.home).mkdir(parents=True)
        Path(self.home, "config.json").write_text(json.dumps({"notify": {"on": ["email"], "when": "always", "email": {
            "host": "127.0.0.1", "port": smtp.port, "starttls": False, "from": "a@b.c", "to": ["d@e.f"]}}}))
        self.run_cli("notify")
        self.assertEqual(len(smtp.messages), 1)
        self.run_cli("import", self.write("in.csv", "url\nhttp://nonexistent-host-xyz.invalid/\n"))
        with common.proxy_env():
            self.run_cli("maintain", "--notify", "--check-limit", "5", "--archive-limit", "0")
        self.assertEqual(len(smtp.messages), 2)
        self.assertIn("mhbm:", smtp.messages[1])

    def test_notify_without_configuration_explains_itself(self):
        with self.assertRaises(SystemExit):
            self.run_cli("notify")


if __name__ == "__main__":
    unittest.main()
