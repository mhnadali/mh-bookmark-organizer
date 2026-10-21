"""Start the MCP server the way OpenAI Codex needs it: ``python3 scripts/codex_mcp.py`` (the plugin runs it with ``cwd`` set to its ``scripts`` folder).

Codex starts a plugin's server in a folder it chooses and passes it only a small, fixed set of environment variables (``HOME`` and
``PATH`` among them; the user's own ``MHBM_*`` variables do not arrive), and it does not tell the server which folders the user works in.
So this launcher supplies what a Claude Code plugin gets from its settings, with defaults that are safe on their own:

* library: ``$MHBM_HOME`` if it reaches the server (``codex mcp add --env MHBM_HOME=...``), else ``~/.mh-bookmark-organizer``;
* files the tools may read or write: ``$MHBM_MCP_ALLOW_DIRS`` if set, else the user's Downloads, Documents and Desktop folders that exist
  (never the library itself). Relative file names are resolved in the first of them. If none exists, imports and exports are refused until
  the operator names a folder;
* ``--read-only``, ``--no-network`` and the other options of :mod:`mhbm.mcp.cli` still apply (``MHBM_MCP_READ_ONLY`` ...).
"""
from __future__ import annotations

import os
import sys

LIBRARY_DIR = ".mh-bookmark-organizer"
WORK_DIRS = ("Downloads", "Documents", "Desktop")


def code_dir() -> str:
    """The folder that holds the ``mhbm`` package (``scripts/`` in the plugin)."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def defaults(environ, home: str) -> tuple:
    """``(library, [allowed folders])`` for this environment and user home folder."""
    library = environ.get("MHBM_HOME") or os.path.join(home, LIBRARY_DIR)
    explicit = [d for d in environ.get("MHBM_MCP_ALLOW_DIRS", "").split(os.pathsep) if d]
    return library, explicit or [p for p in (os.path.join(home, n) for n in WORK_DIRS) if os.path.isdir(p)]


def pin_import_path(code: str) -> None:
    """Import ``mhbm`` only from ``code``: a folder the server later changes into (or a ``''`` entry) must never supply modules."""
    here = os.getcwd()
    sys.path[:] = [code] + [p for p in sys.path if p and os.path.abspath(p) not in (here, code)]


def main(argv=None) -> int:
    pin_import_path(code_dir())
    from .cli import main as serve_main

    library, allowed = defaults(os.environ, os.path.expanduser("~"))
    if allowed:
        os.environ["MHBM_MCP_ALLOW_DIRS"] = os.pathsep.join(allowed)
        os.chdir(allowed[0])
    return serve_main(["--home", library] + list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())
