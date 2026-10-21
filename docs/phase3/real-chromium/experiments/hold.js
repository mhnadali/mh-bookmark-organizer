'use strict';
// Keep Chromium running on a profile while the shell does things to the profile files.
// usage: node hold.js <userDataDir> <ready-file> <go-file> [--change]   (--change: add one bookmark after <go-file> appears, then close)
const fs = require('fs');
const { launch } = require('./lib');
(async () => {
  const a = process.argv.slice(2);
  const change = a.includes('--change');
  const [udd, ready, go] = a.filter(x => !x.startsWith('--'));
  const { ctx, sw } = await launch(udd, {});
  fs.writeFileSync(ready, 'ready');
  while (!fs.existsSync(go)) await new Promise(r => setTimeout(r, 100));
  let info = {};
  info.before = await sw.evaluate(async () => { let n = 0; (function w(x) { n++; (x.children || []).forEach(w); })((await chrome.bookmarks.getTree())[0]); return n; });
  if (change) await sw.evaluate(async () => { const t = (await chrome.bookmarks.getTree())[0].children; await chrome.bookmarks.create({ parentId: t[0].id, title: 'added by the running browser', url: 'https://running.example/' }); });
  await ctx.close();
  console.log(JSON.stringify(info));
})().catch(e => { console.error(e); process.exit(1); });
