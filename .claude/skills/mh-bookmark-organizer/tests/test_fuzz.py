"""Seeded randomised tests: parsers, normalisers and round-trips must survive hostile and malformed input.

Everything here is deterministic (fixed seeds, stdlib ``random``) so a failure reproduces; the failing input is
included in the assertion message.
"""
import json
import random
import re
import tempfile
import unittest

import common  # noqa: F401
from mhbm import browsers, categorize, formats, model, textnorm, urlnorm
from mhbm.model import Bookmark, esc_name, unesc_name
from mhbm.store import Library

NASTY = ['"', "'", "<", ">", "&", "&amp;", "&#x41;", "</a>", "<script>", "%", "%00", "\\", "=", "@", "+", "-", "#", "?", ";", ":", "|", "`",
         "‏", "‮", "العربية", "فارسی", "کتاب", "😀", "é", "ü", "ß", "İ", " ", "\t"]
WORDS = ["github", "python", "recipe", "news", "docs", "api", "blog", "video", "کتاب", "نان", "guide", "tips", "2024", "x"]


def text(rnd, n=6, nasty=True):
    parts = []
    for _ in range(rnd.randint(0, n)):
        parts.append(rnd.choice(NASTY) if nasty and rnd.random() < 0.35 else rnd.choice(WORDS))
    return rnd.choice([" ", "", "-", "_"]).join(parts)


def junk_url(rnd):
    pieces = ["http://", "https://", "ftp://", "javascript:", "//", "", "HTTP://", "chrome://"]
    host = rnd.choice(["example.com", "WWW.Example.COM.", "[::1]", "[::1", "a b.com", "xn--p1ai.com", "127.0.0.1:99999", "user:pw@host.com",
                       "", ":::", "例え.jp", "a..b", "-.com", "host:80", "host:abc"])
    path = rnd.choice(["", "/", "/a/b/", "/a%20b", "/a b", "/%zz", "/a?x=1", "/a#frag", "//", "/./../"])
    query = rnd.choice(["", "?utm_source=a", "?a=1&utm_medium=b&c=2", "?&&", "?=", "?a", "?a=1&a=2", "?fbclid=x&id=9", "?q=%E0%A4%A"])
    frag = rnd.choice(["", "#", "#top", "#a=b"])
    return rnd.choice(pieces) + host + path + query + frag


def real_url(rnd):
    return f"https://{rnd.choice(['a', 'b', 'docs', 'www'])}.{rnd.choice(['example.com', 'site.org', 'x.io'])}/{rnd.randint(0, 9999)}/{rnd.choice(WORDS)}"


def rand_json(rnd, depth=0):
    kinds = ["null", "bool", "int", "float", "str", "list", "dict"] if depth < 4 else ["null", "bool", "int", "str"]
    k = rnd.choice(kinds)
    if k == "null":
        return None
    if k == "bool":
        return rnd.random() < 0.5
    if k == "int":
        return rnd.choice([0, 1, -1, 7, 10**6, 10**12, 10**15, 10**18, 13_000_000_000_000_000, 1_700_000_000])
    if k == "float":
        return rnd.choice([0.5, -2.5, 1e18, 1.7e9])
    if k == "str":
        return text(rnd, 3)
    if k == "list":
        return [rand_json(rnd, depth + 1) for _ in range(rnd.randint(0, 4))]
    keys = ["url", "name", "title", "type", "children", "roots", "bookmarks", "folder", "tags", "uri", "typeCode", "id", "parentId",
            "collections", "links", "date_added", "dateAdded", "added", "notes", "root", "content", "labels", "guid"]
    return {rnd.choice(keys): rand_json(rnd, depth + 1) for _ in range(rnd.randint(0, 5))}


def bookmarks(rnd, n):
    out, seen = [], set()
    folders = ["", "Bookmarks bar", "Bookmarks bar/Dev", "Other bookmarks/Deep/Er/Folder", "Reading list", "غذا/نان", "A B  ", " lead", "Weird & <Name>"]
    while len(out) < n:
        b = Bookmark(url=real_url(rnd), title=text(rnd).strip(), folder=rnd.choice(folders), tags=sorted({rnd.choice(WORDS) for _ in range(rnd.randint(0, 3))}),
                     notes=text(rnd, 4).strip(), added=rnd.choice([0, 1_500_000_000, 1_700_000_000 + rnd.randint(0, 10**6)]))
        if (b.url, b.folder) not in seen:
            seen.add((b.url, b.folder))
            out.append(b)
    model.assign_ids(out, set())
    return out


def mutate(rnd, s, n=6):
    s = list(s)
    for _ in range(n):
        if not s:
            break
        i = rnd.randrange(len(s))
        op = rnd.random()
        if op < 0.4:
            del s[i]
        elif op < 0.7:
            s.insert(i, rnd.choice(NASTY + list("{}[]<>/\"'")))
        else:
            s[i] = rnd.choice(NASTY + list("{}[]<>/\"'"))
    return "".join(s)


class UrlProperties(unittest.TestCase):
    def test_functions_never_raise_and_are_idempotent(self):
        rnd = random.Random(1)
        for _ in range(4000):
            u = junk_url(rnd)
            n = urlnorm.normalize_url(u)
            self.assertIsInstance(n, str, u)
            self.assertEqual(urlnorm.normalize_url(n), n, f"normalize not idempotent for {u!r}")
            s = urlnorm.strip_tracking(u)
            self.assertEqual(urlnorm.strip_tracking(s), s, f"strip_tracking not idempotent for {u!r}")
            self.assertIsInstance(urlnorm.variant_key(u), str, u)
            self.assertIsInstance(urlnorm.host_of(u), str, u)

    def test_strip_tracking_only_removes_whole_pairs_and_keeps_the_rest_verbatim(self):
        from urllib.parse import urlsplit
        rnd = random.Random(2)
        pairs = ["a=1", "b=Two%20words", "utm_source=x", "utm_medium=y", "fbclid=zz", "id=7", "q=a+b", "flag", "gclid=1", "ref=home"]
        for _ in range(1500):
            chosen = [rnd.choice(pairs) for _ in range(rnd.randint(1, 6))]
            url = f"https://Host.com/Path/{rnd.choice(WORDS)}?{'&'.join(chosen)}#Frag"
            out = urlnorm.strip_tracking(url)
            a, b = urlsplit(url), urlsplit(out)
            self.assertEqual((a.scheme, a.netloc, a.path, a.fragment), (b.scheme, b.netloc, b.path, b.fragment), url)
            kept = [p for p in b.query.split("&") if p]
            it = iter(p for p in a.query.split("&") if p)
            self.assertTrue(all(any(k == o for o in it) for k in kept), f"{url} -> {out}: not an ordered subset")
            self.assertFalse([p for p in kept if p.split("=")[0] in ("utm_source", "utm_medium", "fbclid", "gclid")], out)


class TextProperties(unittest.TestCase):
    def test_text_normalisation_is_idempotent_and_total(self):
        rnd = random.Random(3)
        for _ in range(3000):
            s = text(rnd, 8)
            n = textnorm.normalize_text(s)
            self.assertEqual(textnorm.normalize_text(n), n, repr(s))
            self.assertIsInstance(textnorm.words(s), list)
            self.assertIsInstance(textnorm.char_ngrams(s), list)

    def test_folder_and_tag_cleaning_is_idempotent(self):
        rnd = random.Random(4)
        for _ in range(3000):
            f = "/".join(text(rnd, 3) for _ in range(rnd.randint(0, 4))).replace("∕", "")
            c = model.clean_folder(f)
            self.assertEqual(model.clean_folder(c), c, repr(f))
            self.assertNotIn("..", c.split("/"))
            tags = [text(rnd, 2) for _ in range(rnd.randint(0, 5))]
            ct = model.clean_tags(tags)
            self.assertEqual(model.clean_tags(ct), ct, repr(tags))

    def test_folder_name_escape_roundtrips_and_never_leaks_a_slash(self):
        rnd = random.Random(5)
        for _ in range(2000):
            name = text(rnd, 4) + rnd.choice(["", "/", " / ", "a/b"])
            e = esc_name(name)
            self.assertNotIn("/", e)
            if name.strip():
                self.assertEqual(unesc_name(e), name)
            else:
                self.assertEqual(e, "")


class RoundTrips(unittest.TestCase):
    @staticmethod
    def key(b):
        return (b.url, b.title.strip(), b.folder)

    @staticmethod
    def html_key(b):                    # HTML collapses runs of ASCII whitespace in text (not NBSP, which is a different character)
        return (b.url, re.sub(r"[ \t\r\n\f]+", " ", b.title).strip(), b.folder)

    @staticmethod
    def chrome_key(b):                  # Chromium has three roots; anything else is filed under "Other bookmarks"
        root = b.folder.split("/")[0]
        folder = b.folder if root in formats.CHROME_ROOTS.values() else "/".join(["Other bookmarks"] + ([b.folder] if b.folder else []))
        return (b.url, b.title.strip(), folder)

    def test_netscape_html(self):
        rnd = random.Random(6)
        for trial in range(25):
            bms = bookmarks(rnd, rnd.randint(1, 30))
            back = formats.parse_netscape(formats.write_netscape(bms, sorted({b.folder for b in bms if b.folder})))
            self.assertEqual(sorted(map(self.html_key, back)), sorted(map(self.html_key, bms)), f"trial {trial}")
            self.assertEqual(sorted((b.url, b.added) for b in back), sorted((b.url, b.added) for b in bms))

    def test_chrome_json_is_valid_and_roundtrips(self):
        rnd = random.Random(7)
        for trial in range(25):
            bms = bookmarks(rnd, rnd.randint(1, 30))
            raw = formats.write_chrome(bms, sorted({b.folder for b in bms if b.folder}))
            data = json.loads(raw)
            self.assertEqual(formats.verify_chrome(data), [], f"trial {trial}")
            back = formats.parse_chrome(data)
            self.assertEqual(sorted(map(self.chrome_key, back)), sorted(map(self.chrome_key, bms)), f"trial {trial}")

    def test_json_and_csv(self):
        rnd = random.Random(8)
        for trial in range(25):
            bms = bookmarks(rnd, rnd.randint(1, 30))
            for write, read in ((formats.write_json, lambda t: formats.parse_json(json.loads(t))), (formats.write_csv, formats.parse_csv)):
                back = read(write(bms))
                # our own JSON restores folder names exactly; CSV is a spreadsheet format where stray padding is trimmed
                keyed = self.key if write is formats.write_json else (lambda b: (b.url, b.title.strip(), "/".join(p.strip() for p in b.folder.split("/"))))
                self.assertEqual(sorted(map(keyed, back)), sorted(map(keyed, bms)), f"{write.__name__} trial {trial}")
                self.assertEqual(sorted((b.url, tuple(b.tags), b.notes.strip()) for b in back),
                                 sorted((b.url, tuple(b.tags), b.notes.strip()) for b in bms), f"{write.__name__} trial {trial}")

    def test_known_edge_cases(self):
        nb = Bookmark(url="https://a.com/1", title="Price\u00a0list \u00a0 2024", folder="Pad  ", id="n1")
        self.assertEqual(formats.parse_netscape(formats.write_netscape([nb], ["Pad  "]))[0].title, nb.title)    # no-break spaces are content
        back = formats.parse_json(json.loads(formats.write_json([nb])))[0]
        self.assertEqual(back.folder, "Pad  ")                                                               # our own export restores names exactly
        self.assertEqual(formats.parse_json([{"url": "https://a.com", "folder": " Pad  / Sub "}])[0].folder, "Pad/Sub")  # foreign JSON is trimmed
        for note in ("'-x", "''=cmd", "'plain", "'", "=SUM(1)", "\t tab"):
            row = Bookmark(url="https://a.com/2", title="t", notes=note, id="n2")
            self.assertEqual(formats.parse_csv(formats.write_csv([row]))[0].notes, note, note)

    def test_csv_formula_guard_survives_the_roundtrip(self):
        bms = [Bookmark(url="https://a.com/1", title=t, id=f"i{n}") for n, t in enumerate(["=1+1", "+cmd", "-2", "@SUM(A1)", "'quoted", "normal"])]
        out = formats.write_csv(bms)
        for line in out.splitlines()[1:]:
            self.assertFalse(line.split(",", 1)[1].startswith(("=", "+", "-", "@")), line)   # never a live formula in a spreadsheet
        self.assertEqual([b.title for b in formats.parse_csv(out)], [b.title for b in bms])


class HostileInput(unittest.TestCase):
    def test_garbage_never_escapes_as_an_unexpected_exception(self):
        rnd = random.Random(9)
        readers = (("netscape", formats.parse_netscape, "text"), ("csv", formats.parse_csv, "text"),
                   ("chrome", formats.parse_chrome, "json"), ("firefox", formats.parse_firefox, "json"), ("json", formats.parse_json, "json"))
        for _ in range(1200):
            for name, fn, kind in readers:
                arg = text(rnd, 12) + rnd.choice(["", "<A HREF=", "<DL><DT>", '"url"', "\x00"]) if kind == "text" else rand_json(rnd)
                try:
                    out = fn(arg)
                except formats.FormatError:
                    continue
                except Exception as e:
                    self.fail(f"{name} raised {type(e).__name__}: {e} for {arg!r}")
                self.assertIsInstance(out, list)

    def test_mutated_valid_files_still_parse_or_fail_cleanly(self):
        rnd = random.Random(10)
        bms = bookmarks(rnd, 15)
        folders = sorted({b.folder for b in bms if b.folder})
        samples = (("netscape", formats.parse_netscape, formats.write_netscape(bms, folders), False),
                   ("csv", formats.parse_csv, formats.write_csv(bms), False),
                   ("chrome", formats.parse_chrome, formats.write_chrome(bms, folders), True),
                   ("json", formats.parse_json, formats.write_json(bms), True))
        for _ in range(400):
            for name, fn, good, is_json in samples:
                bad = mutate(rnd, good, rnd.randint(1, 12))
                try:
                    fn(json.loads(bad) if is_json else bad)
                except (formats.FormatError, json.JSONDecodeError):
                    continue
                except Exception as e:
                    self.fail(f"{name} raised {type(e).__name__}: {e}")

    def test_deeply_nested_input_is_refused_not_crashed(self):
        deep = {"type": "url", "url": "https://deep.example.com", "name": "x"}
        for _ in range(5000):
            deep = {"type": "folder", "name": "d", "children": [deep]}
        with self.assertRaises(formats.FormatError):
            formats.parse_chrome({"roots": {"bookmark_bar": deep}})
        try:
            formats.parse_netscape("<DL><DT><H3>x</H3>" * 5000 + '<DT><A HREF="https://a.com">a</A>')
        except formats.FormatError:
            pass                                                     # either answer is fine; a traceback is not


def lz4_stream(rnd):
    """Build a random valid LZ4 block together with the bytes it must decode to."""
    out, enc = bytearray(), bytearray()
    for step in range(rnd.randint(1, 12)):
        lits = bytes(rnd.randrange(256) for _ in range(rnd.choice([0, 1, 5, 14, 15, 16, 40, 300])))
        last = step == 11 or rnd.random() < 0.15
        if not last and not (out or lits):
            lits = b"x"
        match = 0 if last else rnd.choice([4, 5, 18, 19, 30, 300, 600])
        token = (min(len(lits), 15) << 4) | (min(match - 4, 15) if match else 0)
        enc.append(token)
        if len(lits) >= 15:
            n = len(lits) - 15
            enc += b"\xff" * (n // 255) + bytes([n % 255])
        enc += lits
        out += lits
        if last:
            break
        offset = rnd.randint(1, len(out))
        enc += offset.to_bytes(2, "little")
        if match - 4 >= 15:
            n = match - 4 - 15
            enc += b"\xff" * (n // 255) + bytes([n % 255])
        start = len(out) - offset
        for k in range(match):
            out.append(out[start + k])
    else:
        enc.append(0)                                   # final literal-less sequence
    return bytes(enc), bytes(out)


class Lz4(unittest.TestCase):
    def test_decoder_matches_the_format(self):
        rnd = random.Random(11)
        for trial in range(300):
            enc, want = lz4_stream(rnd)
            self.assertEqual(browsers.lz4_block_decompress(enc, len(want)), want, f"trial {trial}")

    def test_truncated_or_corrupted_streams_raise_format_error_only(self):
        rnd = random.Random(12)
        for _ in range(600):
            enc, want = lz4_stream(rnd)
            if not enc:
                continue
            cut = enc[:rnd.randrange(len(enc))]
            flip = bytearray(enc)
            flip[rnd.randrange(len(flip))] = rnd.randrange(256)
            for blob in (cut, bytes(flip)):
                try:
                    browsers.lz4_block_decompress(blob, len(want))
                except formats.FormatError:
                    pass
                except Exception as e:
                    self.fail(f"{type(e).__name__}: {e} for {blob!r}")

    def test_bomb_is_refused(self):
        bomb = bytes([0x1F, 0x41, 0x00, 0xFF] + [0xFF] * 2000 + [0x00])      # a match that claims to expand enormously
        with self.assertRaises(formats.FormatError):
            browsers.lz4_block_decompress(bomb, 100)


class StoreAndPlans(unittest.TestCase):
    def test_search_never_raises_on_hostile_queries(self):
        rnd = random.Random(13)
        with tempfile.TemporaryDirectory() as d:
            lib = Library(d)
            try:
                bms = bookmarks(rnd, 40)
                lib.upsert(bms)
                ops = ["AND", "OR", "NOT", "NEAR(", ")", "(", '"', "*", "tag:", "folder:", "site:", "-", "before:", "after:2024-99-99", "is:", "status:", "^", ":", "\\"]
                for _ in range(1500):
                    q = " ".join(rnd.choice(ops + WORDS + NASTY) for _ in range(rnd.randint(0, 6)))
                    try:
                        res = lib.search(q, limit=5)
                    except Exception as e:
                        self.fail(f"search({q!r}) raised {type(e).__name__}: {e}")
                    self.assertLessEqual(len(res), 5)
            finally:
                lib.close()

    def test_search_finds_what_was_stored_with_either_arabic_or_persian_letters(self):
        with tempfile.TemporaryDirectory() as d:
            lib = Library(d)
            try:
                b = Bookmark(url="https://a.example/1", title="مقاله درباره كتاب و علي", id="a1")
                lib.upsert([b])
                for q in ("کتاب", "كتاب", "علی", "علي"):
                    self.assertEqual([b.id for b, _ in lib.search(q)], ["a1"], q)
            finally:
                lib.close()

    def test_a_rejected_plan_entry_changes_nothing(self):
        b = Bookmark(url="https://a.com/x", title="Keep", folder="Old", tags=["t"], id="k1")
        for entry, why in (({"folder": "New", "title": "Changed", "url": "ftp://nope"}, "http(s)"),
                           ({"folder": ["not", "a", "string"]}, "folder must be a string"),
                           ({"folder": None}, "folder must be a string"),
                           ({"folder": "New", "tags": [1, 2]}, "tags must be"),
                           ({"folder": "New", "added": True}, "added must be"),
                           ({"folder": "A/B/C/D/E", "title": "Changed"}, "deeper")):
            res = categorize.apply_plan([b], {"k1": entry}, max_depth=3)
            self.assertEqual((res.changed, len(res.rejected)), ([], 1), entry)
            self.assertIn(why, res.rejected[0])
            self.assertEqual((b.folder, b.title, b.tags, b.url), ("Old", "Keep", ["t"], "https://a.com/x"), entry)
        ok = categorize.apply_plan([b], {"k1": {"folder": "New", "tags": "x, y", "title": " T ", "added": 5}}, max_depth=3)
        self.assertEqual((len(ok.changed), b.folder, b.title, b.tags, b.added), (1, "New", "T", ["t", "x", "y"], 5))

    def test_random_plans_never_crash_apply_plan(self):
        rnd = random.Random(14)
        bms = bookmarks(rnd, 20)
        ids = [b.id for b in bms]
        before = {b.id: (b.url, b.folder, tuple(b.tags), b.title) for b in bms}
        for _ in range(3000):
            plan = {rnd.choice(ids + ["missing", ""]): rand_json(rnd, 2) for _ in range(rnd.randint(0, 4))}
            if rnd.random() < 0.3:
                plan[rnd.choice(ids)] = {"folder": rand_json(rnd, 3), "tags": rand_json(rnd, 3), "url": rand_json(rnd, 3),
                                         "title": rand_json(rnd, 3), "notes": rand_json(rnd, 3), "added": rand_json(rnd, 3)}
            work = [Bookmark(**{**b.__dict__}) for b in bms]
            try:
                res = categorize.apply_plan(work, plan, max_depth=3)
            except Exception as e:
                self.fail(f"apply_plan raised {type(e).__name__}: {e} for {plan!r}")
            self.assertTrue(all(isinstance(r, str) for r in res.rejected))
            touched = {b.id for b in res.changed} | {b.id for b in res.deleted}
            for b in work:
                if b.id not in touched:
                    self.assertEqual((b.url, b.folder, tuple(b.tags), b.title), before[b.id], f"{b.id} changed without being reported: {plan!r}")
            for b in res.changed:
                self.assertTrue(b.url.startswith(("http://", "https://")) or b.url == dict(zip(ids, [x.url for x in bms]))[b.id], b.url)
                self.assertNotIn("..", b.folder.split("/"))


if __name__ == "__main__":
    unittest.main()
