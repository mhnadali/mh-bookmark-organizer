"""Two-way sync between the library and a Chromium-family ``Bookmarks`` file.

The browser's file is *patched in place* (nodes are added or removed; ids, GUIDs, dates and everything else stay), the
checksum is recomputed, and a backup is kept. A baseline of what both sides looked like after the previous sync lets
us tell "deleted on this side" from "new on the other side". Identity is (normalised URL, folder path).

Run it with the browser closed: it rewrites the profile file, and a running browser (or its sync) would overwrite it.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import profilesafe
from .formats import (
    CHROME_ROOTS,
    MAX_CHROME_DEPTH,
    WEBKIT_EPOCH_OFFSET,
    chrome_checksum,
    chrome_checksum_sha256,
    chrome_url_ok,
    parse_chrome,
)
from .model import esc_name, unesc_name
from .urlnorm import browser_key

_GUID_NS = uuid.UUID("6b9f3c52-7a41-4f0e-9d6a-2f0f4f0b8c11")
ROOT_BY_NAME = {v: k for k, v in CHROME_ROOTS.items()}
_BROWSER_BLANKS = re.compile("[\t\r\n\u2028\u2029]")


class SyncError(ValueError):
    pass


def browser_folder(folder: str) -> str:
    """Where the browser file keeps a library folder: under its root if the path starts with one, else in "Other bookmarks"
    (the same rule ``write_chrome`` and ``patch_chrome`` apply when they place a bookmark)."""
    folder = _BROWSER_BLANKS.sub(" ", folder)   # Chromium turns tab, CR, LF, U+2028 and U+2029 in a name into a space
    root = folder.split("/", 1)[0]
    if root in ROOT_BY_NAME:
        return folder
    return f"{CHROME_ROOTS['other']}/{folder}" if folder else CHROME_ROOTS["other"]


def key_of(url: str, folder: str) -> str:
    """Identity of a bookmark on both sides. The URL goes through ``browser_key`` (so ``a b`` equals the ``a%20b`` Chromium
    stores) and the folder through ``browser_folder`` (so the library's ``Work`` equals the file's ``Other bookmarks/Work``);
    without both, every such bookmark looked deleted and re-added on the next sync."""
    return f"{browser_key(url)}\u0001{browser_folder(folder)}"


@dataclass
class SyncPlan:
    add_local: list = field(default_factory=list)      # Bookmark objects from the browser
    delete_local: list = field(default_factory=list)   # library Bookmarks deleted in the browser
    add_remote: list = field(default_factory=list)     # library Bookmarks new here
    delete_remote: list = field(default_factory=list)  # keys of browser nodes deleted here
    retitle_local: list = field(default_factory=list)  # (library Bookmark, new title)
    retitle_remote: list = field(default_factory=list)  # (key, new title)
    new_folders_local: set = field(default_factory=set)
    unsyncable: list = field(default_factory=list)     # library Bookmarks whose URL the browser would silently drop: left alone
    collapsed: list = field(default_factory=list)      # library Bookmarks the browser cannot tell from another one (same key): left alone
    unchanged: int = 0

    @property
    def remote_changes(self) -> int:
        return len(self.add_remote) + len(self.delete_remote) + len(self.retitle_remote)

    @property
    def local_changes(self) -> int:
        return len(self.add_local) + len(self.delete_local) + len(self.retitle_local)

    def summary(self) -> dict:
        out = {"add_to_library": len(self.add_local), "delete_from_library": len(self.delete_local),
               "add_to_browser": len(self.add_remote), "delete_from_browser": len(self.delete_remote),
               "retitle_library": len(self.retitle_local), "retitle_browser": len(self.retitle_remote),
               "unchanged": self.unchanged}
        if self.unsyncable:
            out["not_syncable"] = len(self.unsyncable)
        if self.collapsed:
            out["kept_local_same_address"] = len(self.collapsed)
        return out


def _library_index(local, R: dict, plan: SyncPlan) -> dict:
    """key -> the library bookmark that stands for it. URLs the browser would drop go to ``plan.unsyncable``; several library bookmarks that the
    browser cannot tell apart (same key) are represented by one - the one whose title the browser has, else the first - and the rest go to
    ``plan.collapsed``: they stay in the library and are neither pushed to the browser nor deleted along with the representative."""
    plan.unsyncable = [b for b in local if not chrome_url_ok(b.url)]
    skip = {id(b) for b in plan.unsyncable}
    groups: dict = {}
    for b in local:
        if id(b) not in skip:
            groups.setdefault(key_of(b.url, b.folder), []).append(b)
    L = {}
    for key, group in groups.items():
        rb = R.get(key)
        L[key] = next((b for b in group if rb is not None and b.title == rb.title), group[0])
        plan.collapsed += [b for b in group if b is not L[key]]
    return L


def plan_sync(local, remote, baseline: dict, *, prefer: str = "remote", max_delete_frac: float = 0.2, force: bool = False) -> SyncPlan:
    """``local``/``remote``: lists of Bookmark; ``baseline``: key -> title at the last sync (empty on the first sync)."""
    plan = SyncPlan()
    R = {key_of(b.url, b.folder): b for b in remote}
    L = _library_index(local, R, plan)
    for key, lb in L.items():
        rb = R.get(key)
        if rb is not None:
            plan.unchanged += 1
            base = baseline.get(key)
            if lb.title != rb.title:
                local_changed, remote_changed = base is None or lb.title != base, base is None or rb.title != base
                if remote_changed and (not local_changed or prefer == "remote"):
                    plan.retitle_local.append((lb, rb.title))
                elif local_changed:
                    plan.retitle_remote.append((key, lb.title))
        elif key in baseline:
            plan.delete_local.append(lb)   # was on both sides last time, gone from the browser now
        else:
            plan.add_remote.append(lb)
    for key, rb in R.items():
        if key in L:
            continue
        if key in baseline:
            plan.delete_remote.append(key)  # gone from the library since last time
        else:
            plan.add_local.append(rb)
    for rb in plan.add_local:
        parts = rb.folder.split("/") if rb.folder else []
        plan.new_folders_local.update("/".join(parts[: i + 1]) for i in range(len(parts)))
    base_n = max(len(baseline), 1)
    for side, n in (("library", len(plan.delete_local)), ("browser", len(plan.delete_remote))):
        if not force and n > max(10, max_delete_frac * base_n):
            raise SyncError(f"refusing to delete {n} bookmarks from the {side} in one sync (more than "
                            f"{max_delete_frac:.0%} of what was synced before): check the files, or pass --force")
    return plan


# ------------------------------------------------------------ patching the browser file
def _stamp(ts) -> str:
    return str((int(ts) + WEBKIT_EPOCH_OFFSET) * 10**6) if ts else str((int(time.time()) + WEBKIT_EPOCH_OFFSET) * 10**6)


def _max_id(roots) -> int:
    best = 0

    def walk(n):
        nonlocal best
        try:
            best = max(best, int(n.get("id", 0)))
        except (TypeError, ValueError):
            pass
        for c in n.get("children", []):
            walk(c)
    for r in roots.values():
        if isinstance(r, dict):
            walk(r)
    return best


def _index(roots) -> dict:
    """key -> (parent children list, node) for every URL node, using the same folder naming as ``parse_chrome``."""
    out: dict = defaultdict(list)

    def walk(node, path, parent):
        for child in node.get("children", []):
            if child.get("type") == "url":
                out[key_of(child.get("url", ""), "/".join(path))].append((node["children"], child))
            elif child.get("type") == "folder":
                walk(child, path + [esc_name(child.get("name", ""))], node)

    for rkey, root in roots.items():
        if isinstance(root, dict):
            walk(root, [esc_name(CHROME_ROOTS.get(rkey, root.get("name", rkey)))], None)
    return out


def _locate(roots, folder: str, counter, create=True):
    parts = [p for p in folder.split("/") if p]
    if len(parts) > MAX_CHROME_DEPTH:
        raise SyncError(f"the folder {folder[:60]!r} is nested more than {MAX_CHROME_DEPTH} levels deep: Chromium would discard the whole "
                        "bookmarks file at 98. Nothing was changed; flatten that folder first (`restructure --max-depth`).")
    root_key = ROOT_BY_NAME.get(parts[0]) if parts else None
    node = roots[root_key or "other"]
    for part in (parts[1:] if root_key else parts):
        name = unesc_name(part)
        nxt = next((c for c in node["children"] if c.get("type") == "folder" and c.get("name") == name), None)
        if nxt is None:
            if not create:
                return None
            nid = str(next(counter))
            nxt = {"id": nid, "name": name, "type": "folder", "date_added": _stamp(0), "date_last_used": "0", "date_modified": _stamp(0),
                   "guid": str(uuid.uuid5(_GUID_NS, f"sync-folder|{nid}|{folder}|{name}")), "children": []}
            node["children"].append(nxt)
        node = nxt
    return node


def patch_chrome(data: dict, plan: SyncPlan) -> dict:
    """Apply the browser-side part of ``plan`` to a parsed ``Bookmarks`` document (returned, modified in place)."""
    roots = data.get("roots")
    if not isinstance(roots, dict) or any(not isinstance(roots.get(key), dict) for key in CHROME_ROOTS):
        raise SyncError("the browser file is missing one of its three root folders (bookmark bar, other, mobile); run `verify-chrome` on it. "
                        "Nothing was changed.")
    counter = iter(range(_max_id(roots) + 1, 10**9))
    index = _index(roots)
    for key in plan.delete_remote:
        hits = index.get(key)
        if hits:
            children, node = hits.pop(0)
            children.remove(node)
    for key, title in plan.retitle_remote:
        hits = index.get(key)
        if hits:
            hits[0][1]["name"] = title
    for b in plan.add_remote:
        if not chrome_url_ok(b.url):
            continue
        nid = str(next(counter))
        _locate(roots, b.folder, counter)["children"].append({
            "id": nid, "name": b.title, "type": "url", "url": b.url, "date_added": _stamp(b.added), "date_last_used": "0",
            "guid": str(uuid.uuid5(_GUID_NS, f"sync-url|{nid}|{b.url}"))})
    data["checksum"] = chrome_checksum(roots)
    if "checksum_sha256" in data:   # written by builds with kEnableBookmarkCodecSHA256: keep it true, never stale
        data["checksum_sha256"] = chrome_checksum_sha256(roots)
    return data


def read_remote(path: Path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))   # Chromium itself accepts a BOM
    return data, list(parse_chrome(data))


def write_remote(path: Path, data: dict) -> Path:
    """Back the file up (a timestamped copy that is never overwritten), then replace it atomically; returns the backup."""
    return profilesafe.write_profile_file(Path(path), json.dumps(data, indent=3, ensure_ascii=False))["backup"]


def write_text_atomic(path: Path, text: str) -> None:
    """Temp file + fsync + rename, keeping the target's permissions and owner (see ``profilesafe``)."""
    profilesafe.atomic_write_text(Path(path), text)


def browser_running(profile_file: Path) -> bool:
    """True when a browser may be running on the profile that owns ``profile_file`` (see ``profilesafe.chromium_lock``)."""
    return profilesafe.chromium_lock(Path(profile_file)).blocks
