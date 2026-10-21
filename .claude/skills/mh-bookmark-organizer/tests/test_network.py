import ipaddress
import socket
import ssl
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import common
from mhbm import archive, extract, linkcheck, netsafe, snapshot
from mhbm.model import Bookmark


def bm(base, path, **kw):
    return Bookmark(url=base + path, id=path.strip("/") or "root", **kw)


class Safety(unittest.TestCase):
    def test_private_targets_blocked(self):
        for url in ["http://127.0.0.1/", "http://169.254.169.254/latest/meta-data", "http://[::1]/", "file:///etc/passwd",
                    "http://10.0.0.1/", "http://192.168.1.1/"]:
            self.assertTrue(netsafe.fetch(url, timeout=2).blocked, url)
            self.assertEqual(linkcheck._probe(Bookmark(url=url), 2, None, 0)[1], "skipped")

    def test_redirect_to_private_is_blocked(self):
        with common.Server(allow_private=False) as s:   # the server is on loopback; with the guard on it must refuse even the first hop
            self.assertTrue(netsafe.fetch(s.base + "/ok", timeout=2).blocked)
        with common.Server() as s:
            self.assertFalse(netsafe.fetch(s.base + "/ok", timeout=2).blocked)
        self.assertTrue(netsafe.fetch("http://127.0.0.1:9/", timeout=2).blocked)           # the exemption ends with the block


class Rebinding(unittest.TestCase):
    """The address that is checked must be the address that is connected to (DNS rebinding)."""

    def test_name_that_turns_private_between_check_and_connect_is_refused(self):
        with common.Server(allow_private=False) as s, common.FakeDNS({"rebind.test": [["93.184.216.34"], ["127.0.0.1"]]}) as dns:
            r = netsafe.fetch(f"http://rebind.test:{s.httpd.server_address[1]}/ok", timeout=3)
            self.assertTrue(r.blocked, r)
            self.assertIn("non-public", r.error)
            self.assertEqual(dns.lookups["rebind.test"], 2)       # once for the early check, once for the connection
            self.assertEqual(s.hits, [])                          # the local server was never touched

    def test_mixed_public_and_private_answer_is_refused(self):
        with common.Server(allow_private=False) as s, common.FakeDNS({"mixed.test": [["93.184.216.34", "127.0.0.1"]]}):
            r = netsafe.fetch(f"http://mixed.test:{s.httpd.server_address[1]}/ok", timeout=3)
            self.assertTrue(r.blocked, r)
            self.assertEqual(s.hits, [])
            with self.assertRaises(netsafe.Blocked):               # and the connect step checks on its own, too
                netsafe._create_public_connection(("mixed.test", s.httpd.server_address[1]), timeout=2)

    def test_the_harness_reaches_a_local_server_only_when_allowed(self):
        with common.Server() as s, common.FakeDNS({"local.test": [["127.0.0.1"]]}):
            r = netsafe.fetch(f"http://local.test:{s.httpd.server_address[1]}/ok", timeout=3)
            self.assertEqual((r.status, r.blocked), (200, False))
            self.assertTrue(s.hits)

    def test_ipv6_forms_of_private_ipv4_are_not_public(self):
        for text in ["::ffff:127.0.0.1", "::ffff:10.0.0.5", "::ffff:169.254.169.254", "2002:7f00:0001::1", "2002:a9fe:a9fe::1",
                     "2001:0:4136:e378:8000:63bf:3fff:fdd2", "::1", "fe80::1", "fc00::1",
                     "64:ff9b::7f00:1", "64:ff9b::a00:1", "64:ff9b::a9fe:a9fe", "::7f00:1", "::a00:1", "::ffff:0:7f00:1", "fec0::1", "::"]:
            self.assertFalse(netsafe.is_public(ipaddress.ip_address(text)), text)
        for text in ["::ffff:8.8.8.8", "2002:0808:0808::1", "8.8.8.8", "2606:4700:4700::1111", "64:ff9b::808:808"]:
            self.assertTrue(netsafe.is_public(ipaddress.ip_address(text)), text)

    def test_ipv4_mapped_literal_url_is_blocked(self):
        with common.Server(allow_private=False) as s:
            port = s.httpd.server_address[1]
            for url in [f"http://[::ffff:127.0.0.1]:{port}/ok", f"http://[::ffff:7f00:1]:{port}/ok", f"http://[2002:7f00:1::]:{port}/ok",
                        f"http://[64:ff9b::7f00:1]:{port}/ok", f"http://[::7f00:1]:{port}/ok"]:
                self.assertTrue(netsafe.fetch(url, timeout=2).blocked, url)
            self.assertEqual(s.hits, [])

    def test_https_still_verifies_the_host_name(self):
        """The socket is opened by us, so the TLS layer must still be told the *name* (not the IP) to check the certificate for."""
        seen = {}

        class Ctx(ssl.SSLContext):          # a real context (every Python version inspects its verify settings); only the handshake is stubbed
            def wrap_socket(self, sock, *args, server_hostname=None, **kwargs):
                seen["server_hostname"] = server_hostname
                sock.close()
                raise OSError("stop here")

        with common.Server() as s, common.FakeDNS({"secure.test": [["127.0.0.1"]]}):
            conn = netsafe._PublicHTTPSConnection("secure.test", s.httpd.server_address[1], timeout=2, context=Ctx(ssl.PROTOCOL_TLS_CLIENT))
            with self.assertRaises(OSError):
                conn.connect()
        self.assertEqual(seen, {"server_hostname": "secure.test"})

    def test_proxied_requests_use_the_plain_connection(self):
        req = urllib.request.Request("http://example.org/")
        self.assertFalse(netsafe._proxied(req))
        req.set_proxy("proxy.example:3128", "http")
        self.assertTrue(netsafe._proxied(req))
        secure = urllib.request.Request("https://example.org/")
        secure.set_proxy("proxy.example:3128", "https")
        self.assertTrue(netsafe._proxied(secure))
        with mock.patch.object(netsafe._PublicHTTPSHandler, "do_open", return_value="ok") as do_open:
            handler = netsafe._PublicHTTPSHandler()
            handler.https_open(secure)
            self.assertIs(do_open.call_args[0][0], netsafe.http.client.HTTPSConnection)   # the proxy resolves names; no local pin
            handler.https_open(urllib.request.Request("https://example.org/"))
            self.assertIs(do_open.call_args[0][0], netsafe._PublicHTTPSConnection)

    def test_dns_failure_at_connect_is_an_error_not_a_block(self):
        with common.proxy_env(), common.FakeDNS({"flaky.test": [["93.184.216.34"]]}):
            with self.assertRaises(OSError):
                netsafe._create_public_connection(("nowhere-at-all.invalid", 80), timeout=2)
        self.assertEqual(socket.getaddrinfo("127.0.0.1", 80, type=socket.SOCK_STREAM)[0][4][0], "127.0.0.1")   # the patch is gone again


class ProxyAware(unittest.TestCase):
    """Behind an HTTP proxy the proxy resolves names; local DNS must not decide a link is dead."""

    def test_unresolvable_name_is_fetched_through_the_proxy(self):
        with common.Server(allow_private=False) as s:  # the proxy itself is on loopback; only targets are validated
            with common.proxy_env(s.base):
                r = netsafe.fetch("http://nowhere-at-all.invalid/ok", timeout=5)
                self.assertEqual((r.status, r.blocked, r.error), (200, False, ""))
                self.assertIn(("GET", "http://nowhere-at-all.invalid/ok"), s.hits)  # the proxy got the absolute URL

    def test_targets_that_are_obviously_local_are_still_refused_via_proxy(self):
        with common.Server(allow_private=False) as s, common.proxy_env(s.base):
            for url in ["http://127.0.0.1/ok", "http://169.254.169.254/latest", "http://[::1]/", "http://10.1.2.3/",
                        "http://printer.local/", "http://intranet/", "http://service.internal/x", "http://localhost.localdomain/"]:
                self.assertTrue(netsafe.fetch(url, timeout=3).blocked, url)
            self.assertEqual([h for h in s.hits if "127.0.0.1/ok" in h[1] or "169.254" in h[1]], [])

    def test_odd_spellings_of_private_addresses_are_refused_via_proxy(self):
        """A proxy resolves ``127.1`` or ``0x7f.0.0.1`` to loopback (on the proxy's network); they must not be taken for host names."""
        with common.Server(allow_private=False) as s, common.proxy_env(s.base):
            for host in ["127.1", "0177.0.0.1", "0x7f.0.0.1", "10.1", "2130706433", "0x7f000001", "0xA9FEA9FE", "0251.0376.0251.0376", "192.168.1"]:
                r = netsafe.fetch(f"http://{host}/ok", timeout=3)
                self.assertTrue(r.blocked, host)
            self.assertEqual(s.hits, [])
            self.assertEqual(str(netsafe._legacy_ipv4("0x7f.1")), "127.0.0.1")
            for name in ["example.com", "1.2.3.4.5", "256.1.1.1", "09.1", "a.b", "0x"]:
                self.assertIsNone(netsafe._legacy_ipv4(name), name)

    def test_without_proxy_unresolvable_names_are_dns_failures(self):
        with common.proxy_env():
            r = netsafe.fetch("http://nowhere-at-all.invalid/", timeout=3)
            self.assertEqual(r.status, 0)
            self.assertIn("DNS", r.error)


class Offline(unittest.TestCase):
    def test_offline_batch_is_detected_not_judged(self):
        res = [(Bookmark(url=f"http://x{i}.invalid"), "suspect", 0, "", "dns") for i in range(12)]
        self.assertTrue(linkcheck.looks_offline(res))
        self.assertFalse(linkcheck.looks_offline(res[:5]))                      # too small to conclude anything
        res[0] = (res[0][0], "ok", 200, "", "")
        res[1] = (res[1][0], "ok", 200, "", "")
        self.assertFalse(linkcheck.looks_offline(res))                           # some links answered: failures are real


class LinkCheck(unittest.TestCase):
    def run_check(self, s, paths, now, **kw):
        bms = [bm(s.base, p) for p in paths]
        return bms, list(linkcheck.check_all(bms, interval=0, now=now, **kw))

    def test_statuses(self):
        with common.Server() as s:
            bms, res = self.run_check(s, ["/ok", "/moved", "/temp", "/forbidden", "/gone", "/flaky"], 1_000_000, retries=0)
            st = {b.id: b.status for b in bms}
            self.assertEqual(st, {"ok": "ok", "moved": "redirect", "temp": "redirect", "forbidden": "restricted",
                                  "gone": "suspect", "flaky": "suspect"})
            new_urls = {b.id: u for b, _, _, u, _ in res}
            self.assertTrue(new_urls["moved"].endswith("/ok"))   # 301 -> permanent, safe to rewrite
            self.assertEqual(new_urls["temp"], "")               # 302 -> never rewrite

    def test_dead_needs_repeated_failures_over_time(self):
        with common.Server() as s:
            b = bm(s.base, "/gone")
            t = 1_000_000
            for now, expect in [(t, "suspect"), (t + 60, "suspect"), (t + 7 * 3600, "dead")]:
                list(linkcheck.check_all([b], interval=0, now=now, retries=0, confirmations=2, min_gap=6 * 3600))
                self.assertEqual(b.status, expect, now)
            self.assertEqual(b.fail_count, 3)

    def test_soft_failures_need_one_more_confirmation(self):
        with common.Server() as s:
            b = bm(s.base, "/flaky")
            for now in (1000, 1000 + 7 * 3600):
                list(linkcheck.check_all([b], interval=0, now=now, retries=0, confirmations=2, min_gap=6 * 3600))
            self.assertEqual(b.status, "suspect")
            list(linkcheck.check_all([b], interval=0, now=1000 + 14 * 3600, retries=0, confirmations=2, min_gap=6 * 3600))
            self.assertEqual(b.status, "dead")

    def test_recovery_resets_failures(self):
        with common.Server() as s:
            b = bm(s.base, "/ok", fail_count=2, first_failed=5, status="suspect")
            list(linkcheck.check_all([b], interval=0, now=100, retries=0))
            self.assertEqual((b.status, b.fail_count, b.first_failed), ("ok", 0, 0))

    def test_dns_failure_is_not_dead_on_first_sight(self):
        b = Bookmark(url="http://nonexistent-host-zzzz.invalid/", id="d")
        list(linkcheck.check_all([b], interval=0, now=10, retries=0, timeout=3))
        self.assertEqual(b.status, "suspect")

    def test_retry_and_per_host_throttle(self):
        with common.Server() as s:
            bms = [bm(s.base, "/ok") for _ in range(1)] + [Bookmark(url=s.base + "/ok", id=f"x{i}") for i in range(6)]
            list(linkcheck.check_all(bms, interval=0.05, per_host=1, workers=6, retries=0))
            self.assertTrue(all(b.status == "ok" for b in bms))

    def test_wayback_lookup(self):
        real = linkcheck.fetch
        try:
            linkcheck.fetch = lambda *a, **k: netsafe.Response(status=200, body=b'{"archived_snapshots":{"closest":{"available":true,"url":"https://web.archive.org/web/1/x"}}}')
            self.assertEqual(linkcheck.wayback_lookup("http://gone.example/x"), "https://web.archive.org/web/1/x")
            linkcheck.fetch = lambda *a, **k: netsafe.Response(status=200, body=b'{"archived_snapshots":{}}')
            self.assertEqual(linkcheck.wayback_lookup("http://gone.example/x"), "")
        finally:
            linkcheck.fetch = real

    def test_classify_table(self):
        c = linkcheck.classify
        self.assertEqual(c(404, "", False, False), ("fail", linkcheck.HARD))
        self.assertEqual(c(403, "", False, False), ("restricted", ""))
        self.assertEqual(c(200, "", False, True), ("redirect", ""))
        self.assertEqual(c(0, "timed out", False, False), ("fail", linkcheck.SOFT))
        self.assertEqual(c(0, "DNS failure for x", False, False), ("fail", linkcheck.HARD))
        self.assertEqual(c(503, "", False, False), ("fail", linkcheck.SOFT))


class Extraction(unittest.TestCase):
    def test_extract_prefers_article_and_drops_chrome(self):
        info = extract.extract(common.PAGE)
        self.assertEqual((info["title"], info["canonical"], info["lang"]), ("Operators Guide", "https://example.com/guide", "en"))
        self.assertIn("controllers reconcile", info["text"])
        for junk in ("Home About", "copyright", "evil"):
            self.assertNotIn(junk, info["text"])

    def test_extract_survives_garbage(self):
        self.assertIn("unclosed text", extract.extract("<<<>>><p>unclosed <b>text")["text"])

    def test_snapshot_is_script_free_and_locked_down(self):
        page = ('<html><head><script>alert(1)</script><link rel="stylesheet" href="http://169.254.169.254/x.css"></head>'
                '<body onload="x()"><a href="javascript:evil()">x</a><iframe src="//evil"></iframe>'
                '<img src="http://169.254.169.254/i.png" onerror="y()"><form action="http://evil"><input></form>'
                '<style>p{background:url(http://evil/a.png)}</style></body></html>')
        out = snapshot.build_snapshot(page, "https://x.com/a")
        low = out.lower()
        for bad in ("<script", "onload", "onerror", "<iframe", "javascript:", "http://evil/a.png", "169.254"):
            self.assertNotIn(bad, low, bad)
        self.assertIn("content-security-policy", low)
        self.assertIn("default-src &#x27;none&#x27;", low)


class Archive(unittest.TestCase):
    def test_archive_page_snapshot_robots(self):
        with common.Server() as s, tempfile.TemporaryDirectory() as d:
            ok, private = bm(s.base, "/ok"), bm(s.base, "/private")
            res = {b.id: (good, err) for b, good, err in archive.archive_all([ok, private], d, interval=0)}
            self.assertTrue(res["ok"][0])
            self.assertEqual(res["private"], (False, "blocked by robots.txt"))
            self.assertIn("controllers reconcile", ok.content)
            self.assertEqual((ok.title, ok.meta["canonical"]), ("Operators Guide", "https://example.com/guide"))
            html = Path(d, ok.archive_path).read_text()
            self.assertIn("data:image/png;base64,", html)       # image inlined
            self.assertIn("color:red", html)                     # stylesheet inlined
            self.assertNotIn("evil.example", html)               # remote url() removed
            self.assertNotIn("<script", html.lower())
            self.assertNotIn(("GET", "/private"), s.hits)        # robots honoured: never requested
            list(archive.archive_all([private], d, interval=0, respect_robots=False))
            self.assertIn("secret", private.content)

    def test_text_only_keeps_no_file(self):
        with common.Server() as s, tempfile.TemporaryDirectory() as d:
            b = bm(s.base, "/ok")
            list(archive.archive_all([b], d, interval=0, keep_snapshot=False))
            self.assertEqual((b.archive_path, bool(b.content)), ("", True))
            self.assertFalse(Path(d, "archive").exists())

    def test_pdf_text_degrades_gracefully(self):
        self.assertEqual(archive.pdf_text(b"not a pdf"), "")


if __name__ == "__main__":
    unittest.main()
