'use strict';
// Experiment A: empty profile + probe extension -> create bookmarks through chrome.bookmarks -> quit -> keep the Bookmarks file Chromium wrote.
// usage: node exp_a_create.js <userDataDir> <spec.json> <treeOut.json> [logFile] [--bin path]
const fs = require('fs');
const { launch, settle, getTree, build } = require('./lib');
(async () => {
  const a = process.argv.slice(2);
  const bi = a.indexOf('--bin'); const bin = bi >= 0 ? a.splice(bi, 2)[1] : undefined;
  const [udd, specFile, treeOut, logFile] = a;
  const t0 = Date.now();
  const { ctx, sw } = await launch(udd, { logFile, logging: !!logFile, bin });
  const t1 = Date.now();
  const before = await getTree(sw);
  const spec = JSON.parse(fs.readFileSync(specFile, 'utf8'));
  const errors = await build(sw, spec);
  const tree = await getTree(sw);
  fs.writeFileSync(treeOut, JSON.stringify({ before, errors, tree }, null, 1));
  await settle();
  await ctx.close();
  console.log(JSON.stringify({ launch_ms: t1 - t0, total_ms: Date.now() - t0, errors }));
})().catch(e => { console.error(e); process.exit(1); });
