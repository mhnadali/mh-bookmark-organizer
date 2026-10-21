'use strict';
// Ask real Chromium to canonicalise URLs: chrome.bookmarks.create() stores GURL(url).spec().  Reads a JSON array, writes [{raw, canon|error}].
// usage: node exp_canon.js <userDataDir> <urls.json> <out.json> [--bin path]
const fs = require('fs');
const { launch } = require('./lib');
(async () => {
  const a = process.argv.slice(2);
  const bi = a.indexOf('--bin'); const bin = bi >= 0 ? a.splice(bi, 2)[1] : undefined;
  const [udd, inp, out] = a;
  const urls = JSON.parse(fs.readFileSync(inp, 'utf8'));
  const { ctx, sw } = await launch(udd, { bin });
  const res = await sw.evaluate(async (urls) => {
    const r = [];
    for (const u of urls) {
      try { const n = await chrome.bookmarks.create({ parentId: '2', title: 't', url: u }); r.push({ raw: u, canon: n.url }); }
      catch (e) { r.push({ raw: u, error: String(e.message || e) }); }
    }
    return r;
  }, urls);
  fs.writeFileSync(out, JSON.stringify(res));
  console.log(JSON.stringify({ n: res.length, errors: res.filter(x => x.error).length }));
  await ctx.close();
})().catch(e => { console.error(e); process.exit(1); });
