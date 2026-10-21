"""Chrome/Edge/Brave *native messaging* host for the mh-bookmark-organizer browser extension.

Protocol: each message is a 4-byte native-endian length followed by UTF-8 JSON, on stdin/stdout. Requests carry an
``id`` and a ``cmd``; replies are ``{"id", "ok": true, "result": ...}`` or ``{"id", "ok": false, "error": ...}``.
The host only ever talks to the library it was started with (``--home``) and to nothing else on the machine.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

from . import __version__, autotag, categorize, feedback, learn
from . import rules as rules_mod
from .model import Bookmark, assign_ids, clean_folder, clean_tags
from .store import Library
from .urlnorm import normalize_url

MAX_IN = 64 * 1024 * 1024   # a whole bookmark tree can be large
MAX_OUT = 1024 * 1024       # browsers refuse host -> extension messages above 1 MB
HOST_NAME = "com.mhbm.organizer"


def read_message(stream):
    head = stream.read(4)
    if len(head) < 4:
        return None
    (n,) = struct.unpack("=I", head)
    if n > MAX_IN:
        raise ValueError(f"message of {n} bytes refused")
    body = stream.read(n)
    if len(body) < n:
        return None
    return json.loads(body.decode("utf-8"))


def write_message(stream, obj) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_OUT:
        data = json.dumps({"id": obj.get("id"), "ok": False, "error": "reply too large; ask for fewer results"}).encode("utf-8")
    stream.write(struct.pack("=I", len(data)))
    stream.write(data)
    stream.flush()


class Host:
    def __init__(self, home):
        self.home = Path(home)
        self.lib = Library(self.home)
        self._model = None
        self._vocab = None
        self._bms = None

    def close(self):
        self.lib.close()

    # ---- cached views of the library; dropped whenever we change it
    def _invalidate(self):
        self._model = self._vocab = self._bms = None

    def bookmarks(self):
        if self._bms is None:
            self._bms = self.lib.all()
        return self._bms

    def model(self):
        if self._model is None:
            self._model = learn.FolderModel(self.bookmarks(), cache=self.home / "model.json")
            self._model.set_outcomes(feedback.outcomes(self.lib))
        return self._model

    # ---- commands
    def handle(self, msg: dict) -> dict:
        mid = msg.get("id")
        try:
            fn = getattr(self, "cmd_" + str(msg.get("cmd", "")).replace("-", "_"), None)
            if fn is None:
                raise ValueError(f"unknown command {msg.get('cmd')!r}")
            return {"id": mid, "ok": True, "result": fn(msg)}
        except (ValueError, KeyError, TypeError) as e:
            return {"id": mid, "ok": False, "error": str(e)}

    def cmd_ping(self, msg):
        return {"version": __version__, "bookmarks": self.lib.count()}

    def cmd_folders(self, msg):
        return sorted(self.lib.folders() | self.lib.used_folders())

    def cmd_search(self, msg):
        hits = self.lib.search(str(msg.get("query", "")), min(int(msg.get("limit", 15)), 50))
        return [{"id": b.id, "title": b.title, "url": b.url, "folder": b.folder, "tags": b.tags} for b, _ in hits]

    def cmd_suggest(self, msg):
        """Where would this page be filed? Rules first, then the model; always returns candidates to choose from."""
        url, title = str(msg["url"]), str(msg.get("title", ""))
        probe = Bookmark(url=url, title=title, added=int(msg.get("added", 0)))
        known = self.lib.folders() | self.lib.used_folders()
        rule = rules_mod.apply(probe, rules_mod.load(self.home), known)
        model = self.model()
        probe.norm = normalize_url(url)
        skip = feedback.rejections(self.lib).get(probe.norm, ())   # folders already turned down for this very page
        guess = model.predict(probe, 0.5, exclude=skip) if model.ready else None
        alts = [{"folder": f, "score": round(p, 2)} for f, p in model.rank(probe, 4, skip)] if model.ready else []
        if self._vocab is None:
            self._vocab = autotag.Vocab(self.bookmarks())
        tags = [t for t, _, _ in autotag.suggest(probe, self._vocab, max_tags=3)]
        best = rule or (guess and {"folder": guess["folder"], "confidence": guess["confidence"], "level": guess["level"],
                                   "source": "learned", "tags": guess["tags"]})
        return {"folder": best["folder"] if best else (alts[0]["folder"] if alts else ""), "source": best["source"] if best else "weak",
                "confidence": best["confidence"] if best else None, "level": best.get("level", "exact") if best else "exact",
                "alternatives": alts, "tags": best["tags"] if best and best.get("tags") else tags}

    def cmd_save(self, msg):
        folder = categorize.resolve_folder(str(msg.get("folder", "")), self.lib.folders() | self.lib.used_folders())
        b = Bookmark(url=str(msg["url"]), title=str(msg.get("title", "")), folder=folder, tags=clean_tags(msg.get("tags", [])),
                     notes=str(msg.get("notes", "")), added=int(msg.get("added", 0)))
        b.norm = normalize_url(b.url)
        assign_ids([b], self.lib.ids())
        with self.lib.op("extension-save"):
            self.lib.upsert([b])
            if folder:
                self.lib.add_folders([folder])
            if isinstance(msg.get("suggested"), str) and msg["suggested"]:  # the popup echoes what it proposed: judge the outcome
                conf = msg.get("confidence")
                feedback.settle(self.lib, b, folder, "extension", suggested=msg["suggested"],
                                confidence=float(conf) if isinstance(conf, (int, float)) else None,
                                level=str(msg.get("level") or "exact"), source=str(msg.get("source") or "learned"))
        self._invalidate()
        return {"id": b.id, "folder": folder}

    def cmd_import_tree(self, msg):
        """Add the browser's bookmarks that the library does not have yet (same URL in the same folder)."""
        have = {(b.norm or normalize_url(b.url), b.folder) for b in self.bookmarks()}
        fresh, seen = [], set()
        for n in msg["nodes"]:
            b = Bookmark(url=str(n["url"]), title=str(n.get("title", "")), folder=clean_folder(str(n.get("folderPath", "")), strip=False),
                         added=int(n.get("dateAdded", 0)) // 1000)
            b.norm = normalize_url(b.url)
            key = (b.norm, b.folder)
            if key not in have and key not in seen:
                seen.add(key)
                fresh.append(b)
        assign_ids(fresh, self.lib.ids())
        with self.lib.op("extension-import"):
            self.lib.upsert(fresh)
            self.lib.add_folders({f for b in fresh for f in _prefixes(b.folder)})
        self._invalidate()
        return {"read": len(msg["nodes"]), "imported": len(fresh)}

    def cmd_plan(self, msg):
        """Suggested moves for unfiled bookmarks, as (url, from, to) so the extension can find the nodes itself."""
        bms = self.bookmarks()
        plan = categorize.propose(bms, categorize.load_taxonomy(), self.model(), min_conf=float(msg.get("precision", 0.85)),
                                  rules=rules_mod.load(self.home), known_folders=self.lib.folders() | self.lib.used_folders(),
                                  exclude=feedback.exclusions_for(self.lib, bms))
        feedback.issue(self.lib, plan, bms)
        by_id = {b.id: b for b in bms}
        moves = [{"url": by_id[i].url, "from": by_id[i].folder, "to": c["folder"], "tags": c.get("tags", []),
                  "confidence": c.get("confidence"), "level": c.get("level", "exact"), "reason": c.get("reason", "")}
                 for i, c in plan.items() if i in by_id]
        return {"moves": moves[: int(msg.get("limit", 400))], "total": len(moves)}

    def cmd_apply_moves(self, msg):
        """Record moves the extension performed in the browser (updates folders and tags in the library)."""
        index = {}
        for b in self.bookmarks():
            index.setdefault((b.norm or normalize_url(b.url), b.folder), b)
        changed = []
        for m in msg["moves"]:
            b = index.get((normalize_url(str(m["url"])), clean_folder(str(m.get("from", "")), strip=False)))
            if b is not None:
                b.folder = clean_folder(str(m["to"]), strip=False)
                b.tags = clean_tags(b.tags + list(m.get("tags", [])))
                changed.append(b)
        with self.lib.op("extension-moves"):
            self.lib.upsert(changed)
            self.lib.add_folders({f for b in changed for f in _prefixes(b.folder)})
            for b in changed:  # pressing "apply" on a list is not a look at each item: recorded, not counted as evidence
                feedback.settle(self.lib, b, b.folder, "extension-moves")
        self._invalidate()
        return {"updated": len(changed)}


def _prefixes(folder: str):
    parts = folder.split("/") if folder else []
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def serve(home, stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    host = Host(home)
    try:
        while True:
            try:
                msg = read_message(stdin)
            except (ValueError, json.JSONDecodeError) as e:
                write_message(stdout, {"id": None, "ok": False, "error": f"bad message: {e}"})
                continue
            if msg is None:
                break
            write_message(stdout, host.handle(msg))
    finally:
        host.close()
