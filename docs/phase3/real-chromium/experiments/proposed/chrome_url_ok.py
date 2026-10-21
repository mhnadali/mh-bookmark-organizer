"""PROTOTYPE of ``formats.chrome_url_ok`` (to be merged into mhbm/formats.py).

True if Chromium will keep a bookmark with this url string when it loads a Bookmarks file; False if it silently drops the node
(observed on Chromium 141 and Brave 155 with exp_urlvalid.py).  Deliberately conservative in one direction only: it may say
"ok" for a string the browser drops (rare corner cases), but it should never say "bad" for a normal URL.
"""
import re

_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_SPECIAL = ("http", "https", "ftp", "ws", "wss", "chrome", "chrome-extension", "chrome-untrusted", "devtools")   # schemes that need a host
_CTL = re.compile(r"[\x00-\x20]+")


def chrome_url_ok(url) -> bool:
    if not isinstance(url, str):
        return False
    s = re.sub(r"[\t\r\n]", "", url).strip("\x00 \x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f")
    m = _SCHEME.match(s)
    if not m:
        return False                       # "example.com", "//x", "/path", "?q", "#f", "git@host:a/b" ...
    scheme = m.group(0)[:-1].lower()
    if scheme not in _SPECIAL:
        return True                        # javascript:, data:, file:, about:, mailto:, chrome://x, localhost:8080 (a "scheme") ...
    rest = s[m.end():].replace("\\", "/")
    rest = rest.lstrip("/")                # "http:example.com", "http:/x", "http:///x" are all repaired by the parser
    authority = re.split(r"[/?#]", rest, 1)[0]
    userinfo, _, hostport = authority.rpartition("@")
    if authority.endswith("@") or not hostport:
        return False                       # "http://", "http://user@/", "http://:80/"
    if hostport.startswith("["):
        if "]" not in hostport:
            return False                   # "http://[::1"
        host, port = hostport[1:].split("]", 1)
        if not re.fullmatch(r"[0-9A-Fa-f:.]+", host):
            return False
        port = port[1:] if port.startswith(":") else port
    else:
        host, _, port = hostport.partition(":")
    if port and (not port.isdigit() or int(port) > 65535):
        return False                       # "http://h:abc/", "http://h:99999/"
    if not host:
        return False
    if re.search(r"[<>^|]", host) or re.search(r"%(?![0-9A-Fa-f]{2})", host):
        return False                       # "http://exa<mple.com/", "http://exa%mple.com/"
    if re.fullmatch(r"(?=.*[0-9])[0-9.]+", host):     # all-numeric dotted host must be a valid (legacy) IPv4 address
        parts = host.rstrip(".").split(".")
        if len(parts) > 4 or any(not p for p in parts) or (len(parts) == 4 and any(int(p) > 255 for p in parts)):
            return False
    return True
