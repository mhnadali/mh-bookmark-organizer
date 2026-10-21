#!/usr/bin/env node
'use strict';
// Drives a real Chromium-family browser on a scratch user-data-dir through a tiny MV3 probe extension (probe-ext/).
// One command per process, JSON on disk, so the Python tests stay simple and a hung browser cannot outlive its test.
//
//   node driver.js [--bin PATH] [--log FILE] create <userDataDir> <spec.json> <out.json>
//       spec: {bar:[node], other:[node]}, node = {title,url} | {title,children:[node]};  out: {errors, tree: flat}
//   node driver.js [--bin PATH] dump   <userDataDir> <out.json> [--touch]
//       out: {version, tree: flat}; --touch creates and removes one bookmark so the browser rewrites its Bookmarks file
//   node driver.js [--bin PATH] hold   <userDataDir>      prints "ready", keeps the browser open until stdin closes (to test lock detection)
//   node driver.js probe                                   prints {"bin": ...} if Playwright and a browser are available (exit 3 otherwise)
//   node driver.js [--bin PATH] canon  <userDataDir> <urls.json> <out.json>
//       out: [{raw, canon} | {raw, error}]  (what the browser's URL parser makes of each string)
//
// flat = [{id, parentId, index, title, url?, dateAdded?, folderType?}] in document order (nested trees can be too deep for CDP).
// Browser: --bin, else $CHROMIUM_BIN, else Playwright's own install, else /opt/pw-browsers/chromium-*/chrome-linux/chrome.
const fs = require('fs');
const path = require('path');

function loadPlaywright() {
  const tried = [];
  for (const name of [process.env.PLAYWRIGHT_MODULE, 'playwright-core', 'playwright', '@playwright/test', '/opt/node22/lib/node_modules/playwright'].filter(Boolean)) {
    try { return require(name); } catch (e) { tried.push(name); }
  }
  throw new Error(`playwright not found (tried ${tried.join(', ')}); run "npm ci" in tests/realbrowser or set PLAYWRIGHT_MODULE`);
}

function findBinary(pw, explicit) {
  const cands = [explicit, process.env.CHROMIUM_BIN];
  try { cands.push(pw.chromium.executablePath()); } catch (e) { /* no managed browser */ }
  try {
    const root = process.env.PLAYWRIGHT_BROWSERS_PATH || '/opt/pw-browsers';
    for (const d of fs.readdirSync(root).filter(x => /^chromium-\d+$/.test(x)).sort().reverse()) cands.push(path.join(root, d, 'chrome-linux', 'chrome'));
  } catch (e) { /* none */ }
  const found = cands.filter(Boolean).find(p => fs.existsSync(p));
  if (!found) throw new Error('no Chromium-family browser found: set CHROMIUM_BIN');
  return found;
}

// The browser may run other service workers (component extensions): pick ours by the marker its background.js sets.
async function probeWorker(ctx) {
  const deadline = Date.now() + 30000;
  const isProbe = async (w) => { try { return (await w.evaluate(() => self.mhbmProbe === true)) === true; } catch (e) { return false; } };
  while (Date.now() < deadline) {
    for (const w of ctx.serviceWorkers()) if (await isProbe(w)) return w;
    await ctx.waitForEvent('serviceworker', { timeout: 1000 }).catch(() => null);
  }
  throw new Error('probe extension did not start (does this browser build load unpacked extensions from the command line?)');
}

async function launch(userDataDir, bin, logFile) {
  const pw = loadPlaywright();
  const ext = path.join(__dirname, 'probe-ext');
  const args = ['--headless=new', `--disable-extensions-except=${ext}`, `--load-extension=${ext}`,
    // branded builds (Chrome >= 137) ignore --load-extension unless this is disabled; builds that do not know the name ignore it
    '--disable-features=DisableLoadExtensionCommandLineSwitch'];
  if (logFile) args.push('--enable-logging', `--log-file=${path.resolve(logFile)}`, '--v=0');
  const ctx = await pw.chromium.launchPersistentContext(userDataDir, { executablePath: findBinary(pw, bin), headless: false, args, timeout: 60000 });
  return { ctx, sw: await probeWorker(ctx) };
}

const FLATTEN = async () => {
  // Brave/Chromium <= 143 leave chrome.bookmarks undefined when the Bookmarks file could not be decoded at all: report that as an empty tree.
  if (!chrome.bookmarks) return [];
  const out = [];
  (function walk(n) {
    out.push({ id: n.id, parentId: n.parentId, index: n.index, title: n.title, url: n.url, dateAdded: n.dateAdded, folderType: n.folderType });
    for (const c of (n.children || [])) walk(c);
  })((await chrome.bookmarks.getTree())[0]);
  return out;
};

async function main() {
  const argv = process.argv.slice(2);
  const take = (flag) => { const i = argv.indexOf(flag); return i < 0 ? undefined : argv.splice(i, 2)[1]; };
  const bin = take('--bin'); const logFile = take('--log');
  const touch = argv.includes('--touch'); if (touch) argv.splice(argv.indexOf('--touch'), 1);
  const [cmd, udd, a, b] = argv;
  if (cmd === 'probe') {
    try { console.log(JSON.stringify({ bin: findBinary(loadPlaywright(), bin) })); } catch (e) { console.error(e.message); process.exit(3); }
    return;
  }
  const { ctx, sw } = await launch(udd, bin, logFile);
  try {
    if (cmd === 'create') {
      const spec = JSON.parse(fs.readFileSync(a, 'utf8'));
      const errors = await sw.evaluate(async (spec) => {
        const errors = [];
        const top = (await chrome.bookmarks.getTree())[0].children;
        const parents = { bar: top.find(n => n.folderType === 'bookmarks-bar').id, other: top.find(n => n.folderType === 'other').id };
        async function mk(parentId, node) {
          try {
            const made = await chrome.bookmarks.create({ parentId, title: node.title, ...(node.url !== undefined ? { url: node.url } : {}) });
            for (const c of (node.children || [])) await mk(made.id, c);
          } catch (e) { errors.push({ node: node.title, error: String((e && e.message) || e) }); }
        }
        for (const [root, nodes] of Object.entries(spec)) for (const n of nodes) await mk(parents[root], n);
        return errors;
      }, spec);
      fs.writeFileSync(b, JSON.stringify({ errors, tree: await sw.evaluate(FLATTEN) }));
    } else if (cmd === 'dump') {
      const tree = await sw.evaluate(FLATTEN);
      const ua = await sw.evaluate(() => navigator.userAgent);
      if (touch && tree.length) {
        await sw.evaluate(async () => {
          const other = (await chrome.bookmarks.getTree())[0].children.find(n => n.folderType === 'other');
          const n = await chrome.bookmarks.create({ parentId: other.id, title: 'touch', url: 'https://touch.example/' });
          await chrome.bookmarks.remove(n.id);
        });
      }
      fs.writeFileSync(a, JSON.stringify({ version: (/Chrome\/(\d+)/.exec(ua) || [])[1], userAgent: ua, tree }));
    } else if (cmd === 'canon') {
      const urls = JSON.parse(fs.readFileSync(a, 'utf8'));
      const res = await sw.evaluate(async (urls) => {
        const other = (await chrome.bookmarks.getTree())[0].children.find(n => n.folderType === 'other');
        const r = [];
        for (const u of urls) {
          try { r.push({ raw: u, canon: (await chrome.bookmarks.create({ parentId: other.id, title: 't', url: u })).url }); }
          catch (e) { r.push({ raw: u, error: String((e && e.message) || e) }); }
        }
        return r;
      }, urls);
      fs.writeFileSync(b, JSON.stringify(res));
    } else if (cmd === 'hold') {
      console.log('ready');
      await new Promise(resolve => { process.stdin.on('end', resolve); process.stdin.resume(); });
    } else {
      throw new Error(`unknown command ${cmd}`);
    }
  } finally {
    await ctx.close();   // a clean close flushes Bookmarks immediately (no need to wait for the 2.5 s save timer)
  }
}
main().catch(e => { console.error(e.stack || e); process.exit(1); });
