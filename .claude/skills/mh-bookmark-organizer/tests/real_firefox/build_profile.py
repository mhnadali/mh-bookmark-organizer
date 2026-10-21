#!/usr/bin/env python3
"""Have REAL Gecko write a Firefox profile (places.sqlite + bookmarkbackups/*.jsonlz4) from a synthetic Netscape file.

usage: MHBM_FIREFOX_DIR=<dir with `firefox`> python3 -I build_profile.py <out-profile-dir> <source.html>
           [--extras] [--end clean|kill|term] [--no-backup] [--workdir DIR]

  --extras   after the HTML import, add things HTML cannot express through Gecko's own PlacesUtils API
             (mobile root bookmark, API tags, keyword with POST data, separator, livemark attempt, smart-bookmark annotation)
  --end      clean = Marionette:Quit(eAttemptQuit) so Firefox checkpoints the WAL and releases the lock;
             kill  = SIGKILL right after the writes (WAL not checkpointed, lock file left behind);
             term  = SIGTERM (Firefox's nsProfileLock handler removes `lock` and re-raises: still no checkpoint)

Writes next to the profile:  <out>.truth.json  (the bookmark tree exactly as Gecko reports it: PlacesUtils.promiseBookmarksTree
plus tags/keywords), <out>.export.html (Firefox's own HTML export), <out>.backup.json (plain JSON backup).
The whole thing runs in a loopback-only network namespace (re-exec through `unshare -n`).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ffenv
import marionette

EXTRAS_JS = r"""
const { PlacesUtils } = ChromeUtils.importESModule("resource://gre/modules/PlacesUtils.sys.mjs");
const log = {};
const B = PlacesUtils.bookmarks;
async function step(name, fn) { try { log[name] = (await fn()) ?? "ok"; } catch (e) { log[name] = "ERROR " + e; } }
await step("mobile", async () => (await B.insert({parentGuid: B.mobileGuid, url: "https://mobile.example.org/1", title: "Mobile one"})).guid);
await step("mobile_folder", async () => {
  const f = await B.insert({parentGuid: B.mobileGuid, type: B.TYPE_FOLDER, title: "Mobile folder"});
  await B.insert({parentGuid: f.guid, url: "https://mobile.example.org/2", title: "In mobile folder"});
  return f.guid;
});
await step("api_tags", async () => {
  await B.insert({parentGuid: B.unfiledGuid, url: "https://api.example.org/tagged", title: "API tagged"});
  PlacesUtils.tagging.tagURI(Services.io.newURI("https://api.example.org/tagged"), ["api-tag", "Zeta", "تگب"]);
});
await step("keyword_post", async () => {
  await B.insert({parentGuid: B.toolbarGuid, url: "https://api.example.org/search?q=%s", title: "Search with POST"});
  await PlacesUtils.keywords.insert({keyword: "kwpost", url: "https://api.example.org/search?q=%s", postData: "q=%s"});
});
await step("separator", async () => (await B.insert({parentGuid: B.toolbarGuid, type: B.TYPE_SEPARATOR})).guid);
await step("smart_query", async () => {
  const q = await B.insert({parentGuid: B.menuGuid, url: "place:parent=" + B.toolbarGuid + "&queryType=1", title: "Query on toolbar"});
  return q.guid;
});
await step("livemark", async () => {
  if (!PlacesUtils.livemarks) return "PlacesUtils.livemarks is gone in this build";
  const l = await PlacesUtils.livemarks.addLivemark({title: "A feed", parentGuid: B.menuGuid,
      feedURI: Services.io.newURI("https://feeds.example.org/rss.xml"), siteURI: Services.io.newURI("https://feeds.example.org/")});
  return l.guid;
});
return log;
"""

BULK_JS = r"""
const { PlacesUtils } = ChromeUtils.importESModule("resource://gre/modules/PlacesUtils.sys.mjs");
const N = arguments[0];
const B = PlacesUtils.bookmarks;
let seed = 12345;
const rnd = () => { seed ^= seed << 13; seed >>>= 0; seed ^= seed >>> 17; seed ^= seed << 5; seed >>>= 0; return seed / 4294967296; };
const words = ["garden","comet","dovetail","gambit","sourdough","gearing","aperture","mainsail","kiln","warbler","kernel","ledger","باغ","ستاره","نان","شطرنج"];
const pick = a => a[Math.floor(rnd() * a.length)];
const folders = [];
for (let i = 0; i < Math.max(10, N / 100); i++) {
  const kids = [];
  const per = 60 + Math.floor(rnd() * 80);
  for (let j = 0; j < per && folders.length * 100 + kids.length < N; j++) {
    kids.push({url: `https://${pick(words).replace(/[^a-z]/g, "x")}${i % 97}.example.org/${i}/${j}?id=${Math.floor(rnd() * 1e6)}`,
               title: `${pick(words)} ${pick(words)} ${pick(words)} ${j}`, dateAdded: new Date((1500000000 + Math.floor(rnd() * 1.2e8)) * 1000)});
  }
  folders.push({type: B.TYPE_FOLDER, title: `${pick(words)} ${i}`, children: kids});
  if (folders.reduce((n, f) => n + f.children.length, 0) >= N) break;
}
let total = 0;
for (let i = 0; i < folders.length; i += 50) {
  const batch = folders.slice(i, i + 50);
  await B.insertTree({guid: B.toolbarGuid, children: batch});
  total += batch.reduce((n, f) => n + f.children.length, 0);
}
// tag ~3% of them
let tagged = 0;
for (let i = 0; i < folders.length; i += 3) {
  for (const c of folders[i].children.slice(0, 5)) { PlacesUtils.tagging.tagURI(Services.io.newURI(c.url), ["bulk", pick(words)]); tagged++; }
}
return {inserted: total, folders: folders.length, tagged};
"""

DUMP_JS = r"""
const { PlacesUtils } = ChromeUtils.importESModule("resource://gre/modules/PlacesUtils.sys.mjs");
const tree = await PlacesUtils.promiseBookmarksTree("", {includeItemIds: true});
const kw = [];
await PlacesUtils.withConnectionWrapper("truth", async db => {
  const rows = await db.execute("SELECT keyword, h.url, post_data FROM moz_keywords k JOIN moz_places h ON h.id = k.place_id");
  for (const r of rows) kw.push([r.getResultByName("keyword"), r.getResultByName("url"), r.getResultByName("post_data")]);
});
const tags = [];
await PlacesUtils.withConnectionWrapper("truth2", async db => {
  const rows = await db.execute("SELECT t.title, h.url FROM moz_bookmarks b JOIN moz_bookmarks t ON t.id = b.parent JOIN moz_places h ON h.id = b.fk WHERE t.parent = (SELECT id FROM moz_bookmarks WHERE guid = 'tags________') ORDER BY t.title, h.url");
  for (const r of rows) tags.push([r.getResultByName("title"), r.getResultByName("url")]);
});
return {tree, keywords: kw, tag_rows: tags, version: Services.appinfo.version, buildID: Services.appinfo.appBuildID};
"""

BACKUP_JS = r"""
const { PlacesBackups } = ChromeUtils.importESModule("resource://gre/modules/PlacesBackups.sys.mjs");
const { BookmarkHTMLUtils } = ChromeUtils.importESModule("resource://gre/modules/BookmarkHTMLUtils.sys.mjs");
const jsonPath = arguments[0], htmlPath = arguments[1];
const out = {};
out.create = String(await PlacesBackups.create());
out.json = (await PlacesBackups.saveBookmarksToJSONFile(jsonPath)) + "";
await BookmarkHTMLUtils.exportToFile(htmlPath);
out.html = "ok";
return out;
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("profile")
    ap.add_argument("html")
    ap.add_argument("--extras", action="store_true")
    ap.add_argument("--end", choices=("clean", "kill", "term"), default="clean")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--bulk", type=int, default=0, help="also insert N synthetic bookmarks (deterministic) through Gecko's insertTree")
    ap.add_argument("--workdir")
    args = ap.parse_args()
    ffenv.ensure_netns()

    prof = Path(args.profile).resolve()
    shutil.rmtree(prof, ignore_errors=True)
    home = Path(args.workdir or str(prof) + ".home")
    shutil.rmtree(home, ignore_errors=True)
    ffenv.write_user_js(prof, {"browser.places.importBookmarksHTML": True, "browser.bookmarks.file": str(Path(args.html).resolve()),
                               "browser.bookmarks.restore_default_bookmarks": False})
    t0 = time.time()
    proc = ffenv.launch(prof, home, log=Path(str(prof) + ".log"), netns=False, extra_args=["-marionette", "--remote-allow-system-access"])
    report: dict = {"start": None}
    try:
        m = marionette.Marionette()
        m.connect(90)
        m.new_session()
        m.chrome()
        report["start"] = round(time.time() - t0, 1)
        # wait until the HTML import has finished (the pref flips back to false once nsBrowserGlue is done)
        for _ in range(200):
            done = m.run("return !Services.prefs.getBoolPref('browser.places.importBookmarksHTML', false);")
            if done:
                break
            time.sleep(0.25)
        report["import_done"] = round(time.time() - t0, 1)
        if args.extras:
            report["extras"] = m.run(EXTRAS_JS)
        if args.bulk:
            t_b = time.time()
            report["bulk"] = m.run(BULK_JS, [args.bulk])
            report["bulk_seconds"] = round(time.time() - t_b, 1)
        truth = m.run(DUMP_JS)
        Path(str(prof) + ".truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8")
        if not args.no_backup:
            report["backup"] = m.run(BACKUP_JS, [str(prof) + ".backup.json", str(prof) + ".export.html"])
        if args.end == "clean":
            m.quit(("eAttemptQuit",))
            try:
                proc.wait(60)
                report["exit"] = proc.returncode
            except Exception:
                report["exit"] = "timeout"
        else:
            import signal
            ffenv.kill_group(proc, signal.SIGKILL if args.end == "kill" else signal.SIGTERM)
            report["exit"] = proc.returncode
    finally:
        ffenv.kill_group(proc)
    report["elapsed"] = round(time.time() - t0, 1)
    report["files"] = {f: os.lstat(prof / f).st_size for f in sorted(os.listdir(prof)) if f.startswith(("places", "lock", ".parentlock"))}
    report["backups"] = sorted(os.listdir(prof / "bookmarkbackups")) if (prof / "bookmarkbackups").is_dir() else []
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
