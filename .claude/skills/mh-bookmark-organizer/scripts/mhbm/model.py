"""Bookmark data model and folder-path helpers."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .urlnorm import normalize_url

ROOT_NAMES = ("Bookmarks bar", "Other bookmarks", "Mobile bookmarks", "Bookmarks menu")
FOLDER_SLASH = "∕"  # stands in for a literal "/" inside a folder *name*
READ_STATES = ("unread", "reading", "read")


def esc_name(name: str) -> str:
    """Folder name safe to use as one path component. Whitespace is kept: names are the user's, not ours to rewrite."""
    name = str(name)
    return name.replace("/", FOLDER_SLASH) if name.strip() else ""


def unesc_name(name: str) -> str:
    return name.replace(FOLDER_SLASH, "/")


def join_path(names) -> str:
    return "/".join(esc_name(n) for n in names if n and str(n).strip())


_TAG_SYNONYMS: dict = {}
_FOLDER_SYNONYMS: dict = {}


def set_tag_synonyms(mapping: dict) -> None:
    """alias -> canonical tag, applied by every ``clean_tags`` call."""
    _TAG_SYNONYMS.clear()
    _TAG_SYNONYMS.update(mapping)


def set_folder_synonyms(mapping: dict) -> None:
    """lower-case alias path -> canonical path, used when resolving folder names."""
    _FOLDER_SYNONYMS.clear()
    _FOLDER_SYNONYMS.update(mapping)


def folder_synonym(path: str) -> str:
    return _FOLDER_SYNONYMS.get(path.lower(), path)


def clean_folder(folder: str, strip: bool = True) -> str:
    """Normalise a folder path typed by a user, a CSV or an AI plan (``strip`` trims each component).

    Imports from structured formats pass ``strip=False`` so existing folder names survive exactly.
    """
    parts = str(folder or "").replace("\\", "/").split("/")
    if strip:
        parts = [p.strip() for p in parts]
    return "/".join(p for p in parts if p.strip() and p.strip() not in (".", ".."))


def clean_tags(tags) -> list:
    if isinstance(tags, str):
        tags = tags.replace(";", ",").split(",")
    seen, out = set(), []
    for t in tags or []:
        t = str(t).strip().lower().replace(" ", "-")
        t = _TAG_SYNONYMS.get(t, t)
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def url_id(url: str, n: int = 0) -> str:
    return hashlib.sha1(f"{normalize_url(url)}|{n}".encode()).hexdigest()[:12]


@dataclass
class Bookmark:
    url: str
    title: str = ""
    folder: str = ""  # "/"-separated path, "" = unfiled
    tags: list = field(default_factory=list)
    notes: str = ""
    added: int = 0  # unix seconds, 0 = unknown
    id: str = ""
    status: str = ""  # ok | redirect | suspect | dead | restricted | skipped | ""
    http_code: int = 0
    last_checked: int = 0
    fail_count: int = 0
    first_failed: int = 0
    wayback: str = ""
    archive_path: str = ""
    content: str = ""  # page text (a 2000-char snippet when loaded in bulk)
    meta: dict = field(default_factory=dict)  # canonical, published, author, lang
    norm: str = ""
    icon: str = ""  # favicon data URI from an import; kept in its own table, never loaded in bulk
    state: str = ""  # reading list: "" (not tracked) | unread | reading | read
    favorite: bool = False

    def __post_init__(self):
        self.tags = clean_tags(self.tags)
        self.state = self.state if self.state in READ_STATES else ""
        self.favorite = bool(self.favorite)
        self.folder = clean_folder(self.folder, strip=False)
        if not self.id:
            self.id = url_id(self.url)


def is_unfiled(b: Bookmark) -> bool:
    """Unfiled, or dumped in a browser's top-level wrapper folder."""
    return b.folder == "" or b.folder in ROOT_NAMES


def assign_ids(new, taken) -> None:
    """Give every new bookmark a deterministic, collision-free id (the nth copy of a URL gets suffix n)."""
    taken = set(taken)
    for b in new:
        n = 0
        while url_id(b.url, n) in taken:
            n += 1
        b.id = url_id(b.url, n)
        taken.add(b.id)
