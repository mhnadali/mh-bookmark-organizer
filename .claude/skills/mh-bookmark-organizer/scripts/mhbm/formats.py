"""Readers and writers for every supported bookmark format."""
from __future__ import annotations

import csv
import functools
import hashlib
import html
import io
import json
import re
import time
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from .model import Bookmark, clean_folder, esc_name, unesc_name

WEBKIT_EPOCH_OFFSET = 11644473600  # seconds between 1601-01-01 and 1970-01-01
CHROME_ROOTS = {
    "bookmark_bar": "Bookmarks bar",
    "other": "Other bookmarks",
    "synced": "Mobile bookmarks",
}
READ_FORMATS = ("html", "chrome", "firefox", "csv", "json")
WRITE_FORMATS = ("html", "chrome", "csv", "json", "md", "obsidian", "raindrop", "pocket", "linkwarden", "karakeep", "instapaper")
TOOLBAR = "Bookmarks bar"
OTHER = "Other bookmarks"


class FormatError(ValueError):
    pass


GENERATOR = "mh-bookmark-organizer"  # marks our own JSON export, whose folder names are restored exactly


def _structural(fn):
    """Parsers walk untrusted nested data: a malformed shape is a ``FormatError``, never a traceback."""
    @functools.wraps(fn)
    def wrapper(arg, *args, **kwargs):
        try:
            return fn(arg, *args, **kwargs)
        except FormatError:
            raise
        except (AttributeError, TypeError, KeyError, IndexError, ValueError, OverflowError, RecursionError, csv.Error) as e:
            raise FormatError(f"malformed {fn.__name__.removeprefix('parse_')} data ({type(e).__name__}: {e})") from e
    return wrapper


class ParsedList(list):
    """List of bookmarks that also remembers every folder seen (empty ones included) and their dates."""

    def __init__(self, items=(), folders=(), folder_meta=None):
        super().__init__(items)
        self.folders = set(folders)
        self.folder_meta = dict(folder_meta or {})  # path -> (add_date, last_modified)


# --------------------------------------------------------------------- dates
def to_unix(value) -> int:
    """Best-effort conversion of the many timestamp styles in the wild."""
    if value in (None, "", 0, "0"):
        return 0
    if isinstance(value, str):
        v = value.strip()
        if not re.fullmatch(r"\d+(\.\d+)?", v):
            try:
                dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp())
            except ValueError:
                return 0
        value = float(v)
    n = int(value)
    if n > 10**17:  # Firefox imported a milliseconds ADD_DATE as if it were seconds and stored it x 10**6
        return n // 10**9
    if n > 10**16:  # webkit microseconds
        return n // 10**6 - WEBKIT_EPOCH_OFFSET
    if n > 10**14:  # unix microseconds (Firefox)
        return n // 10**6
    if n > 10**11:  # unix milliseconds
        return n // 1000
    return n


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else ""


# ------------------------------------------------------------------ sniffing
def sniff(path: Path) -> str:
    suffix = path.suffix.lower()
    head = path.read_text(encoding="utf-8-sig", errors="replace")[:4096].lstrip()   # utf-8-sig: Chromium accepts a BOM, json.loads does not
    if suffix in (".html", ".htm") or head[:200].upper().startswith(("<!DOCTYPE NETSCAPE", "<!DOCTYPE HTML")):
        return "html"
    if suffix == ".csv":
        return "csv"
    if head.startswith(("{", "[")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as e:
            raise FormatError(f"{path}: invalid JSON ({e})") from e
        if isinstance(data, dict) and "roots" in data:
            return "chrome"
        if isinstance(data, dict) and ("typeCode" in data or "root" in data):
            return "firefox"
        return "json"
    raise FormatError(f"{path}: cannot detect format; pass --format")


def read_file(path, fmt: str | None = None) -> ParsedList:
    try:
        return _read_file(path, fmt)
    except RecursionError as e:  # absurdly nested JSON
        raise FormatError(f"{path}: nested too deeply to read") from e


def _read_file(path, fmt: str | None = None) -> ParsedList:
    path = Path(path)
    fmt = fmt or sniff(path)
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    readers = {
        "html": parse_netscape,
        "chrome": lambda t: parse_chrome(json.loads(t)),
        "firefox": lambda t: parse_firefox(json.loads(t)),
        "csv": parse_csv,
        "json": lambda t: parse_json(json.loads(t)),
    }
    if fmt not in readers:
        raise FormatError(f"unknown format {fmt!r}; choose from {READ_FORMATS}")
    return readers[fmt](text)


def _with_folders(items, extra=(), folder_meta=None) -> ParsedList:
    folders = set(extra)
    for b in items:
        parts = b.folder.split("/") if b.folder else []
        folders.update("/".join(parts[: i + 1]) for i in range(len(parts)))
    return ParsedList(items, folders, folder_meta)


# ------------------------------------------------- Netscape HTML (+ Pocket)
_HTML_WS = re.compile(r"[ \t\r\n\f]+")  # HTML's own whitespace: a no-break space inside a title is content, not padding


class _NetscapeParser(HTMLParser):
    POCKET_SECTIONS = {"unread": "Pocket/Unread", "read archive": "Pocket/Read Archive"}
    POCKET_STATE = {"Pocket/Unread": "unread", "Pocket/Read Archive": "read"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: list = []
        self.folders: set = set()
        self.folder_meta: dict = {}
        self.pending_dates = (0, 0)
        self.stack: list = []
        self.base = ""
        self.pending_folder = None
        self.capture = None  # "h3" | "h1" | "a" | "dd"
        self.buf: list = []
        self.cur: Bookmark | None = None
        self.last: Bookmark | None = None

    def _path(self):
        names = [n for n in self.stack if n]
        return "/".join(([self.base] if self.base else []) + names)

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if self.capture == "dd" and tag != "br":  # <DD> has no end tag
            self._flush_dd()
        if tag in ("h3", "h1"):
            self.capture, self.buf = tag, []
            if tag == "h3":
                self.pending_dates = (to_unix(a.get("add_date")), to_unix(a.get("last_modified")))
        elif tag == "dl":
            self.stack.append(self.pending_folder)
            if self.pending_folder:
                path = self._path()
                self.folders.add(path)
                if any(self.pending_dates):
                    self.folder_meta[path] = self.pending_dates
            self.pending_folder = None
        elif tag == "a" and a.get("href"):
            self.cur = Bookmark(
                url=a["href"].strip(),
                folder=self._path(),
                tags=a.get("tags", ""),
                added=to_unix(a.get("add_date") or a.get("time_added")),
                icon=a.get("icon", "") if a.get("icon", "").startswith("data:image/") else "",
                state=self.POCKET_STATE.get(self.base, ""),
            )
            self.capture, self.buf = "a", []
        elif tag == "dd":
            self.capture, self.buf = "dd", []

    def handle_endtag(self, tag):
        if tag == "h3" and self.capture == "h3":
            self.pending_folder = esc_name("".join(self.buf)) or None
            self.capture = None
        elif tag == "h1" and self.capture == "h1":
            self.base = self.POCKET_SECTIONS.get("".join(self.buf).strip().lower(), "")
            self.capture = None
        elif tag == "a" and self.capture == "a" and self.cur:
            self.cur.title = _HTML_WS.sub(" ", "".join(self.buf)).strip(" \t\r\n\f")
            self.items.append(self.cur)
            self.last, self.cur, self.capture = self.cur, None, None
        elif tag == "dl" and self.stack:
            if self.capture == "dd":
                self._flush_dd()
            self.stack.pop()
        elif tag == "dd" and self.capture == "dd":
            self._flush_dd()

    def handle_data(self, data):
        if self.capture:
            self.buf.append(data)

    def _flush_dd(self):
        if self.last is not None:
            self.last.notes = " ".join("".join(self.buf).split())
        self.capture = None

    def close(self):
        if self.capture == "dd":
            self._flush_dd()
        super().close()


@_structural
def parse_netscape(text: str) -> ParsedList:
    p = _NetscapeParser()
    p.feed(text)
    p.close()
    return _with_folders(p.items, p.folders, p.folder_meta)


def _tree(bms, folders):
    tree: dict = {"_items": [], "_dirs": {}}

    def descend(path):
        node = tree
        for part in filter(None, path.split("/")):
            node = node["_dirs"].setdefault(part, {"_items": [], "_dirs": {}})
        return node

    for f in folders:
        descend(f)
    for b in bms:
        descend(b.folder)["_items"].append(b)
    return tree


def write_netscape(bms, folders=(), icons=None, folder_meta=None) -> str:
    icons, folder_meta = icons or {}, folder_meta or {}
    out = [
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
        "<TITLE>Bookmarks</TITLE>",
        "<H1>Bookmarks</H1>",
        "<DL><p>",
    ]

    def emit(node, depth, prefix=""):
        pad = "    " * depth
        for b in node["_items"]:
            attrs = f'HREF="{html.escape(b.url, quote=True)}"'
            if b.added:
                attrs += f' ADD_DATE="{b.added}"'
            if b.tags:
                attrs += f' TAGS="{html.escape(",".join(b.tags), quote=True)}"'
            icon = icons.get(b.id) or b.icon
            if icon:
                attrs += f' ICON="{html.escape(icon, quote=True)}"'
            out.append(f"{pad}<DT><A {attrs}>{html.escape(b.title)}</A>")
            if b.notes:
                out.append(f"{pad}<DD>{html.escape(b.notes)}")
        order = sorted(node["_dirs"], key=lambda n: (n != TOOLBAR or depth != 1, n.lower()))
        for name in order:
            path = f"{prefix}/{name}" if prefix else name
            dates = folder_meta.get(path)
            attrs = ""
            if dates and dates[0]:
                attrs += f' ADD_DATE="{dates[0]}"'
            if dates and dates[1]:
                attrs += f' LAST_MODIFIED="{dates[1]}"'
            if depth == 1 and name == TOOLBAR:
                attrs += ' PERSONAL_TOOLBAR_FOLDER="true"'
            elif depth == 1 and name == OTHER:  # Firefox files this folder under its own "Other Bookmarks" root; Chrome ignores the attribute
                attrs += ' UNFILED_BOOKMARKS_FOLDER="true"'
            out.append(f"{pad}<DT><H3{attrs}>{html.escape(unesc_name(name))}</H3>")
            out.append(f"{pad}<DL><p>")
            emit(node["_dirs"][name], depth + 1, path)
            out.append(f"{pad}</DL><p>")

    emit(_tree(bms, folders), 1)
    out.append("</DL><p>")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------- Chrome
@_structural
def parse_chrome(data: dict) -> ParsedList:
    if "roots" not in data:
        raise FormatError("not a Chrome Bookmarks file (missing 'roots')")
    items: list = []
    folders: set = set()

    def walk(node, path):
        if node.get("type") == "url":
            items.append(Bookmark(
                url=node.get("url", ""), title=node.get("name", ""), folder="/".join(path),
                added=to_unix(node.get("date_added")),
            ))
        for child in node.get("children", []):
            if child.get("type") == "folder":
                sub = path + [esc_name(child.get("name", ""))]
                folders.add("/".join(sub))
                walk(child, sub)
            else:
                walk(child, path)

    for key, root in data["roots"].items():
        if isinstance(root, dict):
            name = CHROME_ROOTS.get(key, root.get("name", key))
            folders.add(esc_name(name))
            walk(root, [esc_name(name)])
    return _with_folders(items, folders)


# GUIDs Chromium assigns to its permanent folders (components/bookmarks/browser/bookmark_node.h)
CHROME_ROOT_GUIDS = {"bookmark_bar": "0bc5d13f-2cba-5d74-951f-3f233fe6c908",
                     "other": "82b081ec-3dd3-529c-8475-ab6c344590dd",
                     "synced": "4cf2e351-0e85-532b-bb37-df045d8f8d0f"}
_GUID_NS = uuid.UUID("6b9f3c52-7a41-4f0e-9d6a-2f0f4f0b8c11")
_GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


MAX_CHROME_DEPTH = 90  # Chromium discards the WHOLE file at 98 nested folders (JSON parser depth limit); 97 still loads
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_HOST_REQUIRED = ("http", "https", "ftp", "ws", "wss", "chrome", "chrome-extension", "chrome-untrusted", "devtools")


def chrome_url_ok(url) -> bool:
    """False for url strings Chromium silently drops when it loads a ``Bookmarks`` file (no scheme, empty host, bad port...).

    Observed on Chromium 141 and Brave 155 (tests/realbrowser/urlvalid): a dropped node also leaves the file's checksum wrong
    on Chromium <= 143, and ``sync`` would later read the missing node as "deleted in the browser". It is a conservative
    predictor, not a URL parser: it knows the realistic bad inputs (``example.com``, empty, ``http://``, ``http://h:99999``)."""
    if not isinstance(url, str):
        return False
    text = re.sub(r"[\t\r\n]", "", url).strip("".join(map(chr, range(33))))
    match = _URL_SCHEME.match(text)
    if not match:
        return False
    if match.group(0)[:-1].lower() not in _HOST_REQUIRED:
        return True
    authority = re.split(r"[/?#]", text[match.end():].replace("\\", "/").lstrip("/"), maxsplit=1)[0]
    hostport = authority.rpartition("@")[2]
    if authority.endswith("@") or not hostport:
        return False
    return _host_port_ok(hostport)


def _host_port_ok(hostport: str) -> bool:
    if hostport.startswith("["):
        host, closed, rest = hostport[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else rest
        if not closed or not re.fullmatch(r"[0-9A-Fa-f:.]+", host):
            return False
    else:
        host, _, port = hostport.partition(":")
    if (port and (not port.isdigit() or int(port) > 65535)) or not host:
        return False
    if re.search(r"[<>^|]", host) or re.search(r"%(?![0-9A-Fa-f]{2})", host):
        return False
    if re.fullmatch(r"(?=.*[0-9])[0-9.]+", host):   # an all-numeric dotted host has to be a valid (legacy) IPv4 address
        parts = host.rstrip(".").split(".")
        return len(parts) <= 4 and all(parts) and not (len(parts) == 4 and any(int(p) > 255 for p in parts))
    return True


def _checksum(roots: dict, hasher) -> str:
    def walk(node):
        hasher.update(str(node.get("id", "")).encode("utf-8"))   # a missing id is reported by verify_chrome, not a crash here
        hasher.update(str(node.get("name", "")).encode("utf-16-le", "surrogatepass"))
        if node.get("type") == "url":
            hasher.update(b"url")
            hasher.update(str(node.get("url", "")).encode("utf-8", "surrogatepass"))
        else:
            hasher.update(b"folder")
            for child in node.get("children", []):
                walk(child)

    for key in CHROME_ROOTS:  # bookmark_bar, other, synced - the order Chromium encodes them
        if key in roots:
            walk(roots[key])
    return hasher.hexdigest()


def chrome_checksum(roots: dict) -> str:
    """MD5 over id, name (UTF-16LE), type and url of every node in Chromium's encoding order (lower-case hex)."""
    return _checksum(roots, hashlib.md5())


def chrome_checksum_sha256(roots: dict) -> str:
    """The same stream as ``chrome_checksum`` hashed with SHA-256: the ``checksum_sha256`` key that newer Chromium builds write
    when their ``kEnableBookmarkCodecSHA256`` feature is on (read from the Chromium source; Chromium 141 does not write it)."""
    return _checksum(roots, hashlib.sha256())


def write_chrome(bms, folders=(), folder_meta=None, skipped=None) -> str:
    """A Chromium-family ``Bookmarks`` file: ids, GUIDs and checksum included.

    Checked against real browsers (Chromium 141, Brave 155, ungoogled-chromium 155): ids, GUIDs, order, titles, URLs and dates
    arrive unchanged and the file passes ``verify-chrome`` again after the browser rewrites it (tests/realbrowser).
    Bookmarks whose URL Chromium would silently drop (see ``chrome_url_ok``) are left out and appended to ``skipped`` (a list)
    when one is given, so that the checksum stays valid and the caller can tell the user. Keep the original file
    (``install-chrome`` backs it up).
    """
    folder_meta = folder_meta or {}
    counter = iter(range(1, 10**9))
    names = {v: k for k, v in CHROME_ROOTS.items()}

    def stamp(ts):
        return str((int(ts) + WEBKIT_EPOCH_OFFSET) * 10**6) if ts else "0"

    def new_folder(name, path=""):
        nid = str(next(counter))
        meta = folder_meta.get(path, (0, 0))
        return {"id": nid, "name": name, "type": "folder", "date_added": stamp(meta[0]),
                "date_last_used": "0", "date_modified": stamp(meta[1]),
                "guid": str(uuid.uuid5(_GUID_NS, f"folder|{nid}|{path}|{name}")), "children": []}

    roots = {k: new_folder(v, v) for k, v in CHROME_ROOTS.items()}
    for key, node in roots.items():
        node["guid"] = CHROME_ROOT_GUIDS[key]

    def locate(path):
        parts = [p for p in path.split("/") if p]
        root_key = names.get(parts[0]) if parts else None
        node = roots[root_key or "other"]
        walked = parts[:1] if root_key else []
        if len(parts) - (1 if root_key else 0) > MAX_CHROME_DEPTH:
            raise FormatError(f"folder nested more than {MAX_CHROME_DEPTH} levels deep ({path[:60]!r}...): Chromium would discard the whole file")
        for part in (parts[1:] if root_key else parts):
            walked.append(part)
            name = unesc_name(part)
            nxt = next((c for c in node["children"] if c["type"] == "folder" and c["name"] == name), None)
            if nxt is None:
                nxt = new_folder(name, "/".join(walked))
                node["children"].append(nxt)
            node = nxt
        return node

    for f in sorted(folders):
        locate(f)
    for b in bms:
        if not chrome_url_ok(b.url):
            if skipped is not None:
                skipped.append(b)
            continue
        nid = str(next(counter))
        locate(b.folder)["children"].append({
            "id": nid, "name": b.title, "type": "url", "url": b.url, "date_added": stamp(b.added),
            "date_last_used": "0", "guid": str(uuid.uuid5(_GUID_NS, f"url|{nid}|{b.url}|{b.title}"))})
    return json.dumps({"checksum": chrome_checksum(roots), "roots": roots, "version": 1}, indent=3, ensure_ascii=False)


class _ChromeCheck:
    """Walks a Chromium bookmark tree collecting structural problems (ids and GUIDs must be unique and well formed)."""

    def __init__(self):
        self.problems: list = []
        self.ids: set = set()
        self.guids: set = set()

    def walk(self, node, where, depth=0) -> None:
        self._required(node, where)
        self._identity(node, where)
        kind = node.get("type")
        if kind == "url":
            self._url(node, where)
        elif kind == "folder":
            if depth >= MAX_CHROME_DEPTH:
                self.problems.append(f"{where}: folders nested more than {MAX_CHROME_DEPTH} deep (Chromium discards the whole file at 98)")
                return
            children = node.get("children", [])
            if not isinstance(children, list):
                self.problems.append(f"{where}: 'children' is not a list")
                return
            for i, child in enumerate(children):
                if not isinstance(child, dict):
                    self.problems.append(f"{where}[{i}]: a bookmark node must be an object")
                    continue
                self.walk(child, f"{where}/{child.get('name', i)}", depth + 1)
        else:
            self.problems.append(f"{where}: unknown type {kind!r}")

    def _url(self, node, where) -> None:
        url = node.get("url", "")
        if not str(url).strip():
            self.problems.append(f"{where}: url node without url")
        elif not chrome_url_ok(url):
            self.problems.append(f"{where}: url {str(url)[:60]!r} is one Chromium silently drops when it loads the file")

    def _required(self, node, where) -> None:
        for field in ("id", "name", "type"):
            if field not in node:
                self.problems.append(f"{where}: missing '{field}'")
        if not isinstance(node.get("name", ""), str):
            self.problems.append(f"{where}: 'name' is not a string")

    def _identity(self, node, where) -> None:
        raw = node.get("id", "")
        nid = str(raw)
        if "id" in node and not (isinstance(raw, str) and raw.isascii() and raw.isdigit() and int(raw) > 0):
            self.problems.append(f"{where}: id {raw!r} is not a positive integer string (Chromium renumbers)")
        if nid in self.ids:
            self.problems.append(f"{where}: duplicate id {nid}")
        self.ids.add(nid)
        guid = node.get("guid")
        if guid is None:
            return
        if not _GUID_RE.match(str(guid)):
            self.problems.append(f"{where}: malformed guid {guid!r}")
        elif guid in self.guids:
            self.problems.append(f"{where}: duplicate guid {guid}")
        self.guids.add(guid)


def verify_chrome(data: dict) -> list:
    """Structural problems in a Chromium ``Bookmarks`` document (an empty list means it looks valid).

    What each problem costs was measured on real browsers: a wrong ``version`` or a missing root makes Chromium discard the
    whole file; a node it cannot parse (bad URL, unknown type) is dropped; a wrong checksum, a duplicate or non-numeric id
    makes Chromium <= 143 renumber every id (144 and later ignore the checksum and fix only the offending id)."""
    roots = data.get("roots")
    if not isinstance(roots, dict):
        return ["missing 'roots'"]
    check = _ChromeCheck()
    if data.get("version") != 1:
        check.problems.append(f"version is {data.get('version')!r}, Chromium only loads 1 (it discards the whole file otherwise)")
    check.problems.extend(f"missing root '{key}'" for key in CHROME_ROOTS if key not in roots)
    for key, node in roots.items():
        if isinstance(node, dict):
            check.walk(node, key)
    stored = data.get("checksum")
    if stored is None:
        check.problems.append("no checksum (Chromium <= 143 renumbers every id; newer builds ignore it)")
    elif stored != chrome_checksum(roots):
        check.problems.append(f"checksum mismatch: file says {stored}, content gives {chrome_checksum(roots)} "
                              "(Chromium <= 143 renumbers every id)")
    stored256 = data.get("checksum_sha256")
    if stored256 is not None and stored256 != chrome_checksum_sha256(roots):
        check.problems.append(f"checksum_sha256 mismatch: file says {stored256}, content gives {chrome_checksum_sha256(roots)}")
    return check.problems


# ------------------------------------------------------------------ Firefox
@_structural
def parse_firefox(data: dict) -> ParsedList:
    items: list = []
    folders: set = set()
    # keys are the `root` field minus "Folder", lower-cased: toolbarFolder, unfiledBookmarksFolder (!), mobileFolder, bookmarksMenuFolder
    names = {"toolbar": "Bookmarks bar", "unfiled": "Other bookmarks", "unfiledbookmarks": "Other bookmarks",
             "mobile": "Mobile bookmarks", "bookmarksmenu": "Bookmarks menu"}

    def walk(node, path):
        if node.get("uri") and node.get("typeCode", 1) == 1 and not node["uri"].startswith("place:"):  # place: = a saved query, not a page
            items.append(Bookmark(url=node["uri"], title=node.get("title", ""), folder="/".join(path),
                                  tags=node.get("tags", ""), added=to_unix(node.get("dateAdded"))))
        for child in node.get("children", []):
            if child.get("children") is not None or child.get("typeCode") == 2:
                root = child.get("root", "").replace("Folder", "").lower()
                name = names.get(root) or child.get("title", "")
                sub = path + [esc_name(name)] if name else path
                if name:
                    folders.add("/".join(sub))
                walk(child, sub)
            else:
                walk(child, path)

    walk(data, [])
    return _with_folders(items, folders)


# ---------------------------------------------------------------- CSV / JSON
_ALIASES = {
    "url": ("url", "href", "link", "uri", "address"),
    "title": ("title", "name", "label"),
    "folder": ("folder", "folders", "collection", "path", "category", "group"),
    "tags": ("tags", "labels", "keywords"),
    "notes": ("notes", "note", "description", "excerpt", "comment", "selection"),
    "added": ("added", "created", "created_at", "createdat", "date_added", "add_date", "date",
              "time_added", "timestamp", "savedat", "saved_at"),
}


def _pick(row: dict, field: str):
    lowered = {str(k).strip().lower(): v for k, v in row.items() if k}
    for alias in _ALIASES[field]:
        if lowered.get(alias):
            return lowered[alias]
    return ""


def _names(value):
    """Tags may be a string, a JSON-ish list string, a list of strings or a list of {name} objects."""
    if isinstance(value, str):
        v = value.strip()
        if v.startswith("["):
            try:
                value = json.loads(v.replace("'", '"'))
            except json.JSONDecodeError:
                return v.strip("[]")
        else:
            return v
    return [t.get("name", "") if isinstance(t, dict) else str(t) for t in value or []]


_STATE_WORDS = {"unread": "unread", "to read": "unread", "reading": "reading", "read": "read", "archive": "read",
                "archived": "read", "read archive": "read", "succeeded": "unread", "processing": "unread"}


def _row_state(row: dict, folder: str) -> str:
    low = {str(k).strip().lower(): v for k, v in row.items() if k}
    raw = str(low.get("state") or low.get("status") or low.get("read_state") or "").strip().lower()
    if raw in _STATE_WORDS:
        return _STATE_WORDS[raw]
    if folder.lower() in ("unread", "archive", "read archive"):  # Instapaper / Pocket style folders
        return _STATE_WORDS[folder.lower()]
    if low.get("archived") in (True, "true", "True", 1, "1"):
        return "read"
    return ""


def _truthy(v) -> bool:
    return v in (True, 1) or str(v).strip().lower() in ("true", "1", "yes", "y")


def _from_row(row: dict, default_folder: str = "", exact: bool = False) -> Bookmark | None:
    content = row.get("content")
    url = str(_pick(row, "url") or (content.get("url", "") if isinstance(content, dict) else "")).strip()
    if not url:
        return None
    folder = clean_folder(str(_pick(row, "folder")) or default_folder, strip=not exact)
    note = _pick(row, "notes") or row.get("note") or ""
    low = {str(k).strip().lower(): v for k, v in row.items() if k}
    fav = _truthy(low.get("favorite") or low.get("favourite") or low.get("favourited") or low.get("starred"))
    return Bookmark(url=url, title=str(_pick(row, "title")), folder=folder, state=_row_state(row, folder), favorite=fav,
                    tags=_names(_pick(row, "tags")), notes=str(note), added=to_unix(_pick(row, "added")))


_CSV_GUARDED = re.compile(r"'*[=+\-@\t\r]")  # starts with a spreadsheet-formula character, perhaps behind apostrophes


def _csv_unsafe(s: str) -> str:
    """Undo ``_cell``: one guard apostrophe in front of a formula-looking value is ours, not the user's."""
    return s[1:] if s.startswith("'") and _CSV_GUARDED.match(s, 1) else s


@_structural
def parse_csv(text: str) -> ParsedList:
    out = []
    reader = csv.DictReader(io.StringIO(text))
    header = {str(h).strip().lower() for h in (reader.fieldnames or [])}
    instapaper = {"selection", "timestamp", "folder"} <= header  # Unread / Archive / Starred are built-in folders there
    for row in reader:
        b = _from_row({k: (_csv_unsafe(v) if isinstance(v, str) else v) for k, v in row.items() if k})
        if b:
            if instapaper:
                built_in = b.folder.lower()
                if built_in == "starred":
                    b.favorite, b.folder = True, ""
                elif built_in in ("unread", "archive"):
                    b.folder = ""
            out.append(b)
    return _with_folders(out)


@_structural
def parse_json(data) -> ParsedList:
    """Native, Karakeep, Linkwarden and Omnivore JSON exports."""
    items = []
    if isinstance(data, dict) and isinstance(data.get("collections"), list):  # Linkwarden
        cols = {c.get("id"): c for c in data["collections"] if isinstance(c, dict)}

        def path(col, guard=0):
            name = esc_name(col.get("name", ""))
            parent = cols.get(col.get("parentId")) if col.get("parentId") is not None else None
            return (path(parent, guard + 1) + "/" if parent and guard < 20 else "") + name

        for col in data["collections"]:
            folder = path(col)
            for link in col.get("links", []):
                b = _from_row(link, default_folder="" if folder in ("Unorganized", "") else folder)
                if b:
                    items.append(b)
        return _with_folders(items)
    rows = data.get("bookmarks", []) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise FormatError("JSON must be a list of bookmarks or {'bookmarks': [...]}")
    exact = isinstance(data, dict) and data.get("generator") == GENERATOR   # our own export: keep names byte for byte
    items = [b for b in (_from_row(r, exact=exact) for r in rows if isinstance(r, dict)) if b]
    return _with_folders(items)


def _cell(value) -> str:
    s = str(value)
    return "'" + s if _CSV_GUARDED.match(s) else s


def write_csv(bms, folders=()) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["url", "title", "folder", "tags", "notes", "added", "status", "state", "favorite"])
    for b in bms:
        w.writerow([_cell(v) for v in (b.url, b.title, b.folder, ",".join(b.tags), b.notes, iso(b.added), b.status,
                                       b.state, "true" if b.favorite else "")])
    return buf.getvalue()


def write_json(bms, folders=()) -> str:
    rows = [{"url": b.url, "title": b.title, "folder": b.folder, "tags": b.tags, "notes": b.notes,
             "added": iso(b.added), "status": b.status, "state": b.state, "favorite": b.favorite} for b in bms]
    return json.dumps({"generator": GENERATOR, "bookmarks": rows, "exported": int(time.time())}, indent=2, ensure_ascii=False)


def _md_text(s: str) -> str:
    return re.sub(r"([\\`*_{}\[\]<>|])", r"\\\1", " ".join(str(s).split()))


def _md_url(u: str) -> str:
    return u.replace(" ", "%20").replace("(", "%28").replace(")", "%29").replace("<", "%3C").replace(">", "%3E")


def write_markdown(bms, folders=()) -> str:
    groups: dict = {}
    for b in bms:
        groups.setdefault(unesc_name(b.folder) if b.folder else "Unfiled", []).append(b)
    out = ["# Bookmarks", ""]
    for folder in sorted(groups, key=str.lower):
        out += [f"## {_md_text(folder)}", ""]
        for b in sorted(groups[folder], key=lambda x: (x.title or x.url).lower()):
            tags = f" `{'` `'.join(b.tags)}`" if b.tags else ""
            note = f" - {_md_text(b.notes)}" if b.notes else ""
            out.append(f"- [{_md_text(b.title or b.url)}]({_md_url(b.url)}){tags}{note}")
        out.append("")
    return "\n".join(out)


def _safe_name(s: str, limit: int = 80) -> str:
    s = re.sub(r'[\\/:*?"<>|#^\[\]\x00-\x1f]', " ", s)
    s = " ".join(s.split()).strip(". ")
    return (s[:limit].strip() or "untitled")


def write_obsidian(bms, out_dir) -> int:
    """One note per bookmark with YAML front matter, filed in subfolders; returns notes written."""
    root = Path(out_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    index: dict = {}
    for b in bms:
        parts = [_safe_name(unesc_name(p), 40) for p in b.folder.split("/") if p]
        while len("/".join(parts)) > 120 and len(parts) > 1:  # very deep trees: keep the innermost folders (full path stays in front matter)
            parts.pop(0)
        folder = root.joinpath(*parts)
        name = f"{_safe_name(b.title or b.url, 60)} ({b.id[:6]}).md"
        path = (folder / name).resolve()
        if root not in path.parents:  # defence in depth against traversal
            continue
        folder.mkdir(parents=True, exist_ok=True)
        fm = {"url": b.url, "title": b.title, "tags": b.tags, "folder": unesc_name(b.folder),
              "added": iso(b.added), "status": b.status}
        lines = ["---"] + [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fm.items() if v not in ("", [], None)]
        lines += ["---", "", f"# {_md_text(b.title or b.url)}", "", f"<{_md_url(b.url)}>", ""]
        if b.notes:
            lines += [b.notes, ""]
        if b.content:
            lines += ["> " + " ".join(b.content[:600].split()), ""]
        path.write_text("\n".join(lines), encoding="utf-8")
        index.setdefault(unesc_name(b.folder) or "Unfiled", []).append(path.stem)
    idx = ["# Bookmarks index", ""]
    for folder in sorted(index, key=str.lower):
        idx += [f"## {_md_text(folder)}", ""] + [f"- [[{n}]]" for n in sorted(index[folder])] + [""]
    (root / "_index.md").write_text("\n".join(idx), encoding="utf-8")
    return sum(len(v) for v in index.values())


# ------------------------------------------------- other services (best effort)
# Written from each service's documented import/export layout; not verified against the live importers. Services
# without nested folders get the folder path as a tag (Pocket, Karakeep) or in the folder column.
def _leaf(folder: str) -> str:
    return unesc_name(folder.rsplit("/", 1)[-1]) if folder else ""


def write_raindrop(bms, folders=()) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["id", "title", "note", "excerpt", "url", "folder", "tags", "created", "cover", "highlights", "favorite"])
    for n, b in enumerate(bms, 1):
        w.writerow([_cell(v) for v in (n, b.title, b.notes, "", b.url, "/".join(unesc_name(p) for p in b.folder.split("/") if p),
                                       ", ".join(b.tags), iso(b.added), "", "", "true" if b.favorite else "false")])
    return buf.getvalue()


def write_pocket(bms, folders=()) -> str:
    """Pocket's own export layout (also read by Raindrop, Wallabag and others)."""
    sections: dict = {"Unread": [], "Read Archive": []}
    for b in bms:
        sections["Read Archive" if b.state == "read" else "Unread"].append(b)
    out = ["<!DOCTYPE html>", '<html><head><meta charset="UTF-8"><title>Pocket Export</title></head><body>']
    for name, items in sections.items():
        out.append(f"<h1>{name}</h1>")
        out.append("<ul>")
        for b in items:
            tag_list = list(b.tags) + ([_leaf(b.folder).lower().replace(" ", "-")] if b.folder else [])
            tags = ",".join(dict.fromkeys(t for t in tag_list if t))
            out.append(f'<li><a href="{html.escape(b.url, quote=True)}" time_added="{b.added or 0}" tags="{html.escape(tags, quote=True)}">'
                       f"{html.escape(b.title or b.url)}</a></li>")
        out.append("</ul>")
    out.append("</body></html>")
    return "\n".join(out) + "\n"


def write_linkwarden(bms, folders=()) -> str:
    """Linkwarden JSON backup: folders become (nested) collections."""
    ids: dict = {}
    collections: list = []

    def collection(path: str):
        if path in ids:
            return collections[ids[path] - 1]
        parent = path.rsplit("/", 1)[0] if "/" in path else None
        pid = collection(parent)["id"] if parent else None
        ids[path] = len(collections) + 1
        col = {"id": ids[path], "name": unesc_name(path.rsplit("/", 1)[-1]), "description": "", "parentId": pid, "links": []}
        collections.append(col)
        return col

    for f in sorted(folders):
        collection(f)
    for b in bms:
        col = collection(b.folder or "Unorganized")
        col["links"].append({"name": b.title, "url": b.url, "description": b.notes, "type": "url",
                             "tags": [{"name": t} for t in b.tags], "createdAt": iso(b.added) or None})
    return json.dumps({"collections": collections}, indent=2, ensure_ascii=False)


def write_karakeep(bms, folders=()) -> str:
    rows = []
    for b in bms:
        tags = list(b.tags) + ([_leaf(b.folder).lower().replace(" ", "-")] if b.folder else [])
        rows.append({"createdAt": b.added or None, "title": b.title or None, "archived": b.state == "read",
                     "favourited": b.favorite, "note": b.notes or None, "tags": list(dict.fromkeys(tags)),
                     "content": {"type": "link", "url": b.url}})
    return json.dumps({"bookmarks": rows}, indent=2, ensure_ascii=False)


def write_instapaper(bms, folders=()) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["URL", "Title", "Selection", "Folder", "Timestamp"])
    for b in bms:
        folder = "Starred" if b.favorite else "Archive" if b.state == "read" else "Unread" if b.state == "unread" else (_leaf(b.folder) or "Unread")
        w.writerow([_cell(v) for v in (b.url, b.title, b.notes, folder, b.added or "")])
    return buf.getvalue()


def render(bms, fmt: str, folders=(), *, icons=None, folder_meta=None) -> str:
    writers: dict = {"html": write_netscape, "chrome": write_chrome, "csv": write_csv,
               "json": write_json, "md": write_markdown, "raindrop": write_raindrop, "pocket": write_pocket,
               "linkwarden": write_linkwarden, "karakeep": write_karakeep, "instapaper": write_instapaper}
    if fmt not in writers:
        raise FormatError(f"unknown export format {fmt!r}; choose from {tuple(writers)}")
    if fmt == "html":
        return write_netscape(bms, folders, icons, folder_meta)
    if fmt == "chrome":
        return write_chrome(bms, folders, folder_meta)
    return writers[fmt](bms, folders)
