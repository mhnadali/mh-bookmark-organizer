import assert from "node:assert/strict";
import test from "node:test";
import { applyMoves, buildSaveMessage, chunk, ensureFolder, flattenTree, folderPaths } from "../lib.js";

/** In-memory stand-in for chrome.bookmarks with the same tree shape. */
function fakeApi() {
  let next = 10;
  const root = { id: "0", title: "", children: [
    { id: "1", title: "Bookmarks bar", folderType: "bookmarks-bar", children: [] },
    { id: "2", title: "Other bookmarks", folderType: "other", children: [] },
    { id: "3", title: "Mobile bookmarks", folderType: "mobile", children: [] }] };
  const all = new Map();
  const index = (n, parent) => { all.set(n.id, { n, parent }); (n.children ?? []).forEach((c) => index(c, n)); };
  index(root, null);
  return {
    root,
    async getTree() { return [structuredClone(root)]; },
    async create({ parentId, title, url }) {
      const node = { id: String(next++), title, ...(url ? { url } : { children: [] }), dateAdded: 1700000000000 };
      const parent = all.get(parentId).n; parent.children.push(node); all.set(node.id, { n: node, parent }); return structuredClone(node);
    },
    async move(id, { parentId }) {
      const { n, parent } = all.get(id); parent.children.splice(parent.children.indexOf(n), 1);
      const dest = all.get(parentId).n; dest.children.push(n); all.set(id, { n, parent: dest });
    },
  };
}

async function seeded() {
  const api = fakeApi();
  const dev = await ensureFolder(api, "Bookmarks bar/Dev");
  await api.create({ parentId: dev, title: "A", url: "https://a.com" });
  await api.create({ parentId: "2", title: "Loose", url: "https://loose.com" });
  await api.create({ parentId: await ensureFolder(api, "Bookmarks bar/Work/Home"), title: "Odd", url: "https://odd.com" });
  return api;
}

test("flattenTree uses host-style folder paths and escapes slashes in names", async () => {
  const api = fakeApi();
  const id = await api.create({ parentId: "1", title: "AC/DC" });
  await api.create({ parentId: id.id, title: "Song", url: "https://song.example" });
  const flat = flattenTree(await api.getTree());
  assert.deepEqual(flat.map((n) => [n.url, n.folderPath]), [["https://song.example", "Bookmarks bar/AC∕DC"]]);
});

test("roots are recognised by folderType, so a localized browser still maps correctly", () => {
  const tree = [{ id: "0", children: [
    { id: "1", title: "نوار نشانه‌ها", folderType: "bookmarks-bar", children: [{ id: "5", title: "x", url: "https://x.test" }] },
    { id: "2", title: "سایر", folderType: "other", children: [] }] }];
  assert.equal(flattenTree(tree)[0].folderPath, "Bookmarks bar");
  assert.deepEqual(folderPaths(tree), ["Bookmarks bar", "Other bookmarks"]);
});

test("ensureFolder creates only what is missing and is idempotent", async () => {
  const api = fakeApi();
  const a = await ensureFolder(api, "Bookmarks bar/X/Y");
  const b = await ensureFolder(api, "Bookmarks bar/X/Y");
  assert.equal(a, b);
  assert.deepEqual(folderPaths(await api.getTree()).sort(), ["Bookmarks bar", "Bookmarks bar/X", "Bookmarks bar/X/Y", "Mobile bookmarks", "Other bookmarks"]);
  const other = await ensureFolder(api, "Plain/Path");                      // outside the three roots -> Other bookmarks
  assert.ok(folderPaths(await api.getTree()).includes("Other bookmarks/Plain/Path"));
  assert.notEqual(other, a);
});

test("applyMoves moves by (url, from), creates folders, and skips what has changed", async () => {
  const api = await seeded();
  const res = await applyMoves(api, [
    { url: "https://loose.com", from: "Other bookmarks", to: "Bookmarks bar/Reading/Later" },
    { url: "https://a.com", from: "Bookmarks bar/Dev", to: "Bookmarks bar/Dev" },          // already there
    { url: "https://odd.com", from: "Bookmarks bar/Elsewhere", to: "Bookmarks bar/Dev" },   // not where the host thought
    { url: "https://nope.com", from: "", to: "Bookmarks bar/Dev" }]);
  assert.deepEqual([res.moved, res.skipped, res.created], [1, 3, 2]);
  const flat = flattenTree(await api.getTree());
  assert.equal(flat.find((n) => n.url === "https://loose.com").folderPath, "Bookmarks bar/Reading/Later");
  assert.equal(flat.find((n) => n.url === "https://odd.com").folderPath, "Bookmarks bar/Work/Home");
  assert.deepEqual(res.applied.map((m) => m.url), ["https://loose.com"]);
});

test("duplicate bookmarks in one folder are moved one at a time", async () => {
  const api = fakeApi();
  const f = await ensureFolder(api, "Bookmarks bar/D");
  await api.create({ parentId: f, title: "1", url: "https://same.com" });
  await api.create({ parentId: f, title: "2", url: "https://same.com" });
  const res = await applyMoves(api, [{ url: "https://same.com", from: "Bookmarks bar/D", to: "Bookmarks bar/E" }]);
  assert.equal(res.moved, 1);
  assert.equal(flattenTree(await api.getTree()).filter((n) => n.folderPath === "Bookmarks bar/D").length, 1);
});

test("chunk keeps native messages small", () => {
  assert.deepEqual(chunk([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]]);
});

test("the save message carries the proposal so the library can tell if it was kept", () => {
  const tab = { url: "https://a.example/x", title: "Title" };
  const guess = { folder: "Food/Recipes", confidence: 0.91, level: "exact", source: "learned" };
  assert.deepEqual(buildSaveMessage(tab, "Food/Recipes", "pasta, quick ,", guess, 1700000000), {
    cmd: "save", url: "https://a.example/x", title: "Title", folder: "Food/Recipes", tags: ["pasta", "quick"], added: 1700000000,
    suggested: "Food/Recipes", confidence: 0.91, level: "exact", source: "learned",
  });
  // changing the folder still reports what was proposed
  assert.equal(buildSaveMessage(tab, "Travel", "", guess, 1).suggested, "Food/Recipes");
});

test("nothing is echoed when there was no real proposal", () => {
  const tab = { url: "https://a.example/x" };
  for (const guess of [null, undefined, {}, { folder: "", source: "learned" }, { folder: "A", source: "weak" }]) {
    const msg = buildSaveMessage(tab, "A", "", guess, 5);
    assert.equal("suggested" in msg, false);
    assert.equal(msg.title, "");
  }
});
