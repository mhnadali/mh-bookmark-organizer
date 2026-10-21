"""Command line interface: ``python -m mhbm <command>`` (or ``mhbm`` once pip-installed).

The commands live in ``mhbm.commands.*``; this module only assembles the parser and dispatches.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from . import __version__, formats, store
from .commands import clean, data_io, diag, enrich, library, ops, organize, triage
from .commands.common import BUILTIN, Out, Settings, _home, _lib, apply_flag, close_all, net_flags  # noqa: F401

MODULES = (data_io, clean, organize, triage, enrich, library, diag, ops)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mhbm", description="mh-bookmark-organizer engine")
    p.add_argument("--home", help="library directory (default $MHBM_HOME or ./.mhbm)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--quiet", action="store_true", help="no progress on stderr")
    p.add_argument("--lang", help="message language (en, fa, ar, es, fr, de); default from MHBM_LANG / LANG")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    for module in MODULES:
        module.register(add, apply_flag, net_flags)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rc = args.fn(args, Settings(args, _home(args)))
        return rc if isinstance(rc, int) else 0
    except (formats.FormatError, FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except RecursionError:   # a file nested so deeply that even parsing it overflows the stack
        print("error: the file is nested too deeply to be a bookmark file; nothing was changed", file=sys.stderr)
        return 2
    except OSError as e:     # a full disk, a file the browser holds open, no permission: a message, not a traceback
        print(f"error: {e}", file=sys.stderr)
        return 2
    except sqlite3.OperationalError as e:
        if not store.is_busy(e):
            raise
        print("error: the library is busy: another mhbm process (a scheduled `maintain`, the web UI, the browser extension) is "
              "writing to it. Nothing was lost; try again in a moment, or raise MHBM_BUSY_TIMEOUT_MS.", file=sys.stderr)
        return 75


def entry() -> None:
    sys.exit(main())
