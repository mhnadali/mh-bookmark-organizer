import collections
import contextlib
import http.server
import os
import socket
import sys
import threading
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
os.environ["MHBM_LANG"] = "en"  # tests assert on the English messages
os.environ.setdefault("MHBM_BIDI", "off")

from mhbm import netsafe

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82")
PAGE = ("<html lang='en'><head><title>Operators Guide</title><link rel='canonical' href='https://example.com/guide'>"
        "<link rel='stylesheet' href='/s.css'><meta name='description' content='Kubernetes operators explained'>"
        "<script>window.evil=1</script></head><body onload='x()'><nav>Home About</nav><article><h1>Guide</h1>"
        "<p>" + "controllers reconcile desired state. " * 12 + "</p><img src='/i.png'></article>"
        "<footer>copyright</footer></body></html>")


class _Handler(http.server.BaseHTTPRequestHandler):
    hits: list = []

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="text/html; charset=utf-8", headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _route(self):
        _Handler.hits.append((self.command, self.path))
        p = self.path
        if p.startswith("http"):  # absolute-form request line, as sent to an HTTP proxy
            sp = urlsplit(p)
            p = sp.path + (("?" + sp.query) if sp.query else "")
        if p == "/ok":
            return self._send(200, PAGE.encode())
        if p == "/gone":
            return self._send(404)
        if p == "/flaky":
            return self._send(500)
        if p == "/forbidden":
            return self._send(403)
        if p == "/moved":
            return self._send(301, headers=[("Location", "/ok")])
        if p == "/temp":
            return self._send(302, headers=[("Location", "/ok")])
        if p == "/private":
            return self._send(200, b"<html><body>secret</body></html>")
        if p == "/robots.txt":
            return self._send(200, b"User-agent: *\nDisallow: /private\n", "text/plain")
        if p == "/s.css":
            return self._send(200, b"body{color:red}.x{background:url(http://evil.example/x.png)}", "text/css")
        if p == "/i.png":
            return self._send(200, PNG, "image/png")
        return self._send(404)

    do_GET = do_HEAD = _route


class Server:
    """Local HTTP server; the SSRF guard is relaxed for it (and only for it) via netsafe.testing_allow_private()."""

    def __init__(self, allow_private=True):
        self.allow_private = allow_private

    def __enter__(self):
        self._stack = contextlib.ExitStack()
        if self.allow_private:
            self._stack.enter_context(netsafe.testing_allow_private())
        _Handler.hits = []
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.hits = _Handler.hits
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._stack.close()


PROXY_VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY")


@contextlib.contextmanager
def proxy_env(proxy=""):
    """Run with a known proxy configuration (none by default), whatever the machine's environment says."""
    saved = {k: os.environ.pop(k) for k in PROXY_VARS if k in os.environ}
    if proxy:
        os.environ["http_proxy"] = os.environ["https_proxy"] = proxy
    try:
        yield
    finally:
        for k in ("http_proxy", "https_proxy"):
            os.environ.pop(k, None)
        os.environ.update(saved)


class FakeDNS:
    """Scripted name resolution. ``script`` maps a host name to a list of answers (each a list of IPs), one per lookup;
    the last answer repeats. Other names resolve normally. ``lookups`` counts how often each scripted name was asked."""

    def __init__(self, script):
        self.script = script
        self.lookups = collections.Counter()

    def __enter__(self):
        real = socket.getaddrinfo

        def fake(host, port, *args, **kwargs):
            if host not in self.script:
                return real(host, port, *args, **kwargs)
            answers = self.script[host]
            ips = answers[min(self.lookups[host], len(answers) - 1)]
            self.lookups[host] += 1
            port = int(port or 0)
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, port, 0, 0)) if ":" in ip
                    else (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

        self._patch = mock.patch.object(socket, "getaddrinfo", fake)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
