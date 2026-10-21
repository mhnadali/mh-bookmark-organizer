'use strict';
// Launch Chromium on a profile and print the number of bookmark nodes (flattened inside the service worker, so very deep trees are fine).
// usage: node exp_count.js <userDataDir> [--bin path]
const { launch } = require('./lib');
(async () => {
  const a = process.argv.slice(2);
  const bi = a.indexOf('--bin'); const bin = bi >= 0 ? a.splice(bi, 2)[1] : undefined;
  const { ctx, sw } = await launch(a[0], { bin });
  const n = await sw.evaluate(async () => {
    let count = 0;
    (function w(x) { count++; for (const c of (x.children || [])) w(c); })((await chrome.bookmarks.getTree())[0]);
    return count;
  });
  console.log(JSON.stringify({ nodes: n }));
  await ctx.close();
})().catch(e => { console.error(e); process.exit(1); });
