"""Command group: ops."""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import time
from pathlib import Path

from .. import archive as archive_mod
from .. import categorize, feedback, linkcheck, report
from .. import dashboard as dashboard_mod
from .. import dedupe as dedupe_mod
from .common import BUILTIN, Out, _batches, _home, _lib, exclusions_for, model_for, redact, rules_for


def cmd_maintain(args, S):
    """Safe, non-destructive upkeep: re-check links, archive new pages, save proposals + a dashboard."""
    lib, o = _lib(args), Out(args)
    stamp = time.strftime("%Y%m%d")
    reports = lib.root / "reports"
    reports.mkdir(exist_ok=True)
    bms = lib.all()
    now = time.time()
    due = [b for b in bms if not b.last_checked or now - b.last_checked > args.recheck_days * 86400 or b.status == "suspect"]
    offline = False
    for chunk in _batches(due[: args.check_limit], 500):
        results = list(linkcheck.check_all(chunk, workers=S("workers"), timeout=S("timeout"), per_host=S("per_host"),
                                           interval=S("interval"), confirmations=S("confirmations"),
                                           min_gap=int(S("min_gap_hours") * 3600), wayback=True))
        if linkcheck.looks_offline(results):
            offline = True
            break
        lib.upsert(chunk)
    fresh = [b for b in lib.all(snippet=0) if not b.archive_path][: args.archive_limit]
    for chunk in _batches(fresh, 100):
        for _ in archive_mod.archive_all(chunk, lib.root, workers=S("archive_workers"), timeout=S("archive_timeout"),
                                         per_host=S("per_host"), interval=S("interval")):
            pass
        lib.upsert(chunk)
        lib.save_pages(chunk)
    bms = lib.all()
    plan = categorize.propose(bms, categorize.load_taxonomy(S("taxonomy")), model_for(lib, args, bms), min_conf=S("min_confidence"), rules=rules_for(lib),
                              exclude=exclusions_for(lib, bms))
    feedback.issue(lib, plan, bms)
    (reports / f"organize-plan-{stamp}.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    _, removed, _groups = dedupe_mod.dedupe(bms, "variant")
    r = report.build(bms, lib.folders())
    (lib.root / "dashboard.html").write_text(dashboard_mod.render(bms, lib.folders(), time.strftime("%Y-%m-%d %H:%M")), encoding="utf-8")
    summary = (f"# Bookmark maintenance {stamp}\n\n"
               + ("- WARNING: no link answered, so the network looks down or blocked; nothing was recorded\n" if offline else "")
               + f"- checked {len(due[:args.check_limit])} links; status {r['status']}\n"
               f"- archived {len(fresh)} new pages\n- {len(plan)} bookmarks have a filing proposal (`apply-plan reports/organize-plan-{stamp}.json`)\n"
               f"- {len(removed)} duplicates waiting for `dedupe --apply`\n- dead links: {r['status'].get('dead', 0)}; suspect: {r['status'].get('suspect', 0)}\n")
    (reports / f"{stamp}.md").write_text(summary, encoding="utf-8")
    o.say(summary)
    facts = {"checked": len(due[:args.check_limit]), "archived": len(fresh), "proposals": len(plan), "duplicates": len(removed),
             "dead": r["status"].get("dead", 0), "suspect": r["status"].get("suspect", 0), "offline": offline,
             "report": str(reports / f"{stamp}.md")}
    o.set(report=str(reports / f"{stamp}.md"), proposals=len(plan), duplicates=len(removed), status=r["status"])
    if args.notify:
        from .. import notify

        for name, ok, detail in notify.send(facts, S.cfg.get("notify", {})):
            o.say(f"notify {name}: {'sent' if ok else 'FAILED'} ({detail})")
    o.finish()


def cmd_notify(args, S):
    """Send a test notification through the configured providers."""
    from .. import notify

    o = Out(args)
    cfg = S.cfg.get("notify", {})
    if not cfg.get("on"):
        sys.exit('nothing configured: add {"notify": {"on": ["desktop"]}} to <home>/config.json (see `mhbm doctor`)')
    results = notify.send({"checked": 0, "proposals": 3, "dead": 1, "report": "(test message)"}, cfg, force=True)
    o.set(results=[{"provider": n, "ok": ok, "detail": d} for n, ok, d in results])
    for n, ok, d in results:
        o.say(f"{n}: {'sent' if ok else 'FAILED'} ({d})")
    o.finish()
    return 0 if all(ok for _, ok, _ in results) else 2


def cmd_schedule(args, S):
    o = Out(args)
    py = sys.executable
    home = Path(_home(args)).resolve()
    base = f'"{py}" -m mhbm --home "{home}" maintain'
    env = f'PYTHONPATH="{Path(__file__).resolve().parents[1]}"'
    o.say("Weekly maintenance (non-destructive: re-checks links, archives new pages, saves proposals + dashboard).\nAdd --notify to the command to be told what it found (see the \"notify\" setting).\n")
    o.say(f"cron (Linux/macOS):\n  0 3 * * 0 {env} {base}\n")
    o.say(f'Windows Task Scheduler:\n  schtasks /Create /SC WEEKLY /D SUN /ST 03:00 /TN mhbm-maintain /TR "{base}"\n')
    o.say("macOS launchd: create ~/Library/LaunchAgents/com.mhbm.maintain.plist running the same command with StartCalendarInterval Weekday=0 Hour=3")
    o.set(command=base)
    o.finish()


def completion_spec(parser) -> dict:
    """Subcommands, their options and positionals - the single source for every shell's completion script."""
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    helps = {ca.dest: ca.help for ca in sub._choices_actions}

    def describe(action):
        takes = action.nargs != 0 and not isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction, argparse._VersionAction, argparse._HelpAction))
        name = (action.metavar if isinstance(action.metavar, str) else action.dest or "").lower()
        files = bool(set(re.split(r"[_\-]", name)) & {"file", "files", "out", "plan", "profile", "path", "dir", "taxonomy", "restore"})
        return {"flags": list(action.option_strings), "help": (action.help or "").replace("%(default)s", ""), "takes": takes,
                "choices": [str(c) for c in (action.choices or [])], "files": files, "dest": action.dest}

    glob = [describe(a) for a in parser._actions if a.option_strings and not isinstance(a, argparse._HelpAction)]
    cmds = {}
    for name, sp in sub.choices.items():
        opts = [describe(a) for a in sp._actions if a.option_strings and not isinstance(a, argparse._HelpAction)]
        pos = [describe(a) for a in sp._actions if not a.option_strings]
        cmds[name] = {"help": helps.get(name, ""), "options": opts, "positionals": pos}
    return {"global": glob, "commands": cmds}


def _sq(s: str) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


def render_zsh(spec: dict) -> str:
    def arg(o):
        flags = o["flags"]
        desc = o["help"].replace("[", "(").replace("]", ")").replace("\n", " ")
        action = ""
        if o["takes"]:
            action = f":{o['dest']}:" + (f"({' '.join(o['choices'])})" if o["choices"] else "_files" if o["files"] else " ")
        out = []
        for f in flags:
            out.append(_sq(f"{f}[{desc}]{action}"))
        return out

    lines = ["#compdef mhbm", "# mh-bookmark-organizer completion for zsh: save as _mhbm in a directory on your $fpath", "",
             "_mhbm() {", "  local curcontext=\"$curcontext\" state line", "  typeset -A opt_args", "  local -a _cmds", "  _cmds=("]
    for name, c in spec["commands"].items():
        lines.append("    " + _sq(f"{name}:{c['help'].replace(':', ' -')}"))
    lines += ["  )", "  _arguments -C \\"]
    for o in spec["global"]:
        for a in arg(o):
            lines.append(f"    {a} \\")
    lines += ["    '1: :->cmd' \\", "    '*:: :->args'", "  case $state in", "    cmd) _describe -t commands 'mhbm command' _cmds ;;", "    args)", "      case $words[1] in"]
    for name, c in spec["commands"].items():
        specs = [a for o in c["options"] for a in arg(o)]
        for p in c["positionals"]:
            specs.append(_sq(f"*:{p['dest']}:" + (f"({' '.join(p['choices'])})" if p["choices"] else "_files" if p["files"] else " ")) if p["choices"] or True else "")
        body = " ".join(specs) if specs else ""
        lines.append(f"        {name}) _arguments {body} ;;".rstrip() if body else f"        {name}) ;;")
    lines += ["      esac ;;", "  esac", "}", "", "compdef _mhbm mhbm", ""]
    return "\n".join(lines)


def render_fish(spec: dict) -> str:
    def q(s):
        return _sq(s)

    lines = ["# mh-bookmark-organizer completion for fish: save as ~/.config/fish/completions/mhbm.fish", "complete -c mhbm -f"]
    for o in spec["global"]:
        flags = []
        for f in o["flags"]:
            flags.append(f"-l {f[2:]}" if f.startswith("--") else f"-s {f[1:]}")
        lines.append(f"complete -c mhbm -n '__fish_use_subcommand' {' '.join(flags)} -d {q(o['help'])}" + (" -r" if o["takes"] else ""))
    for name, c in spec["commands"].items():
        lines.append(f"complete -c mhbm -n '__fish_use_subcommand' -a {name} -d {q(c['help'])}")
        for o in c["options"]:
            flag_opts = " ".join(f"-l {f[2:]}" if f.startswith("--") else f"-s {f[1:]}" for f in o["flags"])
            extra = ""
            if o["takes"]:
                extra = f" -x -a {q(' '.join(o['choices']))}" if o["choices"] else (" -r" if o["files"] else " -x")
            lines.append(f"complete -c mhbm -n '__fish_seen_subcommand_from {name}' {flag_opts} -d {q(o['help'])}{extra}")
        for p in c["positionals"]:
            if p["choices"]:
                lines.append(f"complete -c mhbm -n '__fish_seen_subcommand_from {name}' -a {q(' '.join(p['choices']))}")
    return "\n".join(lines) + "\n"


def cmd_completion(args, S):
    from ..cli import build_parser

    spec = completion_spec(build_parser())
    if args.shell == "fish":
        print(render_fish(spec), end="")
        return
    if args.shell == "zsh":
        print(render_zsh(spec), end="")
        return
    words = {name: sorted({f for o in c["options"] for f in o["flags"]}) for name, c in spec["commands"].items()}
    cases = "\n".join(f'    {n}) opts="{" ".join(o)}" ;;' for n, o in words.items())
    script = f"""_mhbm() {{
  local cur="${{COMP_WORDS[COMP_CWORD]}}" cmd="" i opts
  for ((i=1;i<COMP_CWORD;i++)); do case "${{COMP_WORDS[i]}}" in -*) ;; {"|".join(words)}) cmd="${{COMP_WORDS[i]}}"; break;; esac; done
  if [ -z "$cmd" ]; then opts="{" ".join(words)} --home --json --quiet --lang --version"; else
    case "$cmd" in
{cases}
    esac
  fi
  COMPREPLY=( $(compgen -W "$opts" -- "$cur") )
}}
complete -F _mhbm mhbm
"""
    print(script, end="")


def cmd_mcp(args, S):
    from ..mcp.cli import run

    run(_home(args), args)


def cmd_ui(args, S):
    from .. import webui

    if args.host not in ("127.0.0.1", "localhost"):
        sys.exit("the UI edits your library, so it only listens on this computer (127.0.0.1)")
    webui.serve(_home(args), port=args.port, open_browser=not args.no_browser)


def cmd_native_host(args, S):
    from .. import nativehost

    nativehost.serve(_home(args))


def _native_dirs(browser: str):
    home = Path.home()
    system = platform.system()
    names = {"chrome": ("google-chrome", "Google/Chrome", "Google\\Chrome"), "chromium": ("chromium", "Chromium", "Chromium"),
             "brave": ("BraveSoftware/Brave-Browser", "BraveSoftware/Brave-Browser", "BraveSoftware\\Brave-Browser"),
             "edge": ("microsoft-edge", "Microsoft Edge", "Microsoft\\Edge"), "vivaldi": ("vivaldi", "Vivaldi", "Vivaldi")}
    lin, mac, win = names[browser]
    if system == "Linux":
        return home / ".config" / lin / "NativeMessagingHosts", None
    if system == "Darwin":
        return home / "Library/Application Support" / mac / "NativeMessagingHosts", None
    return Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")) / "mhbm", rf"Software\{win}\NativeMessagingHosts\com.mhbm.organizer"


def cmd_install_native_host(args, S):
    """Register the native messaging host so the extension can talk to this library."""
    from .. import nativehost

    o = Out(args)
    ext = args.extension_id.strip()
    if not re.fullmatch(r"[a-p]{32}", ext):
        sys.exit("the extension id is the 32-letter id shown on chrome://extensions (letters a-p only)")
    home = Path(_home(args)).resolve()
    directory, regkey = _native_dirs(args.browser)
    directory.mkdir(parents=True, exist_ok=True)
    scripts = Path(__file__).resolve().parents[2]  # .../scripts (the folder that contains the mhbm package)
    windows = platform.system() == "Windows"
    launcher = directory / ("mhbm-native-host.bat" if windows else "mhbm-native-host.sh")
    py = sys.executable
    if windows:
        launcher.write_text(f'@echo off\r\nset PYTHONPATH={scripts}\r\n"{py}" -m mhbm --home "{home}" native-host\r\n', encoding="utf-8")
    else:
        launcher.write_text(f'#!/bin/sh\nPYTHONPATH="{scripts}" exec "{py}" -m mhbm --home "{home}" native-host\n', encoding="utf-8")
        launcher.chmod(0o755)
    manifest = directory / f"{nativehost.HOST_NAME}.json"
    manifest.write_text(json.dumps({"name": nativehost.HOST_NAME, "description": "mh-bookmark-organizer library access",
                                    "path": str(launcher), "type": "stdio",
                                    "allowed_origins": [f"chrome-extension://{ext}/"]}, indent=2), encoding="utf-8")
    o.set(manifest=str(manifest), launcher=str(launcher))
    o.say(f"native host registered for extension {ext}\n  manifest: {manifest}\n  launcher: {launcher}")
    if windows:
        try:
            import winreg

            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, regkey) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, str(manifest))
            o.say(f"  registry: HKCU\\{regkey}")
        except OSError as e:
            o.say(f"  could not write the registry key ({e}); run: reg add HKCU\\{regkey} /ve /t REG_SZ /d \"{manifest}\" /f")
    o.say("Restart the browser (or reload the extension) afterwards.")
    o.finish()


def cmd_config(args, S):
    o = Out(args)
    o.set(config_file=str(Path(_home(args)) / "config.json"), effective=redact({k: S(k) for k in BUILTIN}))   # no secret is ever printed
    for k, v in o.data["effective"].items():
        o.say(f"{k:>22} = {v}")
    o.say("\nedit", o.data["config_file"], "to change defaults (JSON object with the keys above)")
    o.finish()


# -------------------------------------------------------------------- parser


def register(add, apply_flag, net_flags):
    sp = add("maintain", cmd_maintain, "non-destructive upkeep: recheck links, archive new pages, proposals + dashboard")
    sp.add_argument("--recheck-days", type=float, default=7)
    sp.add_argument("--check-limit", type=int, default=2000)
    sp.add_argument("--archive-limit", type=int, default=200)
    sp.add_argument("--notify", action="store_true", help="send the summary through the providers set up under \"notify\" in config.json")

    add("schedule", cmd_schedule, "print cron / Task Scheduler lines for weekly maintenance")

    add("completion", cmd_completion, "print a shell completion script").add_argument("shell", choices=["bash", "zsh", "fish"])

    sp = add("mcp", cmd_mcp, "run as an MCP server over stdio (for Claude and other MCP clients)")
    from ..mcp.cli import add_arguments

    add_arguments(sp)

    add("config", cmd_config, "show effective settings")

    add("native-host", cmd_native_host, "run the native messaging host for the browser extension (started by the browser)")
    sp = add("install-native-host", cmd_install_native_host, "register the native host for the browser extension")
    sp.add_argument("--extension-id", required=True, help="id of the loaded extension (chrome://extensions)")
    sp.add_argument("--browser", choices=["chrome", "chromium", "brave", "edge", "vivaldi"], default="chrome")

    sp = add("ui", cmd_ui, "open the local web UI (browse, edit, review plans, undo) on 127.0.0.1")
    sp.add_argument("--port", type=int, default=0)
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--no-browser", action="store_true", help="only print the URL")

    add("notify", cmd_notify, "send a test notification through the configured providers")
