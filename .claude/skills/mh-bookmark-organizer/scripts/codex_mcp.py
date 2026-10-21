#!/usr/bin/env python3
"""Start the MCP server for OpenAI Codex: ``python3 /path/to/scripts/codex_mcp.py [options]`` (see mhbm/mcp/codex.py for the defaults).

Running a file puts only this folder first on the module path, never the working directory, so a project folder cannot supply its own ``mhbm``.
"""
import os
import sys

sys.path[0] = os.path.dirname(os.path.abspath(__file__))

from mhbm.mcp.codex import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
