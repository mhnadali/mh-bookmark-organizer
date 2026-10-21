#!/usr/bin/env python3
"""Generate synthetic raw http(s) URLs of the kinds found in real exports (unencoded Unicode, spaces, odd case, default ports,
dot segments, mixed-case percent escapes, stray whitespace ...).  usage: fuzz_urls.py N SEED > urls.json"""
import json
import random
import sys

n, seed = int(sys.argv[1]), int(sys.argv[2])
r = random.Random(seed)
SCHEMES = ["http", "https", "HTTP", "Https", "HTTPS"]
LABELS = ["example", "Example", "EXAMPLE", "münchen", "bücher", "exämple", "日本語", "تست", "shop", "a-b", "x1", "www"]
TLDS = ["com", "org", "net", "COM", "io", "de", "ir"]
SEGS = ["docs", "Docs", "a b", "a  b", "ü", "straße", "تحلیل", "صفحه اول", "日本語", "%41", "%7e", "%7E", "%2F", "%2f", "%20", "%3F", "%25", "%C3%BC", "%c3%bc",
        "a|b", "a^b", "a`b", "a{b}", "a\"b", "a<b>", "a'b", "a(b)", "a,b", "a;b", "a=b", "a&b", "a@b", "a:b", "a$b", "a!b", "a*b", "a+b", "a~b", "a_b", "a.b",
        ".", "..", "%2e", "%2E%2e", "...", "​zw", " nbsp", "emoji😀", "‮RTL", "a\\b", "", "index.html", "v1.0", "100%"]
KEYS = ["q", "utm_source", "id", "tag", "Q", "lang", "ref", "x y", "ü", "کلید"]
VALS = ["1", "a b", "a+b", "a%20b", "ü", "سلام", "x=y", "a&b", "", "%41", "%zz", "日本", "😀", "a%2Bb", "a;b", "[1]", "{x}"]
FRAG = ["", "#", "#top", "#/route/x", "#!/bang", "#a b", "#ü", "#تست", "#%41", "#x?y"]


def host():
    h = ".".join(r.choice(LABELS) for _ in range(r.choice([1, 2, 2])) ) + "." + r.choice(TLDS)
    if r.random() < 0.1:
        h += "."
    if r.random() < 0.2:
        h += ":" + r.choice(["80", "443", "8080", "8443", ""])
    return h


def one():
    s = r.choice(SCHEMES) + "://"
    if r.random() < 0.05:
        s += "user:pw@"
    s += host()
    segs = [r.choice(SEGS) for _ in range(r.choice([0, 0, 1, 2, 3]))]
    path = ("/" + "/".join(segs)) if segs else r.choice(["", "/"])
    if segs and r.random() < 0.2:
        path += "/"
    s += path
    if r.random() < 0.5:
        s += "?" + "&".join(f"{r.choice(KEYS)}={r.choice(VALS)}" if r.random() < 0.9 else r.choice(KEYS) for _ in range(r.choice([0, 1, 2, 3])))
    s += r.choice(FRAG)
    if r.random() < 0.08:
        s = " " * r.choice([1, 2]) + s + " " * r.choice([0, 1, 3])
    if r.random() < 0.04:
        i = r.randrange(len(s))
        s = s[:i] + r.choice(["\t", "\n", "\r"]) + s[i:]
    return s


seen, out = set(), []
while len(out) < n:
    u = one()
    if u not in seen:
        seen.add(u)
        out.append(u)
json.dump(out, sys.stdout, ensure_ascii=False)
