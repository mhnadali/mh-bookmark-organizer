"""SSRF-safe, polite HTTP fetching for link checking and archiving.

Every request, including each redirect hop, is validated: only http(s) is
allowed and the host must resolve exclusively to public IP addresses, so a
bookmark file cannot make the tool probe localhost, link-local metadata
endpoints or private networks.

For direct connections the check is made again *inside the connect call*, on the very
addresses that are then connected to (``_create_public_connection``), so a DNS answer
that changes between an early check and the connection (DNS rebinding) cannot send the
request to a private address; TLS still verifies the certificate against the host name.
Through an HTTP proxy the proxy resolves names and its own egress policy applies.

``HostThrottle`` limits concurrency and request rate per host so a library
with many links to one site does not hammer it.
"""
from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

USER_AGENT = "Mozilla/5.0 (compatible; mh-bookmark-organizer/1.2; +https://github.com/mhnadali/mh-bookmark-organizer)"
_allow_private_depth = 0  # >0 only inside ``testing_allow_private()``; deliberately not reachable from the CLI or environment
_allow_lock = threading.Lock()


class Blocked(Exception):
    pass


@contextmanager
def testing_allow_private():
    """TESTS ONLY: let fetches reach loopback/private addresses (a local test server) while the block is open."""
    global _allow_private_depth
    with _allow_lock:
        _allow_private_depth += 1
    try:
        yield
    finally:
        with _allow_lock:
            _allow_private_depth -= 1


def _private_allowed() -> bool:
    return _allow_private_depth > 0


_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".localdomain", ".intranet", ".corp")


def _proxy_for(url: str) -> str:
    """The proxy urllib would use for ``url`` ("" = direct connection)."""
    parts = urlsplit(url)
    if not parts.hostname:
        return ""
    proxy = urllib.request.getproxies().get(parts.scheme, "")
    if not proxy:
        return ""
    try:
        if urllib.request.proxy_bypass(parts.hostname):
            return ""
    except Exception:
        pass
    return proxy


_LEGACY_PART = re.compile(r"0[xX][0-9a-fA-F]*|[0-9]+")


def _legacy_ipv4(host: str):
    """The IPv4 address that ``inet_aton``-style notation names: ``127.1``, ``0177.0.0.1``, ``0x7f.0.0.1``, ``2130706433``, ``0x7f000001``.

    Browsers, curl and most proxies resolve these to the numeric address; ``ipaddress`` does not recognise them, so without this a
    proxy would be handed ``127.1`` as if it were a name. Returns None when ``host`` is an ordinary name."""
    parts = host.rstrip(".").split(".")
    if not 1 <= len(parts) <= 4 or not all(_LEGACY_PART.fullmatch(p) for p in parts):
        return None
    try:
        numbers = [int(p, 16) if p[:2] in ("0x", "0X") else int(p, 8) if len(p) > 1 and p[0] == "0" else int(p) for p in parts]
    except ValueError:      # "0x" with no digits, or an octal part containing 8 or 9
        return None
    *lead, last = numbers
    if any(n > 255 for n in lead) or last >= 256 ** (4 - len(lead)):
        return None
    value = last
    for i, n in enumerate(lead):
        value |= n << (8 * (3 - i))
    return ipaddress.IPv4Address(value)


def _literal_ip(host: str):
    try:
        return ipaddress.ip_address(host.strip("[]").split("%")[0])
    except ValueError:
        return _legacy_ipv4(host)


# IPv6 ranges that carry an IPv4 address in their last 32 bits: NAT64 (``64:ff9b::/96``), the deprecated IPv4-compatible
# form (``::/96``) and SIIT (``::ffff:0:0:0/96``). A gateway turns them into a connection to that IPv4 address.
_V4_IN_V6 = tuple(ipaddress.ip_network(n) for n in ("64:ff9b::/96", "::/96", "::ffff:0:0:0/96"))
_SITE_LOCAL = ipaddress.ip_network("fec0::/10")     # deprecated private range that Python still calls global


def is_public(addr) -> bool:
    """True when ``addr`` (an ``ipaddress`` object) is an ordinary public address.

    IPv4 addresses hidden inside IPv6 (``::ffff:127.0.0.1``, 6to4 ``2002::/16``, Teredo, NAT64, IPv4-compatible and SIIT forms)
    are judged by the IPv4 address."""
    if addr.version == 6:
        embedded = addr.ipv4_mapped or addr.sixtofour or (addr.teredo[1] if addr.teredo else None)
        if embedded is None and any(addr in net for net in _V4_IN_V6):
            embedded = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
        if embedded is not None:
            return is_public(embedded)
        if addr in _SITE_LOCAL:
            return False
    return addr.is_global


def _create_public_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    """``socket.create_connection`` that resolves once, refuses any non-public answer, and connects to what it checked."""
    host, port = address
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not _private_allowed():
        for info in infos:
            addr = ipaddress.ip_address(str(info[4][0]).split("%")[0])
            if not is_public(addr):
                raise Blocked(f"{host} resolves to non-public address {addr}")
    error = OSError(f"no address for {host}")
    for family, kind, proto, _canon, target in infos:
        sock = None
        try:
            sock = socket.socket(family, kind, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(target)
            return sock
        except OSError as e:
            error = e
            if sock is not None:
                sock.close()
    raise error


def _proxied(req) -> bool:
    return req.has_proxy() or bool(getattr(req, "_tunnel_host", None))


class _PublicHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _create_public_connection


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _create_public_connection  # the TLS layer still checks the certificate against the host name


class _PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(http.client.HTTPConnection if _proxied(req) else _PublicHTTPConnection, req)


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(http.client.HTTPSConnection if _proxied(req) else _PublicHTTPSConnection, req, context=self._context)


def assert_public(url: str) -> None:
    """Refuse non-web schemes and private/loopback/link-local targets.

    Direct connections: the host is resolved here and every address must be public.
    Through an HTTP(S) proxy the proxy resolves names, so they cannot be checked locally; IP literals and
    well-known local names are still refused and the proxy's own egress policy applies to the rest.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Blocked(f"unsupported URL: {url[:80]}")
    host = parts.hostname.lower().rstrip(".")
    if "%" in host and not _private_allowed():     # %31%32%37.0.0.1 is 127.0.0.1 to a proxy or to curl; no public host name contains an escape
        raise Blocked(f"{host[:80]} contains percent-escapes")
    ip = _literal_ip(host)
    if ip is not None and not is_public(ip) and not _private_allowed():
        raise Blocked(f"{host} is a non-public address")
    if _proxy_for(url) and ip is None:
        if (host == "localhost" or host.endswith(_LOCAL_SUFFIXES) or "." not in host) and not _private_allowed():
            raise Blocked(f"{host} looks like a local name")
        return
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise OSError(f"DNS failure for {host}: {e}") from e
    if _private_allowed():
        return
    for info in infos:
        addr = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        if not is_public(addr):
            raise Blocked(f"{host} resolves to non-public address {addr}")


class HostThrottle:
    """At most ``per_host`` concurrent requests and one request per ``interval`` seconds for each host."""

    def __init__(self, per_host: int = 2, interval: float = 0.4):
        self.per_host, self.interval = max(1, per_host), max(0.0, interval)
        self._lock = threading.Lock()
        self._sems: dict = {}
        self._next: dict = {}

    @contextmanager
    def slot(self, url: str):
        host = (urlsplit(url).hostname or "").lower()
        with self._lock:
            sem = self._sems.setdefault(host, threading.Semaphore(self.per_host))
        with sem:
            with self._lock:
                now = time.monotonic()
                start = max(now, self._next.get(host, 0.0))
                self._next[host] = start + self.interval
            if start > now:
                time.sleep(start - now)
            yield

    def penalize(self, url: str, seconds: float) -> None:
        """Push back all further requests to this host (e.g. after HTTP 429)."""
        host = (urlsplit(url).hostname or "").lower()
        with self._lock:
            self._next[host] = max(self._next.get(host, 0.0), time.monotonic() + min(seconds, 30))


@dataclass
class Response:
    status: int = 0
    final_url: str = ""
    content_type: str = ""
    body: bytes = b""
    permanent_redirect: bool = False
    error: str = ""
    blocked: bool = False
    retry_after: float = 0.0
    chain: list = field(default_factory=list)


class _Redirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 8

    def __init__(self, resp: Response):
        self.resp = resp

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        assert_public(newurl)
        self.resp.chain.append(code)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _retry_after(headers) -> float:
    try:
        return max(0.0, float(headers.get("Retry-After", "")))
    except (TypeError, ValueError):
        return 0.0


def fetch(url: str, *, method: str = "GET", timeout: float = 10, max_bytes: int = 2_000_000,
          throttle: HostThrottle | None = None) -> Response:
    resp = Response(final_url=url)
    try:
        assert_public(url)
        opener = urllib.request.build_opener(_Redirects(resp), _PublicHTTPHandler, _PublicHTTPSHandler)
        req = urllib.request.Request(url, method=method, headers={
            "User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
        ctx = throttle.slot(url) if throttle else _null()
        with ctx:
            with opener.open(req, timeout=timeout) as r:
                resp.status = r.status
                resp.final_url = r.geturl()
                resp.content_type = r.headers.get("Content-Type", "")
                if method == "GET":
                    resp.body = r.read(max_bytes)
    except urllib.error.HTTPError as e:
        resp.status, resp.final_url = e.code, e.geturl() or url
        resp.content_type = e.headers.get("Content-Type", "") if e.headers else ""
        resp.retry_after = _retry_after(e.headers) if e.headers else 0.0
        if e.code == 429 and throttle:
            throttle.penalize(url, resp.retry_after or 5)
    except Blocked as e:
        resp.blocked, resp.error = True, str(e)
    except (urllib.error.URLError, OSError, ValueError) as e:
        resp.error = str(getattr(e, "reason", e))
    # a chain made only of 301/308 hops means the move is permanent
    resp.permanent_redirect = bool(resp.chain) and all(c in (301, 308) for c in resp.chain)
    return resp


@contextmanager
def _null():
    yield
