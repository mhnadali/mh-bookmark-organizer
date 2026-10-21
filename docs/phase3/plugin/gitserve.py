#!/usr/bin/env python3
"""Tiny read-only smart-HTTP git server on 127.0.0.1 for local experiments: gitserve.py ROOT PORT (ROOT holds bare repos)."""
import os, subprocess, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT, PORT = os.path.abspath(sys.argv[1]), int(sys.argv[2])

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    def _cgi(self):
        u = urlsplit(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        env = {"PATH": os.environ["PATH"], "GIT_PROJECT_ROOT": ROOT, "GIT_HTTP_EXPORT_ALL": "1", "REQUEST_METHOD": self.command,
               "PATH_INFO": u.path, "QUERY_STRING": u.query, "CONTENT_TYPE": self.headers.get("Content-Type", ""),
               "CONTENT_LENGTH": str(len(body)), "REMOTE_ADDR": "127.0.0.1", "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
               "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", "")}
        p = subprocess.run([os.path.join(subprocess.check_output(["git", "--exec-path"]).decode().strip(), "git-http-backend")],
                           input=body, env=env, capture_output=True)
        head, _, payload = p.stdout.partition(b"\r\n\r\n")
        status, headers = 200, []
        for line in head.decode("latin1").split("\r\n"):
            k, _, v = line.partition(": ")
            if k.lower() == "status": status = int(v.split()[0])
            elif k: headers.append((k, v))
        self.send_response(status)
        for k, v in headers: self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers(); self.wfile.write(payload)
    do_GET = do_POST = _cgi
    def log_message(self, fmt, *a):
        sys.stderr.write("git-http %s\n" % (fmt % a))

ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
