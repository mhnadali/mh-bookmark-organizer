'use strict';
// Shared helpers for the real-Chromium experiments (Playwright for Node, persistent context + MV3 probe extension).
const path = require('path');
const fs = require('fs');

function loadPlaywright() {
  const cands = [process.env.PLAYWRIGHT_MODULE, 'playwright', '@playwright/test', '/opt/node22/lib/node_modules/playwright'].filter(Boolean);
  for (const c of cands) { try { return require(c); } catch (e) { /* next */ } }
  throw new Error('playwright not found; set PLAYWRIGHT_MODULE');
}

const EXT = path.join(__dirname, 'testext');
const DEFAULT_BIN = '/opt/pw-browsers/chromium-1194/chrome-linux/chrome';

async function launch(userDataDir, opts = {}) {
  const { chromium } = loadPlaywright();
  const bin = opts.bin || process.env.CHROMIUM_BIN || DEFAULT_BIN;
  // Branded builds (Chrome >= 137, probably Brave/Edge too) ignore --load-extension unless this feature is disabled; unknown feature names are ignored by older builds.
  const args = ['--headless=new', `--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`, '--disable-features=DisableLoadExtensionCommandLineSwitch', ...(opts.args || [])];
  // Chromium logging goes to a file (stderr is swallowed by Playwright): LOG(ERROR/WARNING) lines plus VLOG for the bookmark code.
  if (opts.logFile) args.push('--enable-logging', `--log-file=${path.resolve(opts.logFile)}`, '--v=0', '--vmodule=bookmark*=2,*bookmark*=2');
  const ctx = await chromium.launchPersistentContext(userDataDir, {
    executablePath: bin, headless: false, args, ignoreDefaultArgs: opts.ignoreDefaultArgs || false,
    timeout: 60000,
  });
  let sw = ctx.serviceWorkers()[0];
  if (!sw) sw = await ctx.waitForEvent('serviceworker', { timeout: 30000 });
  return { ctx, sw, bin };
}

// Wait until the browser has flushed its Bookmarks file (it debounces writes ~2.5 s) or `ms` elapsed.
async function settle(ms = 4500) { await new Promise(r => setTimeout(r, ms)); }

async function getTree(sw) { return sw.evaluate(() => chrome.bookmarks.getTree()); }

async function build(sw, spec) {
  // spec: {bar:[node], other:[node], mobile:[node]}; node = {title,url} | {title,children:[node]}
  return sw.evaluate(async (spec) => {
    const parents = { bar: '1', other: '2', mobile: '3' };
    const errors = [];
    async function mk(parentId, node) {
      try {
        const args = { parentId, title: node.title };
        if (node.url !== undefined) args.url = node.url;
        const made = await chrome.bookmarks.create(args);
        if (node.children) for (const c of node.children) await mk(made.id, c);
      } catch (e) { errors.push({ node: node.title, error: String(e && e.message || e) }); }
    }
    for (const [root, nodes] of Object.entries(spec)) for (const n of nodes) await mk(parents[root], n);
    return errors;
  }, spec);
}

async function closeContext(ctx) { await ctx.close(); }

module.exports = { launch, settle, getTree, build, closeContext, EXT, DEFAULT_BIN, loadPlaywright };
