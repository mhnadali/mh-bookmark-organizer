'use strict';
// Probe a profile: try to create under each permanent folder, report errors, then dump what the file looks like afterwards.
// usage: node probe_model.js <userDataDir>
const { launch, settle } = require('./lib');
(async () => {
  const { ctx, sw } = await launch(process.argv[2], {});
  const out = await sw.evaluate(async () => {
    const r = {};
    r.tree = (await chrome.bookmarks.getTree())[0].children.map(c => ({ id: c.id, title: c.title, n: (c.children || []).length, folderType: c.folderType }));
    for (const p of ['0', '1', '2', '3']) {
      try { const n = await chrome.bookmarks.create({ parentId: p, title: 'probe' + p, url: 'https://probe' + p + '.example/' }); r['create_' + p] = 'ok id=' + n.id; }
      catch (e) { r['create_' + p] = String(e.message || e); }
    }
    try { r.search = (await chrome.bookmarks.search({})).length; } catch (e) { r.search = String(e); }
    return r;
  });
  console.log(JSON.stringify(out));
  await settle(3500);
  await ctx.close();
})().catch(e => { console.error(e); process.exit(1); });
