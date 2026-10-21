"""Command group: diag - doctor (diagnose and repair) and the guided wizard."""
from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

from .. import __version__, browsers, i18n, linkcheck, netsafe
from ..i18n import t as tr
from ..model import is_unfiled
from .common import BUILTIN, Out, _lib

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"


def _check_python():
    v = sys.version_info
    return (OK if v >= (3, 9) else FAIL), f"Python {v.major}.{v.minor}.{v.micro} on {platform.system()}", "mhbm needs Python 3.9+"


def _check_sqlite():
    try:
        mem = sqlite3.connect(":memory:")
        mem.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        fts = True
    except sqlite3.OperationalError:
        fts = False
    return (OK if fts else WARN), f"SQLite {sqlite3.sqlite_version}, FTS5 {'available' if fts else 'MISSING'}", \
        "" if fts else "search still works but is unranked; use a Python build with FTS5 for ranked search"


def _check_proxy():
    proxies = {k: v for k, v in urllib.request.getproxies().items() if k in ("http", "https")}
    if not proxies:
        return INFO, "no HTTP proxy configured: links are checked directly", ""
    shown = ", ".join(f"{k}={v.split('@')[-1]}" for k, v in proxies.items())  # never print credentials
    return INFO, f"HTTP proxy in use ({shown}); the proxy resolves host names, so local DNS failures are not judged", ""


def _check_dns():
    if os.environ.get("MHBM_NO_NETWORK") == "1":
        return INFO, "DNS check skipped (network access is switched off for this session)", ""
    if urllib.request.getproxies().get("https"):
        return INFO, "DNS check skipped (a proxy resolves names)", ""
    try:
        import socket

        socket.getaddrinfo("example.com", 443)
        return OK, "DNS works", ""
    except OSError as e:
        return WARN, f"DNS lookup failed ({e}); link checks will find nothing", "check your network, VPN or DNS settings"


def _check_tools():
    out = []
    pdf = shutil.which("pdftotext")
    out.append(("pdftotext", OK if pdf else WARN, "PDF text extraction available" if pdf else "pdftotext not installed: PDFs are saved but not searchable",
                "" if pdf else "install poppler-utils (Linux), poppler (macOS) or Xpdf tools (Windows)"))
    return out


def _check_home(lib):
    out = []
    root = lib.root
    if lib.read_only:       # opened read-only: not even a probe file may appear in the library folder
        out.append(("home", INFO, f"{root} was opened read-only; write access is not tested", ""))
    else:
        try:
            probe = root / ".doctor-write-test"
            probe.write_text("x")
            probe.unlink()
            out.append(("home", OK, f"{root} is writable", ""))
        except OSError as e:
            out.append(("home", FAIL, f"{root} is not writable ({e})", "choose another --home"))
    free = shutil.disk_usage(root).free / 2**20
    out.append(("disk", OK if free > 200 else WARN, f"{free:,.0f} MB free", "" if free > 200 else "archiving needs space"))
    health = lib.health()
    integrity = health["integrity"]
    out.append(("database", OK if integrity == "ok" else FAIL, f"integrity: {integrity}; layout {health['schema']}; {health['journal_mode']} journal",
                "" if integrity == "ok" else "restore from a snapshot: `backups`"))
    if health["journal_mode"] != "wal":
        out.append(("concurrency", INFO, "write-ahead logging is off here, so a scheduled `maintain`, the web UI and your shell wait for each other "
                    "instead of sharing the library", "harmless for one process at a time; set by MHBM_JOURNAL_MODE=delete or a file system without shared memory"))
    return out


def _check_index(lib, fix):
    out = []
    if not lib.fts:
        return [("search index", WARN, "FTS5 unavailable", "")]
    rows = lib.db.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0]
    idx = lib.db.execute("SELECT COUNT(*) FROM bm_fts").fetchone()[0]
    gaps = lib.index_gaps()
    if rows != idx or gaps:
        what = f"{idx} entries for {rows} bookmarks" + (f", {gaps} not matching" if gaps else "")
        if fix:
            lib.reindex_all()
            out.append(("search index", OK, f"was out of sync ({what}); rebuilt", ""))
        else:
            out.append(("search index", WARN, f"out of sync ({what})", "run `doctor --fix`"))
    else:
        out.append(("search index", OK, f"{idx} entries, in sync", ""))
    orphans = lib.db.execute("SELECT (SELECT COUNT(*) FROM pages WHERE id NOT IN (SELECT id FROM bookmarks)) + "
                             "(SELECT COUNT(*) FROM icons WHERE id NOT IN (SELECT id FROM bookmarks))").fetchone()[0]
    if orphans and fix:
        lib.gc()
        out.append(("orphans", OK, f"removed {orphans} orphaned page/icon rows", ""))
    elif orphans:
        out.append(("orphans", INFO, f"{orphans} page/icon rows belong to deleted bookmarks (kept so undo can restore them)", "`doctor --fix` removes them"))
    return out


def _check_files(lib):
    from .. import rules, synonyms

    out = []
    cfg = lib.root / "config.json"
    for name, loader in (("config.json", lambda: json.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}),
                         ("rules.json", lambda: rules.load(lib.root)), ("synonyms.json", lambda: synonyms.load(lib.root))):
        try:
            loader()
            out.append((name, OK, "valid" if (lib.root / name).exists() else "not present (defaults used)", ""))
        except (ValueError, OSError) as e:
            out.append((name, FAIL, str(e)[:120], f"fix or delete {lib.root / name}"))
    if cfg.exists():
        try:
            unknown = sorted(set(json.loads(cfg.read_text(encoding="utf-8"))) - set(BUILTIN))
            if unknown:
                out.append(("config keys", WARN, f"unknown keys ignored: {', '.join(unknown)}", f"known: {', '.join(BUILTIN)}"))
        except (ValueError, OSError):
            pass
    return out


def _check_model(lib):
    from .. import learn

    cache = lib.root / "model.json"
    filed = [b for b in lib.all(snippet=0) if not is_unfiled(b)]
    if len(filed) < 10:
        return [("filing model", INFO, f"{len(filed)} filed bookmarks: too few to learn from yet", "")]
    if not cache.exists():
        return [("filing model", INFO, "not trained yet (it trains on the first `organize` or `learn`)", "")]
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        fresh = data.get("fingerprint") == learn.fingerprint(list(filed))
        return [("filing model", OK if fresh else INFO, f"{data.get('mode')} model, {'current' if fresh else 'stale (your filing changed; it retrains next time)'}", "")]
    except (ValueError, OSError):
        return [("filing model", WARN, "model.json is unreadable", "`learn --retrain`")]


def _check_speed():
    from .. import learn

    workers = learn.worker_count(10**6, 5)
    state = "numpy is installed" if learn.numpy_active() else "numpy is not installed (training is slower; `pip install numpy` or `pip install .[fast]`)"
    return [("training speed", INFO, f"{state}; up to {workers} worker process(es) for big libraries", "")]


def _check_feedback(lib):
    rows = lib.feedback_rows()
    if not rows:
        return [("suggestion feedback", INFO, "no decisions recorded yet (they are collected when you review plans, triage, or save from the extension)", "")]
    explicit = sum(1 for r in rows if r["explicit"])
    ok = sum(1 for r in rows if r["action"] == "accepted")
    return [("suggestion feedback", OK, f"{len(rows)} decisions recorded, {ok} accepted, {explicit} looked at one by one", "")]


def _check_guard():
    ok = all(netsafe.fetch(u, timeout=2).blocked for u in ("http://127.0.0.1/", "http://169.254.169.254/", "file:///etc/passwd"))
    return (OK if ok else FAIL), "private/loopback/metadata addresses are refused by the link checker", "" if ok else "the SSRF guard is not working"


def _network_probes():
    out = []
    t = time.time()
    r = netsafe.fetch("https://example.com/", method="HEAD", timeout=8)
    out.append(("network: HTTPS", OK if 200 <= r.status < 400 else FAIL, f"example.com answered {r.status or r.error[:60]} in {time.time() - t:.1f}s",
                "" if r.status else "no outbound HTTPS: link checking and archiving will not work"))
    if not r.status:
        return out + [("network: other probes", INFO, "skipped: nothing answered, so they would prove nothing", "")]
    r = netsafe.fetch("http://example.com/", method="HEAD", timeout=8)
    out.append(("network: redirects", OK if r.status in (200, 301, 308) else WARN, f"http://example.com -> {r.final_url} ({r.status})", ""))
    r = netsafe.fetch("https://example.com/this-page-should-not-exist-mhbm", method="HEAD", timeout=8)
    verdict = linkcheck.classify(r.status, r.error, r.blocked, False)
    out.append(("network: dead-link detection", OK if verdict[0] == "fail" else WARN, f"a missing page was classified {verdict[0]}/{verdict[1]} (HTTP {r.status})",
                "" if verdict[0] == "fail" else "some networks answer 200 to everything (captive portal?)"))
    wb = linkcheck.wayback_lookup("http://example.com/", timeout=10)
    out.append(("network: Wayback Machine", OK if wb else WARN, "reachable" if wb else "no answer from archive.org (optional feature)", ""))
    return out


def cmd_doctor(args, S):
    lib, o = _lib(args), Out(args)
    checks = [("python", *_check_python()), ("sqlite", *_check_sqlite()), ("proxy", *_check_proxy()), ("dns", *_check_dns()),
              ("link-check guard", *_check_guard())]
    checks += _check_tools() + _check_home(lib) + _check_index(lib, args.fix) + _check_files(lib) + _check_model(lib) + _check_speed() + _check_feedback(lib)
    found = browsers.detect()
    checks.append(("browsers", INFO, ", ".join(f"{n} ({len(p)})" for n, p in found.items()) or "none detected on this machine", ""))
    if args.network:
        checks += _network_probes()
    if args.fix:
        lib.vacuum()
    counts = {s: sum(1 for c in checks if c[1] == s) for s in (OK, WARN, FAIL, INFO)}
    o.set(version=__version__, checks=[{"name": n, "status": s, "detail": d, "hint": h} for n, s, d, h in checks], summary=counts)
    mark = {OK: "ok  ", WARN: "WARN", FAIL: "FAIL", INFO: "info"}
    for name, status, detail, hint in checks:
        o.say(f"[{mark[status]}] {name:<30} {detail}" + (f"\n         -> {hint}" if hint and status in (WARN, FAIL) else ""))
    o.say("\n" + tr("doctor_summary", ok=counts[OK], warn=counts[WARN], fail=counts[FAIL]) + ("" if args.network else "   (add --network to test outbound access)"))
    o.finish()
    return 2 if counts[FAIL] else 0


# ----------------------------------------------------------------------------- wizard
STEPS_SAFE = ("clean-urls", "titles", "organize", "autotag")
STEPS_ALL = STEPS_SAFE + ("dedupe", "restructure")


def _ask(prompt: str, default: bool = False, *, scripted=None) -> str:
    """'y', 'n' or 'q'."""
    hint = "[Y/n/q]" if default else "[y/N/q]"
    while True:
        ans = (scripted() if scripted else input(f"{prompt} {hint} ")).strip().lower()
        if ans in ("y", "yes"):
            return "y"
        if ans in ("n", "no"):
            return "n"
        if ans in ("q", "quit"):
            return "q"
        if ans == "":
            return "y" if default else "n"


def cmd_wizard(args, S):
    from .. import cli

    home = ["--home", args.home] if args.home else []
    base = home + (["--quiet"] if args.quiet else [])
    auto = args.auto
    if auto == "none" and not sys.stdin.isatty() and not os.environ.get("MHBM_WIZARD_SCRIPTED"):
        sys.exit("the wizard asks questions, so it needs a terminal; use --auto safe (or --auto all) to run unattended")

    def run(*argv):
        print(f"\n$ mhbm {' '.join(argv)}")
        return cli.main(base + list(argv))

    def step(name, argv, *, destructive=False, preview=True):
        print(f"\n=== {name} ===")
        if preview and run(*argv) != 0:
            return "n"
        if auto == "all" or (auto == "safe" and not destructive):
            choice = "y"
        elif auto in ("safe", "all"):
            print(f"(skipped: {name} changes or removes data; use --auto all to include it)")
            choice = "n"
        else:
            choice = _ask(tr("wizard_apply", name=name), default=not destructive)
        if choice == "y":
            run(*argv, "--apply")
        return choice

    if args.file:
        run("import", args.file)
    elif args.browser:
        run("import-browser", args.browser)
    lib = _lib(args)
    if lib.count() == 0:
        sys.exit("the library is empty: give an export file (`wizard bookmarks.html`) or --browser chrome")
    run("stats")
    plan = [("clean tracking parameters", ["clean-urls"], False), ("fix titles", ["titles"], False),
            ("merge duplicates", ["dedupe", "--level", "variant"], True), ("file unfiled bookmarks", ["organize"], False),
            ("add keyword tags", ["autotag"], False), ("tidy the folder tree", ["restructure"], True)]
    for name, argv, destructive in plan:
        if step(name, argv, destructive=destructive) == "q":
            print(i18n.bidi(tr("wizard_stopped")))
            return 0
    if args.check_links:
        print(f"\n=== check {args.check_links} links ===")
        run("check", "--limit", str(args.check_links), "--wayback")
    out = args.out or str(Path(lib.root) / "organized.html")
    run("export", "--format", "html", "--out", out)
    run("dashboard")
    print(f"\nDone. Import {out} in your browser (or `install-chrome`), then open {Path(lib.root) / 'dashboard.html'}."
          "\nAnything unsure was left alone: `ai-batch` lists it with the model's best guesses. Undo any step with `mhbm undo`.")
    return 0


def register(add, apply_flag, net_flags):
    sp = add("doctor", cmd_doctor, "diagnose the installation and the library; --fix repairs the safe things")
    sp.add_argument("--fix", action="store_true", help="rebuild the search index, drop orphaned rows, vacuum")
    sp.add_argument("--network", action="store_true", help="also test outbound access (a few requests to example.com / archive.org)")

    sp = add("wizard", cmd_wizard, "guided clean-up: import, preview each step, apply the ones you approve")
    sp.add_argument("file", nargs="?", help="bookmark export to import first")
    sp.add_argument("--browser", help="import straight from an installed browser instead")
    sp.add_argument("--auto", choices=["none", "safe", "all"], default="none",
                    help="none: ask for every step; safe: apply the non-destructive steps; all: also merge duplicates and tidy folders")
    sp.add_argument("--check-links", type=int, default=0, metavar="N", help="also check the first N links")
    sp.add_argument("--out", help="where to write the organized HTML export")
