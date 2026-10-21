'use strict';
// Launch Chromium on a user-data-dir, dump chrome.bookmarks.getTree() and quit cleanly. Optionally let it settle so it rewrites the file.
// usage: node exp_dump.js <userDataDir> <treeOut.json> [logFile] [--wait ms] [--bin path] [--touch]
// --touch: create and delete one bookmark so that Chromium has a reason to rewrite the Bookmarks file.
const fs = require('fs');
const { launch, settle, getTree } = require('./lib');
(async () => {
  const a = process.argv.slice(2);
  const opt = (k, d) => { const i = a.indexOf(k); if (i < 0) return d; const v = a[i + 1]; a.splice(i, 2); return v; };
  const wait = parseInt(opt('--wait', '0'), 10);
  const bin = opt('--bin', undefined);
  const ti = a.indexOf('--touch'); const touch = ti >= 0; if (touch) a.splice(ti, 1);
  const [udd, out, logFile] = a;
  const t0 = Date.now();
  const { ctx, sw } = await launch(udd, { logFile, logging: !!logFile, bin });
  const tree = await getTree(sw);
  const version = await sw.evaluate(() => navigator.userAgent);
  fs.writeFileSync(out, JSON.stringify({ userAgent: version, tree }, null, 1));
  if (touch) {
    // NB: do not hard-code parentId '2': when Chromium reassigns ids (checksum mismatch) 'Other bookmarks' is no longer id 2.
    await sw.evaluate(async () => {
      const top = (await chrome.bookmarks.getTree())[0].children;
      const other = top.find(n => n.folderType === 'other') || top[top.length - 1];
      const n = await chrome.bookmarks.create({ parentId: other.id, title: 'touch', url: 'https://touch.example/' });
      await chrome.bookmarks.remove(n.id);
    });
  }
  if (wait) await settle(wait);
  await ctx.close();
  console.log(JSON.stringify({ ms: Date.now() - t0, userAgent: version }));
})().catch(e => { console.error(e); process.exit(1); });
