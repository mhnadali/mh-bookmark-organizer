"""A single offline HTML page to review a filing/tagging/title plan item by item and download the approved plan."""
from __future__ import annotations

import json

TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:">
<title>Review plan</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--ink:#1c2330;--mute:#667085;--line:#e3e6eb;--acc:#2563eb;--ok:#16a34a;--warn:#d97706;--bad:#dc2626}
@media (prefers-color-scheme:dark){:root{--bg:#0f1319;--panel:#171c24;--ink:#e6e9ee;--mute:#98a2b3;--line:#262d38;--acc:#6ea0ff;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{position:sticky;top:0;z-index:2;padding:12px 18px;background:var(--panel);border-bottom:1px solid var(--line);display:flex;gap:14px;align-items:center;flex-wrap:wrap}
h1{font-size:17px;margin:0}input,select,button{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:7px;padding:6px 10px}
button{cursor:pointer;background:var(--panel)}button.primary{background:var(--acc);border-color:var(--acc);color:#fff}
main{max-width:1250px;margin:auto;padding:14px 18px}.item{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:10px;display:grid;grid-template-columns:28px 1fr;gap:8px}
.item.off{opacity:.45}.title{font-weight:600;overflow-wrap:anywhere}.meta{color:var(--mute);font-size:12px;overflow-wrap:anywhere}
.arrow{color:var(--acc)}.pill{font-size:11px;border:1px solid currentColor;border-radius:99px;padding:0 7px;margin-right:6px}.hi{color:var(--ok)}.mid{color:var(--warn)}.lo{color:var(--bad)}
.edit{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px}.edit input{min-width:260px;flex:1}.count{color:var(--mute)}
</style></head><body>
<header><h1>Review plan</h1><input id="q" type="search" placeholder="filter…" style="min-width:200px">
<label>min confidence <input id="conf" type="range" min="0" max="100" value="0"> <span id="confv">0%</span></label>
<select id="src"><option value="">all sources</option></select>
<button id="all">select visible</button><button id="none">clear visible</button>
<span class="count" id="count"></span><button class="primary" id="dl">Download approved plan</button></header>
<main id="list"></main><datalist id="folders"></datalist>
<script id="data" type="application/json">__DATA__</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById("data").textContent), items = D.items;
const $ = id => document.getElementById(id);
const el = (t, a, ...k) => { const e = document.createElement(t); for (const x in a||{}) { if (x === "text") e.textContent = a[x]; else if (x === "class") e.className = a[x]; else e.setAttribute(x, a[x]); } k.forEach(c => c && e.append(c)); return e; };
D.folders.forEach(f => $("folders").append(el("option", {value: f})));
[...new Set(items.map(i => i.source || "plan"))].forEach(s => $("src").append(el("option", {value: s, text: s})));
const state = items.map(i => ({on: true, folder: i.folder, title: i.title, tags: (i.tags || []).join(", ")}));
let touched = false;   // untouched = everything is still as proposed: downloading that is not a review
function visible(i, k) {
  const q = $("q").value.toLowerCase(), c = +$("conf").value / 100, s = $("src").value;
  const hay = (i.btitle + " " + i.url + " " + (i.folder || "") + " " + (i.reason || "")).toLowerCase();
  return (!q || hay.includes(q)) && (i.confidence == null || i.confidence >= c) && (!s || (i.source || "plan") === s);
}
function draw() {
  const box = $("list"); box.replaceChildren(); let shown = 0, on = 0;
  items.forEach((i, k) => {
    if (state[k].on) on++;
    if (!visible(i, k)) return; shown++;
    const cb = el("input", {type: "checkbox"}); cb.checked = state[k].on; cb.onchange = () => { touched = true; state[k].on = cb.checked; row.classList.toggle("off", !cb.checked); counts(); };
    const conf = i.confidence == null ? null : el("span", {class: "pill " + (i.confidence >= .85 ? "hi" : i.confidence >= .6 ? "mid" : "lo"), text: Math.round(i.confidence * 100) + "%"});
    const a = el("a", {href: /^https?:\/\//i.test(i.url) ? i.url : "#", target: "_blank", rel: "noopener noreferrer", dir: "auto", text: i.btitle || i.url}); a.style.color = "var(--acc)";
    const head = el("div", {class: "title"}, conf, a);
    const change = el("div", {class: "meta", dir: "auto"});
    const parts = [];
    if (i.folder != null) parts.push((i.old_folder || "(unfiled)") + "  \u2192  " + (state[k].folder || "(unfiled)"));
    if (i.title != null) parts.push("title: " + i.btitle + "  \u2192  " + state[k].title);
    if (i.tags && i.tags.length) parts.push("tags: + " + i.tags.join(", "));
    if (i.new_url) parts.push("url \u2192 " + i.new_url);
    if (i.delete) parts.push("DELETE");
    change.textContent = parts.join("   |   ");
    const why = el("div", {class: "meta", dir: "auto", text: [i.source, i.level, i.reason].filter(Boolean).join(" \u00b7 ")});
    const kids = [head, change, why];
    if (i.folder != null) {
      const inp = el("input", {list: "folders", value: state[k].folder || "", "aria-label": "folder", placeholder: "folder"}); inp.oninput = () => { touched = true; state[k].folder = inp.value; change.textContent = change.textContent.replace(/\u2192 [^|]*/, "\u2192 " + (inp.value || "(unfiled)") + "   "); };
      const alts = (i.alternatives || []).map(x => { const b = el("button", {type: "button", text: x[0].split("/").slice(-2).join("/") + " " + Math.round(x[1] * 100) + "%"}); b.onclick = () => { inp.value = x[0]; inp.oninput(); }; return b; });
      kids.push(el("div", {class: "edit"}, inp, ...alts));
    }
    const row = el("div", {class: "item" + (state[k].on ? "" : " off")}, el("div", {}, cb), el("div", {}, ...kids));
    box.append(row);
  });
  counts(shown);
}
function counts(shown) { const on = state.filter(s => s.on).length; $("count").textContent = on + " of " + items.length + " approved" + (shown != null ? " \u00b7 " + shown + " shown" : ""); }
["q", "conf", "src"].forEach(id => $(id).addEventListener("input", () => { $("confv").textContent = $("conf").value + "%"; draw(); }));
$("all").onclick = () => { touched = true; items.forEach((i, k) => { if (visible(i, k)) state[k].on = true; }); draw(); };
$("none").onclick = () => { touched = true; items.forEach((i, k) => { if (visible(i, k)) state[k].on = false; }); draw(); };
$("dl").onclick = () => {
  const plan = {};
  items.forEach((i, k) => {
    const filing = i.folder != null && !i.delete && (i.source === "learned" || i.source === "rules" || i.source === "triage");
    const note = filing ? {suggested: i.folder, confidence: i.confidence, level: i.level || "exact", source: i.source, reviewed: touched} : {};
    if (!state[k].on) { if (filing && touched) plan[i.id] = {rejected: true, ...note}; return; }
    const e = {...note};
    if (i.delete) e.delete = true;
    if (i.folder != null) e.folder = state[k].folder;
    if (i.title != null) e.title = state[k].title;
    if (i.tags && i.tags.length) e.tags = i.tags;
    if (i.new_url) e.url = i.new_url;
    if (i.notes != null) e.notes = i.notes;
    plan[i.id] = e; });
  const blob = new Blob([JSON.stringify(plan, null, 2)], {type: "application/json"});
  const a = el("a", {href: URL.createObjectURL(blob), download: "approved-plan.json"}); document.body.append(a); a.click(); a.remove();
};
draw();
</script></body></html>
"""


def render(plan: dict, bms, folders=()) -> str:
    by_id = {b.id: b for b in bms}
    items = []
    for bid, c in plan.items():
        b = by_id.get(bid)
        if b is None or not isinstance(c, dict):
            continue
        item = {"id": bid, "url": b.url, "btitle": b.title, "old_folder": b.folder,
                "source": c.get("source", ""), "level": c.get("level", ""), "reason": c.get("reason", ""),
                "confidence": c.get("confidence"), "alternatives": c.get("alternatives", [])}
        if "folder" in c:
            item["folder"] = c["folder"]
        if "title" in c:
            item["title"] = c["title"]
        if c.get("tags"):
            item["tags"] = list(c["tags"])
        if c.get("url"):
            item["new_url"] = c["url"]
        if c.get("delete"):
            item["delete"] = True
        if "notes" in c:
            item["notes"] = c["notes"]
        items.append(item)
    payload = json.dumps({"items": items, "folders": sorted(folders)}, ensure_ascii=False)
    payload = payload.replace("</", "<\\/").replace("<!--", "<\\!--").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return TEMPLATE.replace("__DATA__", payload)
