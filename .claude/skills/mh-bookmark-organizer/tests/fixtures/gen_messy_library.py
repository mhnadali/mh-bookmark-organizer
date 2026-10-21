"""Generate ``messy-library.html``: a synthetic Chrome-style bookmark export that behaves like a real, neglected library.

Nothing in it comes from a real person's bookmarks.  Topics, words and hosts are invented; only the *shape* is
realistic, because the shape is what breaks tools:

* a deep, granular tree (up to 8 levels) with heavy-tailed folder sizes (a few big folders, many with 1-4 items);
* folder names with trailing spaces, and a folder whose name contains a slash;
* empty folders, folder dates, loose bookmarks on the bar;
* Persian-language titles mixed with English ones;
* bookmarks saved in bursts ("sittings") across several years;
* tracking parameters, empty titles, the same page filed in two folders, data-URI icons.

``python gen_messy_library.py [out.html]`` rewrites the committed fixture.  The output depends only on ``SEED``;
``tests/test_fixture.py`` fails if the committed file and this script ever disagree.  ``manifest()`` states what the
file contains, so tests can check an import against it instead of against magic numbers.
"""
from __future__ import annotations

import html
import random
import sys
from pathlib import Path

SEED = 20240607
HERE = Path(__file__).resolve().parent

# invented topics: (top-level folder, English vocabulary, Persian vocabulary)
TOPICS = [
    ("Gardening", ["soil", "compost", "seedling", "pruning", "mulch", "greenhouse", "tomato", "herb", "bulb", "raised-bed", "irrigation", "pollinator"], ["باغبانی", "کود", "نهال", "گلدان"]),
    ("Astronomy", ["telescope", "nebula", "galaxy", "eyepiece", "comet", "eclipse", "orbit", "aperture", "constellation", "meteor", "planetarium"], ["ستاره", "سیاره", "رصد", "کهکشان"]),
    ("Woodworking", ["chisel", "dovetail", "plane", "lathe", "veneer", "joinery", "sanding", "mortise", "bench", "clamp", "varnish"], ["نجاری", "چوب", "رنده"]),
    ("Chess", ["opening", "endgame", "gambit", "tactic", "fianchetto", "rook", "blunder", "tournament", "pawn", "checkmate", "repertoire"], ["شطرنج", "مهره", "گشایش"]),
    ("Baking", ["sourdough", "dough", "yeast", "proofing", "crumb", "oven", "pastry", "whisk", "ferment", "loaf", "buttercream"], ["نان", "خمیر", "شیرینی", "فر"]),
    ("Cycling", ["gearing", "derailleur", "cassette", "cadence", "saddle", "puncture", "brake", "touring", "peloton", "climb", "helmet"], ["دوچرخه", "رکاب", "جاده"]),
    ("Photography", ["aperture", "shutter", "lens", "exposure", "bokeh", "tripod", "portrait", "raw", "histogram", "flash", "composition"], ["عکاسی", "دوربین", "نور", "قاب"]),
    ("Sailing", ["mainsail", "jib", "winch", "tack", "harbor", "anchor", "knot", "tide", "rigging", "regatta", "keel"], ["قایق", "بادبان", "دریا"]),
    ("Pottery", ["kiln", "glaze", "wheel", "clay", "slip", "bisque", "throwing", "stoneware", "trimming", "porcelain", "firing"], ["سفال", "گل", "لعاب", "کوره"]),
    ("Birdwatching", ["warbler", "binoculars", "migration", "plumage", "wetland", "raptor", "nesting", "call", "sparrow", "flock", "field-guide"], ["پرنده", "جنگل", "پرواز"]),
    ("Linux", ["kernel", "shell", "package", "systemd", "partition", "bootloader", "daemon", "cron", "mount", "filesystem", "terminal"], ["لینوکس", "هسته", "دستور"]),
    ("Budgeting", ["ledger", "savings", "expense", "interest", "mortgage", "invoice", "tax", "deduction", "pension", "cashflow", "spreadsheet"], ["بودجه", "پس\u200cانداز", "هزینه"]),
]
BRANCHES = ["Basics", "Advanced", "Gear", "Reviews", "Projects", "Reference", "Inspiration", "Courses", "Tools", "Community", "Archive", "Ideas",
            "Videos", "Beginners", "Techniques", "History", "Suppliers", "Troubleshooting"]
SLASH_NAME = "Q&A / FAQ"                      # a folder name that contains a slash
EMPTY_FOLDERS = [("Misc ideas",), ("Someday ",)]
FILLERS = ["guide", "tips", "how-to", "review", "beginner", "checklist", "explained", "notes", "comparison", "cheatsheet", "tutorial", "overview"]
HOST_ROOTS = ["bramble", "quill", "lantern", "meadow", "copper", "fennel", "harbor", "willow", "ember", "juniper", "tallow", "kestrel"]
ICON = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
TRACKING = ["utm_source=newsletter", "utm_medium=email", "fbclid=IwAR0xyz", "gclid=Cj0KCQ", "ref=homepage&utm_campaign=spring"]
HAS_PERSIAN = lambda s: any("؀" <= ch <= "ۿ" for ch in s)  # noqa: E731


class Gen:
    """Folders are tuples of names (a name may itself contain a slash); items carry the tuple of their folder."""

    def __init__(self, seed: int = SEED):
        self.rnd = random.Random(seed)
        self.folders: dict = {}
        self.items: list = []

    def pad(self, name: str) -> str:
        return name + " " if self.rnd.random() < 0.4 else name

    def build_tree(self):
        rnd = self.rnd
        for top, vocab, persian in TOPICS:
            hosts = [f"{rnd.choice(HOST_ROOTS)}{rnd.choice(['hub', 'wiki', 'daily', 'lab', 'club'])}" for _ in range(5)]
            self._grow((self.pad(top),), vocab, persian, hosts, limit=rnd.randint(3, 7))
        deep = next(path for path in self.folders if len(path) >= 3)
        self.folders[deep[:-1] + (SLASH_NAME,)] = {**self.folders[deep], "size": 5}

    def _grow(self, path, vocab, persian, hosts, limit):
        rnd = self.rnd
        size = rnd.choice([1, 1, 1, 2, 2, 3, 4, 4, 6, 9, 14, 25])   # heavy tail: most folders are tiny, a few are large
        self.folders[path] = {"vocab": rnd.sample(vocab, k=min(len(vocab), rnd.randint(4, 7))), "persian": persian, "hosts": hosts, "size": size}
        if len(path) >= limit:
            return
        for name in rnd.sample(BRANCHES, k=1 + rnd.choice([0, 0, 1])):                # a spine down to `limit`, with the odd side branch
            self._grow(path + (self.pad(name),), vocab, persian, hosts, limit)

    def title(self, f):
        rnd = self.rnd
        words = rnd.sample(f["vocab"], k=min(len(f["vocab"]), rnd.randint(2, 3))) + [rnd.choice(FILLERS)]
        rnd.shuffle(words)
        text = " ".join(words).capitalize()
        if rnd.random() < 0.24:
            fa = " ".join(rnd.sample(f["persian"], k=min(len(f["persian"]), rnd.randint(2, 3))))
            text = fa if rnd.random() < 0.7 else f"{fa} - {text}"
        return text

    def make_items(self):
        rnd = self.rnd
        for path, f in self.folders.items():
            left = f["size"]
            t = rnd.randint(1_450_000_000, 1_690_000_000)
            while left > 0:
                burst = min(left, rnd.randint(1, 6))                    # a "sitting": several saves within minutes
                t += rnd.choice([3_600, 86_400, 9 * 86_400])
                for k in range(burst):
                    host = rnd.choice(f["hosts"])
                    slug = "-".join(rnd.sample(f["vocab"], k=2)) + f"-{rnd.randint(100, 99999)}"
                    url = f"https://{host}.example.com/{rnd.choice(['articles', 'posts', 'p', 'learn'])}/{slug}"
                    if rnd.random() < 0.04:
                        url += "?" + rnd.choice(TRACKING)
                    self.items.append({"title": self.title(f), "url": url, "folder": path, "added": t + k * rnd.randint(20, 300)})
                left -= burst
        for it in rnd.sample(self.items, k=max(2, len(self.items) // 85)):
            it["title"] = ""                                            # an empty title: the browser shows the URL instead
        clean = [i for i in self.items if "?" not in i["url"]]
        for it in rnd.sample(clean, k=6):                               # the same page filed in two folders
            self.items.append({**it, "folder": rnd.choice([p for p in self.folders if p != it["folder"]]), "added": it["added"] + 86_400})
        for k in range(18):                                             # loose bookmarks on the bar
            self.items.append({"title": f"Loose {rnd.choice(FILLERS)} {k}", "url": f"https://loose{k}.example.org/{rnd.randint(1, 999)}",
                               "folder": (), "added": 1_700_000_000 + k * 60})
        self.items.sort(key=lambda i: (i["folder"], i["added"], i["url"]))

    # ----------------------------------------------------------- output
    def render(self) -> str:
        rnd = random.Random(SEED + 1)
        esc = html.escape
        tree: dict = {"folders": {}, "items": []}

        def node(path):
            cur = tree
            for name in path:
                cur = cur["folders"].setdefault(name, {"folders": {}, "items": []})
            return cur

        for path in list(self.folders) + EMPTY_FOLDERS:
            node(path)
        for it in self.items:
            node(it["folder"])["items"].append(it)

        def dates(path):
            r = random.Random("\x00".join(path))
            first = 1_470_000_000 + r.randint(0, 50_000_000)
            return first, first + r.randint(0, 20_000_000)

        out = ["<!DOCTYPE NETSCAPE-Bookmark-file-1>", "<!-- This is an automatically generated file.", "     It will be read and overwritten.",
               "     DO NOT EDIT! -->", '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
               "<TITLE>Bookmarks</TITLE>", "<H1>Bookmarks</H1>", "<DL><p>"]

        def link(it, pad):
            icon = f' ICON="{ICON}"' if rnd.random() < 0.03 else ""
            return f'{pad}<DT><A HREF="{esc(it["url"], quote=True)}" ADD_DATE="{it["added"]}"{icon}>{esc(it["title"])}</A>'

        def emit(n, indent, path):
            pad = "    " * indent
            out.extend(link(it, pad) for it in n["items"])
            for name in sorted(n["folders"]):
                first, last = dates(path + (name,))
                out.append(f'{pad}<DT><H3 ADD_DATE="{first}" LAST_MODIFIED="{last}">{esc(name)}</H3>')
                out.append(f"{pad}<DL><p>")
                emit(n["folders"][name], indent + 1, path + (name,))
                out.append(f"{pad}</DL><p>")

        out.append('    <DT><H3 ADD_DATE="1480000000" LAST_MODIFIED="1700000000" PERSONAL_TOOLBAR_FOLDER="true">Bookmarks bar</H3>')
        out.append("    <DL><p>")
        out.extend(link(it, "        ") for it in tree["items"])
        out.append("    </DL><p>")
        out.append('    <DT><H3 ADD_DATE="1480000000" LAST_MODIFIED="1700000000">Other bookmarks</H3>')
        out.append("    <DL><p>")
        emit({"folders": tree["folders"], "items": []}, 2, ())
        out.append("    </DL><p>")
        out.append("</DL><p>")
        return "\n".join(out) + "\n"

    # --------------------------------------------------------- manifest
    def manifest(self) -> dict:
        folder_paths = set(self.folders) | {p for path in self.folders for p in (path[:i] for i in range(1, len(path)))}
        norm = {}
        for it in self.items:
            norm.setdefault(it["url"].split("?")[0], []).append(it)
        return {
            "bookmarks": len(self.items),
            "loose": sum(1 for i in self.items if not i["folder"]),
            "folders": len(folder_paths) + len(EMPTY_FOLDERS),
            "empty_folders": [p[0].strip() for p in EMPTY_FOLDERS],
            "trailing_space_folders": sum(1 for p in folder_paths if any(n != n.strip() for n in p)) + 1,   # + "Someday "
            "slash_folders": 1,
            "empty_titles": sum(1 for i in self.items if not i["title"]),
            "tracking_urls": sum(1 for i in self.items if "?" in i["url"]),
            "same_page_twice": sum(len(v) - 1 for v in norm.values() if len(v) > 1),
            "persian_titles": sum(1 for i in self.items if HAS_PERSIAN(i["title"])),
            "max_depth": max(len(p) for p in self.folders) + 1,      # + the "Other bookmarks" root
        }


def build() -> Gen:
    g = Gen()
    g.build_tree()
    g.make_items()
    return g


def generate() -> str:
    return build().render()


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "messy-library.html"
    target.write_text(generate(), encoding="utf-8", newline="\n")
    print(f"wrote {target} ({target.stat().st_size} bytes)")
