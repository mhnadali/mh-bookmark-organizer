import { applyMoves, buildSaveMessage, chunk, ensureFolder, flattenTree, folderPaths } from "./lib.js";

const $ = (id) => document.getElementById(id);
const host = (msg) => new Promise((resolve, reject) =>
  chrome.runtime.sendMessage({ type: "host", msg }, (res) => (res?.ok ? resolve(res.result) : reject(new Error(res?.error || chrome.runtime.lastError?.message || "no answer")))));
const setText = (id, text) => { $(id).textContent = text; };
const li = (text, href) => { const e = document.createElement("li"); if (href) { const a = document.createElement("a"); a.href = href; a.target = "_blank"; a.rel = "noopener"; a.textContent = text; e.append(a); } else e.textContent = text; return e; };
let moves = [];
let guessed = null;   // what we proposed for this page: echoed back on save so the library can learn from the outcome

async function init() {
  try {
    const info = await host({ cmd: "ping" });
    setText("status", `library: ${info.bookmarks} bookmarks`);
  } catch (e) {
    setText("status", "host not reachable");
    setText("saveMsg", `${e.message}. Run: mhbm install-native-host --extension-id ${chrome.runtime.id}`);
    return;
  }
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  setText("page", tab?.title || tab?.url || "");
  const [folders, guess] = await Promise.all([host({ cmd: "folders" }), tab?.url ? host({ cmd: "suggest", url: tab.url, title: tab.title || "" }) : null]);
  for (const f of folders) { const o = document.createElement("option"); o.value = f; $("folders").append(o); }
  if (guess) {
    guessed = guess;
    $("folder").value = guess.folder || "";
    $("tags").value = (guess.tags || []).join(", ");
    for (const a of guess.alternatives || []) {
      const b = document.createElement("button");
      b.textContent = `${a.folder.split("/").slice(-2).join("/")} ${Math.round(a.score * 100)}%`;
      b.onclick = () => { $("folder").value = a.folder; };
      $("alts").append(b);
    }
    if (guess.confidence != null) setText("saveMsg", `suggested by ${guess.source} (${Math.round(guess.confidence * 100)}% sure)`);
  }
}

$("saveBtn").onclick = async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const folder = $("folder").value.trim();
  try {
    const parentId = await ensureFolder(chrome.bookmarks, folder);
    await chrome.bookmarks.create({ parentId, title: tab.title || tab.url, url: tab.url });
    await host(buildSaveMessage(tab, folder, $("tags").value, guessed, Math.floor(Date.now() / 1000)));
    setText("saveMsg", "saved");
  } catch (e) { setText("saveMsg", e.message); }
};

let timer;
$("q").oninput = () => {
  clearTimeout(timer);
  timer = setTimeout(async () => {
    const box = $("results"); box.replaceChildren();
    if (!$("q").value.trim()) return;
    try { for (const r of await host({ cmd: "search", query: $("q").value, limit: 8 })) box.append(li(r.title || r.url, r.url)); } catch (e) { box.append(li(e.message)); }
  }, 250);
};

$("sendBtn").onclick = async () => {
  setText("planMsg", "reading your bookmarks…");
  try {
    const nodes = flattenTree(await chrome.bookmarks.getTree());
    let imported = 0;
    for (const part of chunk(nodes)) imported += (await host({ cmd: "import_tree", nodes: part })).imported;
    setText("planMsg", `${nodes.length} bookmarks read, ${imported} new in the library`);
  } catch (e) { setText("planMsg", e.message); }
};

$("planBtn").onclick = async () => {
  setText("planMsg", "thinking… (the first time this trains on your folders)");
  try {
    const res = await host({ cmd: "plan", limit: 200 });
    moves = res.moves;
    const box = $("plan"); box.replaceChildren();
    for (const m of moves.slice(0, 15)) box.append(li(`${m.url.slice(0, 50)} → ${m.to.split("/").slice(-2).join("/")} (${Math.round((m.confidence ?? 1) * 100)}%)`));
    setText("planMsg", `${res.total} suggested moves` + (moves.length < res.total ? ` (showing the first ${moves.length})` : ""));
    $("applyBtn").hidden = !moves.length;
  } catch (e) { setText("planMsg", e.message); }
};

$("applyBtn").onclick = async () => {
  try {
    const r = await applyMoves(chrome.bookmarks, moves);
    await host({ cmd: "apply_moves", moves: r.applied });
    setText("planMsg", `moved ${r.moved}, skipped ${r.skipped}, created ${r.created} folders`);
    $("applyBtn").hidden = true;
  } catch (e) { setText("planMsg", e.message); }
};

init();
