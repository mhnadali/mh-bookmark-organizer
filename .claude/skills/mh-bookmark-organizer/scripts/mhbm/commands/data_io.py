"""Command group: data_io."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .. import browsers, formats
from ..i18n import t as tr
from ..model import assign_ids
from ..urlnorm import normalize_url
from .common import Out, _lib


def _ingest(lib, items, o, *, skip_existing=False, allow_duplicates=False, label="", show_skipped=False):
    """By default a bookmark already in the library *in the same folder* is skipped, so re-importing a fresh export is safe."""
    for b in items:
        b.norm = normalize_url(b.url)
    fresh = list(items)
    skipped = 0
    if not allow_duplicates:
        existing = lib.all(snippet=0)
        if skip_existing:
            have = {b.norm for b in existing}
            fresh = [b for b in items if b.norm not in have]
        else:
            have = {(b.norm, b.folder) for b in existing}
            fresh = []
            for b in items:
                if (b.norm, b.folder) in have:
                    continue
                have.add((b.norm, b.folder))
                fresh.append(b)
        skipped = len(items) - len(fresh)
    assign_ids(fresh, lib.ids())
    if lib.count():
        o.say("snapshot:", lib.snapshot("pre-import"))
    with lib.op("import"):
        lib.upsert(fresh)
        lib.add_folders(getattr(items, "folders", set()))
    lib.save_icons(fresh)
    lib.save_folder_meta(getattr(items, "folder_meta", {}))
    fresh_ids = {id(b) for b in fresh}
    repeats = [{"url": b.url, "folder": b.folder} for b in items if id(b) not in fresh_ids]
    o.set(source=label, read=len(items), imported=len(fresh), skipped_existing=skipped, skipped_items=repeats[:200],
          library=lib.count())
    if show_skipped:
        for r in repeats[:50]:
            o.say(f"  skipped: [{r['folder'] or 'unfiled'}] {r['url']}")
    o.say(tr("imported", src=label, read=len(items), imported=len(fresh), skipped=skipped, library=lib.count()))


# ------------------------------------------------------------------ commands


def cmd_import(args, S):
    lib, o = _lib(args), Out(args)
    for f in args.files:
        _ingest(lib, formats.read_file(f, args.format), o, skip_existing=args.skip_existing,
                allow_duplicates=args.allow_duplicates, label=str(f), show_skipped=args.show_skipped)
    o.finish()


def cmd_import_browser(args, S):
    lib, o = _lib(args), Out(args)
    items, sources, others, names = browsers.load_sources(args.name, args.profile, args.all_profiles)
    _ingest(lib, items, o, skip_existing=args.skip_existing, allow_duplicates=args.allow_duplicates, label=args.name,
            show_skipped=args.show_skipped)
    o.set(sources=[str(p) for p in sources], profiles=names, other_profiles=others)
    if others:
        o.say(f"note: {others} other profile(s) of {args.name} were not imported; `browsers` lists them, then use --profile NAME or --all-profiles")
    o.finish()


def cmd_browsers(args, S):
    o = Out(args)
    found = browsers.detect()
    profiles = browsers.describe()
    o.set(browsers=found, profiles=profiles)
    for name in browsers.ALL:
        mine = [p for p in profiles if p["browser"] == name]
        if not mine:
            continue
        o.say(name)
        for p in mine:
            flags = ", ".join(f for f, on in (("default", p["default"]), ("signed in", p["signed_in"])) if on)
            o.say(f'    "{p["name"]}" [{p["profile"]}]' + (f" - {flags}" if flags else ""))
            o.say("       ", p["path"])
            for note in p["notes"]:
                o.say("        note:", note)
    if not profiles:
        o.say("no browser profiles found here; export HTML from the browser's bookmark manager instead")
    o.finish()


def cmd_export(args, S):
    lib, o = _lib(args), Out(args)
    bms = lib.all(snippet=600 if args.format == "obsidian" else 0)
    folders = lib.folders()
    if args.folder:
        pre = args.folder + "/"
        bms = [b for b in bms if b.folder == args.folder or b.folder.startswith(pre)]
        folders = {f for f in folders if f == args.folder or f.startswith(pre)}
    if args.tag:
        bms = [b for b in bms if args.tag in b.tags]
        folders = set()
    bms.sort(key=lambda b: (b.folder.lower(), (b.title or b.url).lower()))
    if args.format == "obsidian":
        if not args.out:
            sys.exit("--out DIRECTORY is required for obsidian export")
        n = formats.write_obsidian(bms, args.out)
        o.say(f"wrote {n} notes to {args.out}")
        o.set(written=n, out=args.out)
        return o.finish()
    text = formats.render(bms, args.format, folders, icons=lib.icons() if args.format == "html" else None,
                          folder_meta=lib.folder_meta() if args.format in ("html", "chrome") else None)
    if args.out:
        from .. import profilesafe

        out_path = Path(args.out)
        if profilesafe.looks_like_browser_profile_file(out_path):
            sys.exit(f"{out_path} is a browser profile file: exporting over it would bypass the checks `install-chrome` makes (browser closed, backup, "
                     "verification, atomic write). Use `install-chrome`, or export to another file.")
        profilesafe.atomic_write_text(out_path, text)       # a full disk or a crash leaves the old file, never half a new one
        o.say(f"wrote {len(bms)} bookmarks to {args.out} ({args.format})")
        o.set(written=len(bms), out=args.out)
        o.finish()
    else:
        sys.stdout.write(text)


def _preflight_or_exit(args, target: Path, *, rewrites_everything: bool):
    from .. import profilesafe

    pf = profilesafe.preflight(target, allow_synced=args.allow_synced_profile, ignore_lock=args.ignore_profile_lock,
                               rewrites_everything=rewrites_everything)
    if pf.errors:
        sys.exit("\n".join(pf.errors))
    return pf


def cmd_install_chrome(args, S):
    from .. import profilesafe

    lib, o = _lib(args), Out(args)
    target = Path(args.profile_file)
    restoring = args.restore is not None
    if target.name != "Bookmarks" or not (target.is_file() or (restoring and target.parent.is_dir())):
        sys.exit("target must be an existing Chromium-family profile 'Bookmarks' file (see `browsers`)")
    if not args.confirm_chrome_closed:
        sys.exit("Close the browser completely (check Task Manager), then re-run with --confirm-chrome-closed. "
                 "The browser overwrites this file on exit and Sync may restore old data.")
    pf = _preflight_or_exit(args, target, rewrites_everything=not restoring)
    if restoring:
        res = profilesafe.restore_backup(target, None if args.restore == "latest" else Path(args.restore))
        o.say(f"restored {res['restored'].name} as {target}")
        if res["previous_saved_as"]:
            o.say(f"what it replaced was kept as {res['previous_saved_as']}")
        o.set(restored=str(res["restored"]), previous_saved_as=str(res["previous_saved_as"] or ""),
              twins_restored=[str(p) for p in res["twins_restored"]], warnings=pf.warnings)
        return o.finish()
    before = profilesafe.fingerprint(target)
    bms = sorted(lib.all(snippet=0), key=lambda b: (b.folder.lower(), (b.title or b.url).lower()))
    skipped: list = []
    try:
        text = formats.write_chrome(bms, lib.folders(), lib.folder_meta(), skipped)
    except formats.FormatError as e:
        sys.exit(str(e))
    problems = [p for p in formats.verify_chrome(json.loads(text)) if not p.startswith("no checksum")]
    if problems:
        sys.exit("refusing to write a file that fails verify-chrome: " + "; ".join(problems[:3]))
    profilesafe.assert_unchanged(target, before)
    res = profilesafe.write_profile_file(target, text)
    backup = res["backup"]
    for w in pf.warnings:
        o.say("note:", w)
    o.say(f"wrote {len(bms) - len(skipped)} bookmarks to {target}\noriginal saved to {backup}")
    for twin in res["moved_aside"]:
        o.say(f"moved aside: {twin}")
    o.say("note: bookmarks are written in alphabetical order (the library does not keep the browser's order); "
          "`sync` patches the file in place and keeps it. Undo with `install-chrome FILE --restore --confirm-chrome-closed`.")
    if skipped:
        o.say(f"left out {len(skipped)} bookmark(s) the browser would silently discard (no scheme, empty host...):")
        for b in skipped[:10]:
            o.say(f"  [{b.folder or 'unfiled'}] {b.url[:70]!r}")
    o.set(written=len(bms) - len(skipped), backup=str(backup), not_written=[b.url for b in skipped],
          moved_aside=[str(p) for p in res["moved_aside"]], warnings=pf.warnings)
    o.finish()


def cmd_verify_chrome(args, S):
    o = Out(args)
    data = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
    problems = formats.verify_chrome(data)
    o.set(file=args.file, ok=not [p for p in problems if not p.startswith("no checksum")], problems=problems)
    if problems:
        for p in problems[:30]:
            o.say("problem:", p)
    else:
        o.say(f"{args.file}: structure and checksum look valid")
    o.finish()
    return 0 if o.data["ok"] else 2


def _sync_report(o, plan, args) -> None:
    summary = plan.summary()
    o.set(**summary)
    o.say("sync plan: " + ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in summary.items() if v))
    for b in plan.add_local[:args.show]:
        o.say(f"  + library  [{b.folder}] {b.title[:50]}")
    for b in plan.add_remote[:args.show]:
        o.say(f"  + browser  [{b.folder}] {b.title[:50]}")
    for b in plan.delete_local[:args.show]:
        o.say(f"  - library  [{b.folder}] {b.title[:50]}")
    for b in plan.unsyncable[:args.show]:
        o.say(f"  ! kept in the library only (the browser would silently drop this URL): {b.url[:60]!r}")
    for b in plan.collapsed[:args.show]:
        o.say(f"  ! kept in the library only (another bookmark has the same address and folder in the browser's eyes; `dedupe` merges them): {b.url[:60]!r}")


def _sync_prepare_browser_file(args, o, target: Path, data, plan, before):
    """Everything that can refuse a write to the browser's file, before anything changes. Returns the patched document."""
    from .. import profilesafe
    from .. import sync as sync_mod

    if target.name == "Bookmarks.bak":
        sys.exit("the browser never reads Bookmarks.bak (it is only the previous save), so patching it changes nothing; sync with the real Bookmarks file")
    if not args.confirm_chrome_closed:
        sys.exit("the browser file would change: close the browser completely, then re-run with --confirm-chrome-closed")
    for w in _preflight_or_exit(args, target, rewrites_everything=False).warnings:
        o.say("note:", w)
    patched = sync_mod.patch_chrome(data, plan)          # may refuse (damaged file)
    profilesafe.assert_unchanged(target, before)         # the browser must not have written since we read
    return patched


def _sync_apply_library(lib, plan) -> None:
    from ..model import assign_ids

    with lib.op("sync"):
        fresh = list(plan.add_local)
        for b in fresh:
            b.norm = normalize_url(b.url)
        assign_ids(fresh, lib.ids())
        lib.upsert(fresh)
        lib.delete(b.id for b in plan.delete_local)
        retitled = []
        for b, title in plan.retitle_local:
            b.title = title
            retitled.append(b)
        lib.upsert(retitled)
        lib.add_folders(plan.new_folders_local)


def cmd_sync(args, S):
    """Two-way sync with a Chromium-family Bookmarks file (browser closed)."""
    from .. import profilesafe
    from .. import sync as sync_mod

    lib, o = _lib(args), Out(args)
    target = Path(args.profile_file)
    if (target.name not in ("Bookmarks", "Bookmarks.bak") and target.suffix != ".json") or not target.is_file():
        sys.exit("give an existing Chromium-family profile 'Bookmarks' file (see `browsers`)")
    profile = str(target.resolve())
    if args.status:
        meta = lib.sync_meta(profile)
        o.set(sync=meta)
        o.say("never synced" if not meta else f"last sync {time.strftime('%Y-%m-%d %H:%M', time.localtime(meta['last_sync']))}: {meta['summary']}")
        return o.finish()
    before = profilesafe.fingerprint(target)
    data, remote = sync_mod.read_remote(target)
    plan = sync_mod.plan_sync(lib.all(snippet=0), remote, lib.sync_baseline(profile), prefer=args.prefer, force=args.force)
    _sync_report(o, plan, args)
    if not args.apply:
        o.say("dry run; add --apply (and --confirm-chrome-closed when the browser file changes)")
        return o.finish()
    patched = _sync_prepare_browser_file(args, o, target, data, plan, before) if plan.remote_changes else None
    o.say("snapshot:", lib.snapshot("pre-sync"))
    if patched is not None:
        backup = profilesafe.write_profile_file(target, json.dumps(patched, indent=3, ensure_ascii=False))["backup"]
        o.say(f"browser file updated; original kept as {backup}")
        o.set(backup=str(backup))
    _sync_apply_library(lib, plan)
    # the baseline is what BOTH sides looked like when the plan was made (plus what we wrote), never a fresh read of the file: a bookmark the
    # browser saved meanwhile would be recorded as "synced", and the next sync would then delete it from the browser as "removed from the library"
    merged = list(formats.parse_chrome(patched)) if patched is not None else remote
    lib.save_sync_baseline(profile, {sync_mod.key_of(b.url, b.folder): b.title for b in merged}, plan.summary())
    o.say("synced")
    o.finish()


def cmd_backups(args, S):
    lib, o = _lib(args), Out(args)
    if args.restore:
        n = lib.restore(args.restore)
        o.say(f"restored {n} bookmarks")
        o.set(restored=n)
    else:
        snaps = [str(p) for p in lib.snapshots()]
        o.set(snapshots=snaps)
        for p in snaps:
            o.say(p)
    o.finish()


def cmd_history(args, S):
    lib, o = _lib(args), Out(args)
    ops = lib.history(args.limit)
    o.set(operations=ops)
    for r in ops:
        o.say(f"#{r['op']:<4} {time.strftime('%Y-%m-%d %H:%M', time.localtime(r['ts']))}  {r['command']:<16} "
              f"{r['changes']} bookmark(s){'  (undone)' if r['undone'] else ''}")
    o.finish()


def cmd_undo(args, S):
    lib, o = _lib(args), Out(args)
    try:
        res = lib.undo(args.op or 0)
    except ValueError as e:
        sys.exit(str(e))
    lib.add_folders(lib.used_folders())
    o.set(**res)
    o.say(tr("undid", op=res["op"], command=res["command"], restored=res["restored"], removed=res["removed"]))
    o.finish()


def _profile_guard_flags(sp) -> None:
    sp.add_argument("--ignore-profile-lock", action="store_true", help="write even though a browser lock file says it is running (only if you are sure it is not)")
    sp.add_argument("--allow-synced-profile", action="store_true", help="write into a profile with Chrome Sync on (the sync engine may restore old data)")


def register(add, apply_flag, net_flags):
    sp = add("import", cmd_import, "import HTML/Chrome JSON/Firefox JSON/CSV/JSON (also Pocket, Raindrop, Instapaper, Omnivore, Karakeep, Linkwarden)")
    sp.add_argument("files", nargs="+")
    sp.add_argument("--format", choices=formats.READ_FORMATS)
    sp.add_argument("--skip-existing", action="store_true", help="also skip URLs already present in any other folder")
    sp.add_argument("--allow-duplicates", action="store_true", help="import everything, even exact repeats")
    sp.add_argument("--show-skipped", action="store_true", help="list the bookmarks that were skipped")

    sp = add("import-browser", cmd_import_browser, "read bookmarks directly from an installed browser")
    sp.add_argument("name", help=f"{', '.join(browsers.ALL)} or a profile file/folder path")
    sp.add_argument("--profile", help="a profile's folder name or display name (see `browsers`), or an explicit "
                    "Bookmarks/places.sqlite/Bookmarks.plist/bookmarks-*.jsonlz4 file or profile folder")
    sp.add_argument("--all-profiles", action="store_true", help="import every profile of the browser, not just the one it opens")
    sp.add_argument("--skip-existing", action="store_true")
    sp.add_argument("--allow-duplicates", action="store_true")
    sp.add_argument("--show-skipped", action="store_true")

    add("browsers", cmd_browsers, "list detected browser profiles")

    add("chrome-path", cmd_browsers, "alias of `browsers`")

    sp = add("export", cmd_export, "export to html|chrome|csv|json|md|obsidian|raindrop|pocket|linkwarden|karakeep|instapaper")
    sp.add_argument("--format", choices=formats.WRITE_FORMATS, default="html")
    sp.add_argument("--out", help="new file (new directory for obsidian; existing destinations are not overwritten)")
    sp.add_argument("--folder")
    sp.add_argument("--tag")

    sp = add("install-chrome", cmd_install_chrome, "write the library into a Chromium-family profile (browser must be closed); --restore undoes it")
    sp.add_argument("profile_file")
    sp.add_argument("--confirm-chrome-closed", action="store_true")
    sp.add_argument("--restore", nargs="?", const="latest", metavar="BACKUP", help="put the newest backup (or the named one) back instead of writing")
    _profile_guard_flags(sp)

    add("verify-chrome", cmd_verify_chrome, "check a Chromium Bookmarks file: ids, GUIDs, checksum").add_argument("file")

    sp = add("backups", cmd_backups, "list snapshots or --restore FILE")
    sp.add_argument("--restore")

    add("history", cmd_history, "list undoable operations").add_argument("--limit", type=int, default=20)

    add("undo", cmd_undo, "undo the last operation (or --op N)").add_argument("--op", type=int)

    sp = add("sync", cmd_sync, "two-way sync with a Chromium Bookmarks file (browser closed); patches it in place")
    sp.add_argument("profile_file")
    sp.add_argument("--prefer", choices=["remote", "local"], default="remote", help="who wins a rename conflict")
    sp.add_argument("--force", action="store_true", help="allow a sync that deletes many bookmarks")
    sp.add_argument("--status", action="store_true")
    sp.add_argument("--confirm-chrome-closed", action="store_true")
    _profile_guard_flags(sp)
    sp.add_argument("--apply", action="store_true")
    sp.add_argument("--show", type=int, default=10)
