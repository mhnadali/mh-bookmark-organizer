"""Self-contained offline HTML dashboard (search, folders, tags, charts, archive links)."""
from __future__ import annotations

import json

from . import report

TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:">
<title>Bookmark library</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--ink:#1c2330;--mute:#667085;--line:#e3e6eb;--acc:#2563eb;--ok:#16a34a;--warn:#d97706;--bad:#dc2626}
@media (prefers-color-scheme:dark){:root{--bg:#0f1319;--panel:#171c24;--ink:#e6e9ee;--mute:#98a2b3;--line:#262d38;--acc:#6ea0ff;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{padding:16px 20px;border-bottom:1px solid var(--line);background:var(--panel);display:flex;gap:16px;align-items:center;flex-wrap:wrap}
h1{font-size:18px;margin:0}input{flex:1;min-width:220px;padding:9px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font:inherit}
main{display:grid;grid-template-columns:280px 1fr;gap:16px;padding:16px 20px;max-width:1400px;margin:auto}
@media (max-width:820px){main{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:14px}
.panel h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute);margin:0 0 8px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px}.kpi b{display:block;font-size:20px}.kpi span{color:var(--mute);font-size:12px}
.folder,.tag{cursor:pointer;display:flex;justify-content:space-between;padding:2px 4px;border-radius:5px}.folder:hover,.tag:hover{background:var(--bg)}
.folder small,.tag small{color:var(--mute)}#tags{display:flex;flex-wrap:wrap;gap:6px}.tag{display:inline-flex;gap:4px;border:1px solid var(--line);padding:1px 8px;border-radius:99px}
.item{padding:9px 2px;border-bottom:1px solid var(--line)}.item a{color:var(--acc);text-decoration:none;font-weight:600;overflow-wrap:anywhere}.item .meta{color:var(--mute);font-size:12px;overflow-wrap:anywhere}
.badge{font-size:11px;padding:0 6px;border-radius:99px;border:1px solid currentColor;margin-left:6px}.dead{color:var(--bad)}.suspect,.restricted{color:var(--warn)}.ok{color:var(--ok)}
button{background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:7px 14px;cursor:pointer;font:inherit}
svg text{fill:var(--mute);font-size:11px}.bar{fill:var(--acc)}
</style></head><body>
<header><h1>Bookmark library</h1><input id="q" type="search" placeholder='Search: words  "phrase"  -exclude  tag:dev  folder:Dev  site:github.com  status:dead  is:unfiled  after:2024-01-01' autofocus></header>
<main><aside>
<div class="panel"><h2>Folders</h2><div id="folders"></div></div>
<div class="panel"><h2>Tags</h2><div id="tags"></div></div>
</aside><section>
<div class="panel"><div class="kpis" id="kpis"></div></div>
<div class="panel"><h2 id="count"></h2><div id="results"></div><p><button id="more" hidden>Show more</button></p></div>
<div class="panel"><h2>Top domains</h2><svg id="chartDomains" width="100%" height="200"></svg></div>
<div class="panel"><h2>Saved per year</h2><svg id="chartYears" width="100%" height="160"></svg></div>
</section></main>
<script id="data" type="application/json">__DATA__</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById("data").textContent);
const R = D.rows.map(r => ({id:r[0],title:r[1],url:r[2],folder:r[3],tags:r[4],status:r[5],added:r[6],snip:r[7],arch:r[8],state:r[9],fav:r[10]}));
const $ = id => document.getElementById(id);
const el = (tag, attrs, ...kids) => { const e = document.createElement(tag); for (const k in attrs||{}) { if (k === "text") e.textContent = attrs[k]; else if (k === "class") e.className = attrs[k]; else e.setAttribute(k, attrs[k]); } kids.forEach(c => c && e.append(c)); return e; };
const UNF = ["", "Bookmarks bar", "Other bookmarks", "Mobile bookmarks", "Bookmarks menu"];
const host = u => { try { return new URL(u).hostname.replace(/^www\./, "").toLowerCase(); } catch (e) { return ""; } };
function parse(q) {
  const out = {terms:[], not:[], f:[]}, re = /(-?)(?:(\w+):)?(?:"([^"]+)"|(\S+))/g; let m;
  while ((m = re.exec(q))) { const [, neg, key, phrase, word] = m, v = (phrase ?? word).toLowerCase(), k = (key||"").toLowerCase();
    if (["tag","folder","site","host","status","is","before","after","title"].includes(k)) out.f.push([k, v, !!neg]);
    else if (neg) out.not.push(v); else out.terms.push(v); }
  return out;
}
function match(r, p) {
  const hay = (r.title + " " + r.url + " " + r.folder + " " + r.tags.join(" ") + " " + r.snip).toLowerCase();
  if (!p.terms.every(t => hay.includes(t)) || p.not.some(t => hay.includes(t))) return false;
  return p.f.every(([k, v, neg]) => { let ok = true;
    if (k === "tag") ok = r.tags.includes(v);
    else if (k === "folder") ok = ("/" + r.folder.toLowerCase() + "/").includes("/" + v + "/");
    else if (k === "site" || k === "host") { const h = host(r.url); ok = h === v || h.endsWith("." + v); }
    else if (k === "status") ok = (r.status || "unchecked") === v;
    else if (k === "title") ok = r.title.toLowerCase().includes(v);
    else if (k === "before") ok = r.added > 0 && r.added < Date.parse(v) / 1000;
    else if (k === "after") ok = r.added >= Date.parse(v) / 1000;
    else if (k === "is") ok = v === "unfiled" ? UNF.includes(r.folder) : v === "untagged" ? !r.tags.length : v === "archived" ? !!r.arch : v === "unchecked" ? !r.status : v === "unread" || v === "reading" || v === "read" ? r.state === v : v === "favorite" || v === "favourite" ? !!r.fav : true;
    return neg ? !ok : ok; });
}
let shown = 0, current = [];
function render() {
  const p = parse($("q").value); current = R.filter(r => match(r, p)); shown = 0;
  $("count").textContent = current.length + " of " + R.length + " bookmarks"; $("results").replaceChildren(); more();
}
function safeHref(u) { return /^https?:\/\//i.test(u) ? u : "#"; }
function more() {
  const box = $("results");
  current.slice(shown, shown + 100).forEach(r => {
    const a = el("a", {href: safeHref(r.url), rel: "noopener noreferrer", target: "_blank", dir: "auto", text: r.title || r.url});
    const t = el("div", {}, a); if (r.status && r.status !== "ok") t.append(el("span", {class: "badge " + r.status, text: r.status}));
    if (r.state) t.append(el("span", {class: "badge", text: r.state})); if (r.fav) t.append(el("span", {class: "badge ok", text: "\u2605"}));
    const meta = el("div", {class: "meta", dir: "auto"}); meta.textContent = [r.folder || "unfiled", r.tags.map(x => "#" + x).join(" "), host(r.url)].filter(Boolean).join("  ·  ");
    const kids = [t, meta]; if (r.snip) kids.push(el("div", {class: "meta", dir: "auto", text: r.snip}));
    if (r.arch && /^archive\/[\w.-]+$/.test(r.arch)) kids.push(el("div", {class: "meta"}, el("a", {href: r.arch, target: "_blank", rel: "noopener", text: "saved copy"})));
    box.append(el("div", {class: "item"}, ...kids));
  });
  shown += 100; $("more").hidden = shown >= current.length;
}
function setQ(s) { $("q").value = s; render(); window.scrollTo(0, 0); }
function sidebar() {
  const fc = {}, tc = {};
  R.forEach(r => { const parts = r.folder ? r.folder.split("/") : ["(unfiled)"];
    parts.forEach((_, i) => { const k = parts.slice(0, i + 1).join("/"); fc[k] = (fc[k] || 0) + 1; });
    r.tags.forEach(t => tc[t] = (tc[t] || 0) + 1); });
  const fbox = $("folders"); Object.keys(fc).sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase())).forEach(f => {
    const depth = f === "(unfiled)" ? 0 : f.split("/").length - 1, row = el("div", {class: "folder"}, el("span", {text: f.split("/").pop()}), el("small", {text: fc[f]}));
    row.style.paddingLeft = (4 + depth * 12) + "px"; row.title = f; row.onclick = () => setQ(f === "(unfiled)" ? "is:unfiled" : 'folder:"' + f + '"'); fbox.append(row); });
  Object.entries(tc).sort((a, b) => b[1] - a[1]).slice(0, 60).forEach(([t, n]) => { const e = el("span", {class: "tag"}, el("span", {text: "#" + t}), el("small", {text: n})); e.onclick = () => setQ("tag:" + t); $("tags").append(e); });
}
function bars(svgId, rows) {
  const svg = $(svgId), W = 600, rh = 20, max = Math.max(1, ...rows.map(r => r[1])); svg.setAttribute("viewBox", "0 0 " + W + " " + (rows.length * rh + 6)); svg.removeAttribute("height");
  rows.forEach(([k, v], i) => { const ns = "http://www.w3.org/2000/svg", y = i * rh + 3, w = Math.max(2, (v / max) * (W - 210));
    const t = document.createElementNS(ns, "text"); t.setAttribute("x", 0); t.setAttribute("y", y + 13); t.textContent = k.length > 28 ? k.slice(0, 27) + "…" : k;
    const b = document.createElementNS(ns, "rect"); b.setAttribute("class", "bar"); b.setAttribute("x", 190); b.setAttribute("y", y); b.setAttribute("width", w); b.setAttribute("height", 14); b.setAttribute("rx", 3);
    const n = document.createElementNS(ns, "text"); n.setAttribute("x", 196 + w); n.setAttribute("y", y + 13); n.textContent = v; svg.append(t, b, n); });
}
function kpis() {
  const k = D.stats, items = [["Bookmarks", k.total], ["Unfiled", k.unfiled], ["Untagged", k.untagged], ["Duplicates", k.duplicate_extras], ["Dead", (k.status.dead||0)], ["Suspect", (k.status.suspect||0)], ["Archived", k.archived]];
  items.forEach(([l, v]) => $("kpis").append(el("div", {class: "kpi"}, el("b", {text: v}), el("span", {text: l}))));
}
kpis(); sidebar(); render(); $("q").addEventListener("input", render); $("more").onclick = more;
const dom = {}; R.forEach(r => { const h = host(r.url); if (h) dom[h] = (dom[h]||0) + 1; });
bars("chartDomains", Object.entries(dom).sort((a, b) => b[1] - a[1]).slice(0, 10));
const yrs = {}; R.forEach(r => { if (r.added) { const y = new Date(r.added * 1000).getUTCFullYear(); yrs[y] = (yrs[y]||0) + 1; } });
bars("chartYears", Object.entries(yrs).sort());
</script></body></html>
"""


def render(bms, folders=(), generated: str = "") -> str:
    rows = [[b.id, b.title, b.url, b.folder, b.tags, b.status, b.added,
             " ".join((b.content or b.notes)[:200].split()), b.archive_path, b.state, int(b.favorite)] for b in bms]
    stats = report.build(bms, folders)
    payload = json.dumps({"rows": rows, "stats": stats, "generated": generated}, ensure_ascii=False)
    # keep the JSON inert inside <script>: no "</script>", "<!--" or U+2028/9 can terminate or confuse it
    payload = payload.replace("</", "<\\/").replace("<!--", "<\\!--").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return TEMPLATE.replace("__DATA__", payload)
