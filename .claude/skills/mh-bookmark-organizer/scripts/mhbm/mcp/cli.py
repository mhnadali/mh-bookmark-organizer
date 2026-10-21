"""Command line of the MCP server, shared by ``mhbm mcp`` and the light-weight ``python -m mhbm.mcp`` / ``mhbm-mcp`` entry points."""
from __future__ import annotations

import argparse
import os


def add_arguments(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--read-only", action="store_true", help="expose only the tools that never write (the library is opened read-only)")
    sp.add_argument("--allow-dir", action="append", metavar="DIR",
                    help="directory import/export paths must stay inside (repeatable; default: the client's roots, else $CLAUDE_PROJECT_DIR, else the current directory)")
    sp.add_argument("--no-network", action="store_true", help="hide the tools that contact other hosts (check links, archive)")
    sp.add_argument("--disable-tool", action="append", metavar="NAME", help="hide one tool (repeatable)")
    sp.add_argument("--max-result-chars", type=int, default=30000, help="cap on one tool result (default 30000 characters)")
    sp.add_argument("--no-client-roots", action="store_true", help="ignore the directories the client announces (roots/list)")


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def run(home: str, args) -> None:
    """Start the server. The options also come from the environment (``MHBM_MCP_READ_ONLY``, ``MHBM_MCP_NO_NETWORK``, ``MHBM_MCP_ALLOW_DIRS`` with
    the platform's path separator, ``MHBM_MCP_DISABLE_TOOLS`` comma-separated), because a launcher such as a Claude Code plugin can pass values
    through ``env`` but not decide which flags to add. Read-only and no-network are on when the flag or the variable asks for them;
    ``--allow-dir`` replaces ``MHBM_MCP_ALLOW_DIRS``; disabled tools add up."""
    from .server import serve

    allow = list(args.allow_dir or []) or [d for d in os.environ.get("MHBM_MCP_ALLOW_DIRS", "").split(os.pathsep) if d]
    disabled = tuple(args.disable_tool or ()) + tuple(n.strip() for n in os.environ.get("MHBM_MCP_DISABLE_TOOLS", "").split(",") if n.strip())
    serve(home, read_only=args.read_only or env_flag("MHBM_MCP_READ_ONLY"), allow_dirs=allow or None,
          no_network=args.no_network or env_flag("MHBM_MCP_NO_NETWORK"), disabled=disabled,
          max_result_chars=args.max_result_chars, use_client_roots=not args.no_client_roots)


def main(argv=None) -> int:
    """``python -m mhbm.mcp [--home DIR] [options]``: does not import the rest of the CLI (about 0.2 s faster to start)."""
    p = argparse.ArgumentParser(prog="mhbm-mcp", description="mh-bookmark-organizer MCP server (stdio)")
    p.add_argument("--home", help="library directory (default $MHBM_HOME or ./.mhbm)")
    add_arguments(p)
    args = p.parse_args(argv)
    run(args.home or os.environ.get("MHBM_HOME") or ".mhbm", args)
    return 0
