// Pure helpers for the extension. Nothing here touches `chrome.*` directly: the bookmarks API is passed in,
// which is what makes the logic testable in Node with a fake tree.

export const ROOT_NAMES = ["Bookmarks bar", "Other bookmarks", "Mobile bookmarks"];
const FOLDER_TYPES = { "bookmarks-bar": 0, other: 1, mobile: 2 };
const SLASH = "∕"; // stands in for "/" inside a folder name, the same convention the Python side uses

export const esc = (name) => String(name ?? "").replace(/\//g, SLASH);
export const unesc = (name) => String(name ?? "").replaceAll(SLASH, "/");

/** Display name of a root folder: by folderType when the browser provides it, else by position. */
function rootIndex(root, position) {
  return root.folderType in FOLDER_TYPES ? FOLDER_TYPES[root.folderType] : position;
}

/** Flatten `chrome.bookmarks.getTree()` into [{id, url, title, folderPath, dateAdded}] with host-style folder paths. */
export function flattenTree(tree) {
  const out = [];
  const walk = (node, path) => {
    for (const child of node.children ?? []) {
      if (child.url) {
        out.push({ id: child.id, url: child.url, title: child.title ?? "", folderPath: path.join("/"), dateAdded: child.dateAdded ?? 0 });
      } else {
        walk(child, [...path, esc(child.title)]);
      }
    }
  };
  (tree[0]?.children ?? []).forEach((root, i) => walk(root, [ROOT_NAMES[rootIndex(root, i)] ?? esc(root.title)]));
  return out;
}

/** Every folder path in the tree (including empty ones). */
export function folderPaths(tree) {
  const out = [];
  const walk = (node, path) => {
    for (const child of node.children ?? []) {
      if (!child.url) {
        const p = [...path, esc(child.title)];
        out.push(p.join("/"));
        walk(child, p);
      }
    }
  };
  (tree[0]?.children ?? []).forEach((root, i) => {
    const name = ROOT_NAMES[rootIndex(root, i)] ?? esc(root.title);
    out.push(name);
    walk(root, [name]);
  });
  return out;
}

function findRoot(tree, name) {
  const roots = tree[0]?.children ?? [];
  const idx = ROOT_NAMES.indexOf(name);
  if (idx < 0) return null;
  return roots.find((r, i) => rootIndex(r, i) === idx) ?? null;
}

/**
 * Make sure the folder `path` exists (creating what is missing) and return its id.
 * Paths outside the three roots go under "Other bookmarks", like the Python side does.
 */
export async function ensureFolder(api, path) {
  let tree = await api.getTree();
  const parts = String(path || "").split("/").filter(Boolean);
  let rootName = ROOT_NAMES.includes(parts[0]) ? parts.shift() : "Other bookmarks";
  let node = findRoot(tree, rootName);
  if (!node) throw new Error(`no "${rootName}" folder in this browser`);
  for (const part of parts) {
    const title = unesc(part);
    let next = (node.children ?? []).find((c) => !c.url && c.title === title);
    if (!next) {
      next = await api.create({ parentId: node.id, title });
      next.children = [];
      (node.children ??= []).push(next);
    }
    node = next;
  }
  return node.id;
}

/**
 * Apply moves [{url, from, to}] in the live browser. A move is skipped (not guessed) when the bookmark is no longer at
 * `from`. Returns {moved, skipped, created, applied: [the moves that happened]}.
 */
export async function applyMoves(api, moves) {
  const tree = await api.getTree();
  const nodes = flattenTree(tree);
  const byKey = new Map();
  for (const n of nodes) {
    const key = n.url + "\u0001" + n.folderPath;
    if (!byKey.has(key)) byKey.set(key, []);
    byKey.get(key).push(n);
  }
  const before = new Set(folderPaths(tree));
  let moved = 0, skipped = 0;
  const applied = [];
  for (const m of moves) {
    const hit = byKey.get(m.url + "\u0001" + (m.from ?? ""))?.shift();
    if (!hit || m.to === m.from) { skipped++; continue; }
    const parentId = await ensureFolder(api, m.to);
    await api.move(hit.id, { parentId });
    moved++;
    applied.push(m);
  }
  const created = folderPaths(await api.getTree()).filter((p) => !before.has(p)).length;
  return { moved, skipped, created, applied };
}

/** Chunk an array so each native message stays well under the browser's size limits. */
export function chunk(items, size = 400) {
  const out = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
}

/**
 * The message the popup sends to the library when a page is saved. When a folder was proposed for this page the proposal
 * travels along, so the library can record whether it was kept or changed (that is how it learns whether to trust itself).
 */
export function buildSaveMessage(tab, folder, tagsText, guessed, nowSeconds) {
  const msg = {
    cmd: "save", url: tab.url, title: tab.title || "", folder,
    tags: String(tagsText || "").split(",").map((s) => s.trim()).filter(Boolean), added: nowSeconds,
  };
  if (guessed?.folder && guessed.source !== "weak") {
    Object.assign(msg, { suggested: guessed.folder, confidence: guessed.confidence, level: guessed.level, source: guessed.source });
  }
  return msg;
}
