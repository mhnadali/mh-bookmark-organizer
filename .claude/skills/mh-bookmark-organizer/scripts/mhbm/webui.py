"""A local web UI to browse and edit the library, review plans and undo - served on 127.0.0.1 only.

Security model (it edits your data, so it is strict):
* bound to the loopback interface; random port and a random per-run token;
* the token travels in the URL *fragment* (never sent to the server or logged) and must be sent as a header on every API call;
* the ``Host`` header must be the loopback address we listen on (blocks DNS-rebinding), a present ``Origin`` must match it;
* state-changing requests must be ``application/json`` (blocks cross-site form posts); no CORS headers are ever sent;
* a strict Content-Security-Policy; the page loads nothing from anywhere else.
"""
from __future__ import annotations

import hmac
import json
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import autotag, canonical, categorize, feedback, formats, learn, report, restructure, triage
from . import dedupe as dedupe_mod
from . import rules as rules_mod
from . import titles as titles_mod
from .model import READ_STATES, clean_folder, clean_tags
from .store import Library

MAX_BODY = 20 * 1024 * 1024
PAGE_SIZE_MAX = 500


def _row(b):
    return {"id": b.id, "title": b.title, "url": b.url, "folder": b.folder, "tags": b.tags, "notes": b.notes, "added": b.added,
            "status": b.status, "state": b.state, "favorite": b.favorite}


class UIServer(HTTPServer):
    def __init__(self, home, host="127.0.0.1", port=0, token=None):
        super().__init__((host, port), Handler)
        self.home = Path(home)
        self.token = token or secrets.token_urlsafe(24)
        self.lib: Library | None = None  # opened lazily, in the thread that serves requests
        self.allowed_hosts = {f"127.0.0.1:{self.server_address[1]}", f"localhost:{self.server_address[1]}"}

    def library(self) -> Library:
        if self.lib is None:
            self.lib = Library(self.home, check_same_thread=False)  # one serving thread; closed after it stops
        return self.lib

    def server_close(self):
        if self.lib is not None:
            self.lib.close()
            self.lib = None
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server: UIServer  # narrowed from BaseServer: this handler only ever serves a UIServer
    server_version = "mhbm-ui"

    def log_message(self, *args):  # quiet; the URL (with token) must never end up in a log
        pass

    # ----------------------------------------------------------- plumbing
    def _send(self, code, body: bytes, ctype="application/json", extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; connect-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _error(self, code, message):
        self._json({"error": message}, code)

    def _guard(self, api: bool) -> bool:
        host = self.headers.get("Host", "")
        if host not in self.server.allowed_hosts:
            self._error(403, "bad Host header")
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{h}" for h in self.server.allowed_hosts}:
            self._error(403, "bad Origin")
            return False
        if api and not hmac.compare_digest(self.headers.get("X-MHBM-Token", ""), self.server.token):
            self._error(403, "missing or wrong token")
            return False
        return True

    def _body(self):
        if "application/json" not in self.headers.get("Content-Type", ""):
            self._error(415, "send application/json")
            return None
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            self._error(413, "body too large")
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._error(400, "invalid JSON")
            return None

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path in ("/", "/index.html"):
            if self._guard(False):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if not self._guard(True):
            return
        self._dispatch("GET", url.path, parse_qs(url.query), None)

    def do_POST(self):
        if not self._guard(True):
            return
        body = self._body()
        if body is not None:
            self._dispatch("POST", urlsplit(self.path).path, {}, body)

    def do_PATCH(self):
        self.do_POST_like("PATCH")

    def do_POST_like(self, method):
        if not self._guard(True):
            return
        body = self._body()
        if body is not None:
            self._dispatch(method, urlsplit(self.path).path, {}, body)

    def _dispatch(self, method, path, query, body):
        try:
            routes = {("GET", "/api/state"): self.api_state, ("GET", "/api/bookmarks"): self.api_list, ("GET", "/api/plan"): self.api_plan,
                      ("POST", "/api/apply"): self.api_apply, ("POST", "/api/undo"): self.api_undo, ("GET", "/api/history"): self.api_history,
                      ("POST", "/api/bulk"): self.api_bulk, ("GET", "/api/export"): self.api_export,
                      ("GET", "/api/triage"): self.api_triage, ("POST", "/api/triage"): self.api_triage_decide}
            if method == "PATCH" and path.startswith("/api/bookmark/"):
                return self.api_edit(path.rsplit("/", 1)[-1], body)
            fn = routes.get((method, path))
            if fn is None:
                return self._error(404, "not found")
            return fn(query) if method == "GET" else fn(body)
        except (ValueError, KeyError, TypeError) as e:
            return self._error(400, str(e))

    # -------------------------------------------------------------- API
    def api_state(self, q):
        lib = self.server.library()
        bms = lib.all(snippet=0)
        r = report.build(bms, lib.folders())
        tags: dict = {}
        for b in bms:
            for t in b.tags:
                tags[t] = tags.get(t, 0) + 1
        folders: dict = {}
        for b in bms:
            folders[b.folder] = folders.get(b.folder, 0) + 1
        self._json({"total": r["total"], "unfiled": r["unfiled"], "untagged": r["untagged"], "duplicates": r["duplicate_extras"],
                    "status": r["status"], "folders": folders, "all_folders": sorted(lib.folders() | lib.used_folders()),
                    "tags": dict(sorted(tags.items(), key=lambda kv: -kv[1])[:200]), "read_states": READ_STATES})

    def api_list(self, q):
        lib = self.server.library()
        limit = min(int(q.get("limit", ["100"])[0]), PAGE_SIZE_MAX)
        query = q.get("q", [""])[0].strip()
        offset = max(int(q.get("offset", ["0"])[0]), 0)
        if query:
            rows = [_row(b) for b, _ in lib.search(query, limit + offset)][offset:]
        else:
            allb = sorted(lib.all(snippet=0), key=lambda b: (b.folder.lower(), (b.title or b.url).lower()))
            rows = [_row(b) for b in allb[offset: offset + limit]]
        self._json({"items": rows})

    def api_edit(self, bid, body):
        lib = self.server.library()
        b = lib.get(bid)
        if b is None:
            return self._error(404, "no such bookmark")
        if "title" in body:
            b.title = str(body["title"])[:300]
        if "notes" in body:
            b.notes = str(body["notes"])[:5000]
        if "folder" in body:
            b.folder = categorize.resolve_folder(str(body["folder"]), lib.folders() | lib.used_folders()) if body["folder"] else ""
        if "tags" in body:
            b.tags = clean_tags(body["tags"] if isinstance(body["tags"], list) else str(body["tags"]))
        if "state" in body:
            if body["state"] not in ("",) + READ_STATES:
                return self._error(400, "bad state")
            b.state = body["state"]
        if "favorite" in body:
            b.favorite = bool(body["favorite"])
        with lib.op("ui-edit"):
            lib.upsert([b])
            if b.folder:
                lib.add_folders([b.folder])
        self._json(_row(b))

    def api_bulk(self, body):
        lib = self.server.library()
        ids = [str(i) for i in body.get("ids", [])][:5000]
        bms = [b for b in (lib.get(i) for i in ids) if b]
        known = lib.folders() | lib.used_folders()
        if body.get("delete") is True:
            with lib.op("ui-delete"):
                lib.delete(b.id for b in bms)
            return self._json({"deleted": len(bms)})
        for b in bms:
            if "folder" in body:
                b.folder = categorize.resolve_folder(str(body["folder"]), known) if body["folder"] else ""
            if body.get("add_tags"):
                b.tags = clean_tags(b.tags + list(body["add_tags"]))
            if body.get("remove_tags"):
                drop = set(clean_tags(body["remove_tags"]))
                b.tags = [t for t in b.tags if t not in drop]
            if "state" in body and body["state"] in ("",) + READ_STATES:
                b.state = body["state"]
        with lib.op("ui-bulk"):
            lib.upsert(bms)
            lib.add_folders({b.folder for b in bms if b.folder})
        self._json({"updated": len(bms)})

    def api_plan(self, q):
        kind = q.get("kind", ["organize"])[0]
        lib = self.server.library()
        bms = lib.all()
        if kind == "organize":
            model = learn.FolderModel(bms, cache=lib.root / "model.json")
            model.set_outcomes(feedback.outcomes(lib))
            plan = categorize.propose(bms, categorize.load_taxonomy(), model, rules=rules_mod.load(lib.root),
                                      min_conf=float(q.get("precision", ["0.85"])[0]),
                                      known_folders=lib.folders() | lib.used_folders(), exclude=feedback.exclusions_for(lib, bms))
            feedback.issue(lib, plan, bms)
        elif kind == "autotag":
            plan = autotag.propose(bms)
        elif kind == "titles":
            plan = titles_mod.propose(bms)
        elif kind == "canonical":
            plan, _ = canonical.propose(bms)
        elif kind == "restructure":
            plan = restructure.to_plan(restructure.analyse(bms, model=learn.FolderModel(bms, cache=lib.root / "model.json")))
        elif kind == "dedupe":
            _, removed, groups = dedupe_mod.dedupe(bms, "variant")
            gone = {id(b) for b in removed}
            plan = {}
            for g in groups:
                keeper = next(b for b in g if id(b) not in gone)
                plan[keeper.id] = {"tags": keeper.tags, "notes": keeper.notes, "reason": "kept (best of group)"}
                for b in g:
                    if id(b) in gone:
                        plan[b.id] = {"delete": True, "reason": f"duplicate of {keeper.id}"}
        else:
            return self._error(400, "unknown plan kind")
        by_id = {b.id: b for b in bms}
        self._json({"kind": kind, "plan": plan, "bookmarks": {i: {"title": by_id[i].title, "url": by_id[i].url, "folder": by_id[i].folder}
                                                              for i in plan if i in by_id}})

    def api_apply(self, body):
        lib = self.server.library()
        plan = body.get("plan")
        if not isinstance(plan, dict) or not plan:
            return self._error(400, "send a non-empty plan")
        bms = lib.all()
        reviewed = {i for i in body.get("reviewed", []) if isinstance(i, str)}   # items the person actually looked at
        for i in reviewed:
            if isinstance(plan.get(i), dict):
                plan[i]["reviewed"] = True
        declined = [i for i in body.get("declined", []) if isinstance(i, str)]
        by_id = {b.id: b for b in bms}
        res = categorize.apply_plan(bms, plan, max_depth=99, known_folders=lib.folders() | lib.used_folders())
        with lib.op("ui-" + str(body.get("kind", "plan"))[:20]):
            lib.upsert(res.changed)
            lib.delete(b.id for b in res.deleted)
            lib.add_folders(lib.used_folders())
            counts = {}
            if body.get("kind") == "organize":
                res.declined += [(i, {"reviewed": True}) for i in declined if i in by_id and i not in plan]
                counts = feedback.settle_plan(lib, res, plan, by_id, "ui-unreviewed", "ui")
        self._json({"changed": len(res.changed), "deleted": len(res.deleted), "rejected": res.rejected[:20], "feedback": counts})

    def _clusters(self, lib, bms):
        model = learn.FolderModel(bms, cache=lib.root / "model.json")
        model.set_outcomes(feedback.outcomes(lib))
        known = lib.folders() | lib.used_folders()
        return model, triage.build(bms, model, rules=rules_mod.load(lib.root), known_folders=known, exclude=feedback.exclusions_for(lib, bms))

    def api_triage(self, q):
        lib = self.server.library()
        bms = lib.all()
        _, clusters = self._clusters(lib, bms)
        limit = min(int(q.get("limit", ["200"])[0]), 500)
        self._json({"unfiled": sum(len(c.members) for c in clusters), "decisions": len(clusters),
                    "clusters": [c.to_json(limit=30) for c in clusters[:limit]]})

    def api_triage_decide(self, body):
        """One decision per cluster: ``{"cluster": id, "action": "file"|"reject"|"skip", "folder": "...", "exclude": [ids]}``."""
        lib = self.server.library()
        bms = lib.all()
        _, clusters = self._clusters(lib, bms)
        by_id = {c.id: c for c in clusters}
        decisions = body.get("decisions") if isinstance(body.get("decisions"), list) else [body]
        result = {"filed": 0, "rejected": 0, "skipped": 0, "errors": []}
        known = lib.folders() | lib.used_folders()
        with lib.op("ui-triage"):
            for d in decisions:
                if not isinstance(d, dict) or d.get("cluster") not in by_id:
                    result["errors"].append("unknown or outdated group; reload")
                    continue
                action = d.get("action", "file")
                folder = clean_folder(str(d.get("folder") or by_id[d["cluster"]].folder), strip=False)
                if action not in ("file", "reject", "skip") or (action == "file" and (not folder or len(folder) > 300)):
                    result["errors"].append("a folder is needed to file a group")
                    continue
                exclude = [i for i in d.get("exclude", []) if isinstance(i, str)] if isinstance(d.get("exclude", []), list) else []
                res = triage.decide(lib, by_id[d["cluster"]], folder, exclude_ids=exclude, channel="ui", action=action, known=known)
                result["filed" if res["action"] == "file" else "rejected" if res["action"] == "reject" else "skipped"] += (
                    res["filed"] if res["action"] == "file" else 1)
        self._json(result)

    def api_undo(self, body):
        lib = self.server.library()
        try:
            res = lib.undo(int(body.get("op", 0)) if body else 0)
        except ValueError as e:
            return self._error(409, str(e))
        lib.add_folders(lib.used_folders())
        self._json(res)

    def api_history(self, q):
        self._json({"operations": self.server.library().history(30)})

    def api_export(self, q):
        lib = self.server.library()
        fmt = q.get("format", ["html"])[0]
        if fmt not in formats.WRITE_FORMATS or fmt == "obsidian":
            return self._error(400, "unsupported format")
        bms = sorted(lib.all(snippet=0), key=lambda b: (b.folder.lower(), (b.title or b.url).lower()))
        text = formats.render(bms, fmt, lib.folders(), icons=lib.icons() if fmt == "html" else None,
                              folder_meta=lib.folder_meta() if fmt in ("html", "chrome") else None)
        ctype = {"html": "text/html", "json": "application/json", "chrome": "application/json", "linkwarden": "application/json",
                 "karakeep": "application/json", "md": "text/markdown"}.get(fmt, "text/csv")
        self._send(200, text.encode("utf-8"), ctype + "; charset=utf-8", [("Content-Disposition", f'attachment; filename="bookmarks.{fmt}"')])


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Bookmark library</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--ink:#1c2330;--mute:#667085;--line:#e3e6eb;--acc:#2563eb;--bad:#dc2626;--ok:#16a34a}
@media (prefers-color-scheme:dark){:root{--bg:#0f1319;--panel:#171c24;--ink:#e6e9ee;--mute:#98a2b3;--line:#262d38;--acc:#6ea0ff;--bad:#f87171;--ok:#4ade80}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,sans-serif}
header{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:10px 16px;background:var(--panel);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:3}
input,select,textarea,button{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:7px;padding:6px 10px}button{cursor:pointer;background:var(--panel)}button.primary{background:var(--acc);border-color:var(--acc);color:#fff}
main{display:grid;grid-template-columns:260px 1fr;gap:14px;padding:14px 16px;max-width:1400px;margin:auto}@media(max-width:850px){main{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:12px}h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute);margin:0 0 6px}
.row{display:flex;gap:6px;align-items:center;padding:6px 0;border-bottom:1px solid var(--line)}.row .t{flex:1;min-width:0;overflow-wrap:anywhere}.meta{color:var(--mute);font-size:12px}
.folder{cursor:pointer;display:flex;justify-content:space-between;padding:2px 4px;border-radius:5px}.folder:hover{background:var(--bg)}
#msg{color:var(--mute)}.tabs button.on{background:var(--acc);color:#fff;border-color:var(--acc)}dialog{border:1px solid var(--line);border-radius:12px;background:var(--panel);color:var(--ink);width:min(560px,92vw)}
dialog label{display:block;margin:8px 0}dialog input,dialog textarea,dialog select{width:100%}
</style></head><body>
<header><strong>Bookmark library</strong><input id="q" type="search" placeholder='search: words "phrase" tag: folder: site: is:unfiled…' style="flex:1;min-width:200px">
<span class="tabs"><button id="tLib" class="on">Library</button><button id="tPlan">Review plan</button><button id="tTri">Triage</button></span>
<button id="undo">Undo</button><select id="exp"><option value="">Export…</option><option>html</option><option>chrome</option><option>csv</option><option>json</option><option>md</option><option>linkwarden</option><option>raindrop</option><option>karakeep</option><option>pocket</option><option>instapaper</option></select></header>
<main><aside><div class="panel"><h2>Folders</h2><div id="folders"></div></div><div class="panel"><h2>Tags</h2><div id="tags"></div></div></aside>
<section><div class="panel" id="summary"></div>
<div id="libView"><div class="panel"><div class="row"><input id="bulkFolder" list="fl" placeholder="move selected to folder…"><button id="bulkMove">Move</button><input id="bulkTag" placeholder="add tag"><button id="bulkTagBtn">Tag</button><button id="bulkDel">Delete</button></div><div id="list"></div><p><button id="more">Load more</button> <span id="msg"></span></p></div></div>
<div id="triView" hidden><div class="panel"><div class="row" style="border:0"><button id="triReload">Reload</button><span class="meta">Unfiled bookmarks grouped by site and by when you saved them: one decision per group. Untick a bookmark to set it aside.</span></div><div id="triList"></div></div></div>
<div id="planView" hidden><div class="panel"><div class="row"><select id="kind"><option>organize</option><option>autotag</option><option>titles</option><option>canonical</option><option>dedupe</option><option>restructure</option></select><button id="load">Compute plan</button><button id="selAll">All</button><button id="selNone">None</button><button id="apply" class="primary">Apply selected</button></div><div id="planList"></div></div></div></section></main>
<datalist id="fl"></datalist>
<dialog id="dlg"><form method="dialog"><h2>Edit bookmark</h2><label>Title <input id="eTitle"></label><label>Folder <input id="eFolder" list="fl"></label><label>Tags <input id="eTags"></label><label>Notes <textarea id="eNotes" rows="3"></textarea></label>
<label>Reading state <select id="eState"><option value="">none</option><option>unread</option><option>reading</option><option>read</option></select></label><label><input type="checkbox" id="eFav" style="width:auto"> favourite</label>
<div class="row" style="border:0"><button value="cancel">Cancel</button><button id="eSave" value="ok" class="primary">Save</button></div></form></dialog>
<script>
"use strict";
const TOKEN = (location.hash.match(/token=([\w-]+)/) || [])[1] || ""; history.replaceState(null, "", location.pathname);
const $ = id => document.getElementById(id);
const el = (t, a, ...k) => { const e = document.createElement(t); for (const x in a||{}) { if (x === "text") e.textContent = a[x]; else if (x === "class") e.className = a[x]; else e.setAttribute(x, a[x]); } k.forEach(c => c && e.append(c)); return e; };
async function api(path, method = "GET", body) {
  const r = await fetch(path, {method, headers: {"X-MHBM-Token": TOKEN, ...(body ? {"Content-Type": "application/json"} : {})}, body: body ? JSON.stringify(body) : undefined});
  const data = await r.json().catch(() => ({})); if (!r.ok) throw new Error(data.error || r.statusText); return data;
}
let items = [], offset = 0, selected = new Set(), plan = null, planSel = new Set(), editing = null;
const say = t => { $("msg").textContent = t; };
async function loadState() {
  const s = await api("/api/state");
  $("summary").textContent = `${s.total} bookmarks · ${s.unfiled} unfiled · ${s.untagged} untagged · ${s.duplicates} duplicates`;
  $("fl").replaceChildren(...s.all_folders.map(f => el("option", {value: f})));
  const fb = $("folders"); fb.replaceChildren();
  Object.entries(s.folders).sort().forEach(([f, n]) => { const r = el("div", {class: "folder"}, el("span", {text: f ? f.split("/").slice(-2).join("/") : "(unfiled)", title: f}), el("small", {text: n})); r.onclick = () => { $("q").value = f ? 'folder:"' + f + '"' : "is:unfiled"; reload(); }; fb.append(r); });
  const tb = $("tags"); tb.replaceChildren(...Object.entries(s.tags).slice(0, 60).map(([t, n]) => { const b = el("button", {text: "#" + t + " " + n}); b.onclick = () => { $("q").value = "tag:" + t; reload(); }; return b; }));
}
async function reload() { offset = 0; items = []; selected.clear(); $("list").replaceChildren(); await more(); }
async function more() {
  const r = await api(`/api/bookmarks?limit=100&offset=${offset}&q=${encodeURIComponent($("q").value)}`); offset += r.items.length; items.push(...r.items);
  r.items.forEach(b => { const cb = el("input", {type: "checkbox"}); cb.onchange = () => cb.checked ? selected.add(b.id) : selected.delete(b.id);
    const t = el("div", {class: "t", dir: "auto"}, el("a", {href: /^https?:/i.test(b.url) ? b.url : "#", target: "_blank", rel: "noopener noreferrer", text: b.title || b.url}), el("div", {class: "meta", text: [b.folder || "unfiled", b.tags.map(x => "#" + x).join(" "), b.state, b.favorite ? "★" : ""].filter(Boolean).join(" · ")}));
    const e = el("button", {text: "Edit"}); e.onclick = () => edit(b); $("list").append(el("div", {class: "row"}, cb, t, e)); });
  $("more").hidden = r.items.length < 100; say(items.length + " shown");
}
function edit(b) { editing = b; $("eTitle").value = b.title; $("eFolder").value = b.folder; $("eTags").value = b.tags.join(", "); $("eNotes").value = b.notes || ""; $("eState").value = b.state || ""; $("eFav").checked = b.favorite; $("dlg").showModal(); }
$("eSave").onclick = async () => { if (!editing) return; try { await api("/api/bookmark/" + editing.id, "PATCH", {title: $("eTitle").value, folder: $("eFolder").value, tags: $("eTags").value.split(",").map(s => s.trim()).filter(Boolean), notes: $("eNotes").value, state: $("eState").value, favorite: $("eFav").checked}); await loadState(); await reload(); } catch (e) { say(e.message); } };
const bulk = async body => { if (!selected.size) return say("select some bookmarks first"); try { const r = await api("/api/bulk", "POST", {ids: [...selected], ...body}); say(JSON.stringify(r)); await loadState(); await reload(); } catch (e) { say(e.message); } };
$("bulkMove").onclick = () => bulk({folder: $("bulkFolder").value}); $("bulkTagBtn").onclick = () => bulk({add_tags: [$("bulkTag").value]});
$("bulkDel").onclick = () => confirm("Delete " + selected.size + " bookmarks? (Undo restores them)") && bulk({delete: true});
$("more").onclick = more; let timer; $("q").oninput = () => { clearTimeout(timer); timer = setTimeout(reload, 250); };
$("undo").onclick = async () => { try { const r = await api("/api/undo", "POST", {}); say("undid " + r.command); await loadState(); await reload(); } catch (e) { say(e.message); } };
$("exp").onchange = async () => { const f = $("exp").value; if (!f) return; const r = await fetch("/api/export?format=" + f, {headers: {"X-MHBM-Token": TOKEN}}); const blob = await r.blob();
  const a = el("a", {href: URL.createObjectURL(blob), download: "bookmarks." + f}); document.body.append(a); a.click(); a.remove(); $("exp").value = ""; };
const show = which => { $("triView").hidden = which !== "tri"; $("tTri").className = which === "tri" ? "on" : ""; $("libView").hidden = which !== "lib"; $("planView").hidden = which !== "plan"; $("tLib").className = which === "lib" ? "on" : ""; $("tPlan").className = which === "plan" ? "on" : ""; };
$("triReload").onclick = () => loadTriage();
$("tLib").onclick = () => show("lib"); $("tPlan").onclick = () => show("plan"); $("tTri").onclick = () => { show("tri"); loadTriage(); };
async function loadTriage() {
  const box = $("triList"); box.textContent = "grouping your unfiled bookmarks… (the first time this trains on your folders)";
  try { const t = await api("/api/triage"); box.replaceChildren(el("div", {class: "meta", text: t.unfiled + " unfiled bookmarks \u2192 " + t.decisions + " decisions"})); t.clusters.forEach(c => box.append(triCard(c))); }
  catch (e) { box.textContent = e.message; }
}
function triCard(c) {
  const inp = el("input", {list: "fl", value: c.folder || "", placeholder: "folder", "aria-label": "folder"}); inp.style.minWidth = "280px";
  const out = el("div", {class: "meta"});
  const send = async (action) => { try { const r = await api("/api/triage", "POST", {cluster: c.id, action, folder: inp.value, exclude: [...skip]}); card.remove(); await loadState(); if (r.errors.length) alert(r.errors.join("\n")); } catch (e) { out.textContent = e.message; } };
  const skip = new Set();
  const members = c.members.map(m => { const cb = el("input", {type: "checkbox"}); cb.checked = true; cb.onchange = () => cb.checked ? skip.delete(m.id) : skip.add(m.id);
    return el("div", {class: "row"}, cb, el("div", {class: "t", dir: "auto"}, el("div", {text: m.title || m.url}), el("div", {class: "meta", text: m.url}))); });
  const alts = (c.alternatives || []).map(a => { const b = el("button", {type: "button", text: a[0].split("/").slice(-2).join("/") + " " + Math.round(a[1] * 100) + "%"}); b.onclick = () => { inp.value = a[0]; }; return b; });
  const acts = el("div", {class: "row"}, inp, ...alts);
  const go = el("button", {class: "primary", text: "File " + c.size + " here"}); go.onclick = () => send("file");
  const no = el("button", {text: "Reject"}); no.onclick = () => send("reject"); const later = el("button", {text: "Skip"}); later.onclick = () => send("skip");
  const card = el("div", {class: "panel"}, el("div", {class: "meta", dir: "auto", text: c.reason || "one bookmark"}), ...members,
    c.more ? el("div", {class: "meta", text: "\u2026 and " + c.more + " more"}) : null, acts, el("div", {class: "row", style: "border:0"}, go, no, later), out);
  return card;
}
$("load").onclick = async () => { $("planList").textContent = "computing… (the first organize trains on your folders)"; try { plan = await api("/api/plan?kind=" + $("kind").value); planSel = new Set(Object.keys(plan.plan)); drawPlan(); } catch (e) { $("planList").textContent = e.message; } };
function drawPlan() { const box = $("planList"); box.replaceChildren(); const ids = Object.keys(plan.plan);
  box.append(el("div", {class: "meta", text: ids.length + " proposals"}));
  ids.slice(0, 500).forEach(id => { const c = plan.plan[id], b = plan.bookmarks[id] || {}, cb = el("input", {type: "checkbox"}); cb.checked = planSel.has(id); cb.onchange = () => cb.checked ? planSel.add(id) : planSel.delete(id);
    const what = [c.delete ? "DELETE" : "", c.folder != null ? (b.folder || "(unfiled)") + " → " + c.folder : "", c.title ? "title → " + c.title : "", (c.tags || []).length ? "+ " + c.tags.join(", ") : "", c.url ? "url → " + c.url : ""].filter(Boolean).join("  ·  ");
    box.append(el("div", {class: "row"}, cb, el("div", {class: "t", dir: "auto"}, el("div", {text: b.title || b.url}), el("div", {class: "meta", text: what}), el("div", {class: "meta", text: [c.source, c.level, c.reason].filter(Boolean).join(" · ")})))); }); }
$("selAll").onclick = () => { planSel = new Set(Object.keys(plan?.plan || {})); drawPlan(); }; $("selNone").onclick = () => { planSel.clear(); drawPlan(); };
$("apply").onclick = async () => { if (!plan) return; const chosen = {}; planSel.forEach(i => chosen[i] = plan.plan[i]); if (!planSel.size) return;
  if (plan.kind === "dedupe" && !confirm("Merge duplicates? (Undo restores them)")) return;
  const shown = Object.keys(plan.plan).slice(0, 500), declined = shown.filter(i => !planSel.has(i));
  const reviewed = declined.length ? shown.filter(i => planSel.has(i)) : [];   // ticking everything unseen is not a review
  try { const r = await api("/api/apply", "POST", {kind: plan.kind, plan: chosen, declined, reviewed}); $("planList").textContent = JSON.stringify(r) + " - Undo is available."; plan = null; await loadState(); await reload(); } catch (e) { $("planList").textContent = e.message; } };
loadState().then(reload).catch(e => say(e.message));
</script></body></html>
"""


def serve(home, port=0, host="127.0.0.1", open_browser=False, announce=print) -> None:
    server = UIServer(home, host, port)
    url = f"http://127.0.0.1:{server.server_address[1]}/#token={server.token}"
    announce(f"mhbm UI: {url}\n(only this computer can reach it; Ctrl+C to stop)")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
