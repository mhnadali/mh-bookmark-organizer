#!/bin/sh
# Rebuild tools.json, run every example against the real CLI, verify. (~1 minute; the two network examples make a few requests)
set -e
cd "$(dirname "$0")"
export PYTHONDONTWRITEBYTECODE=1 PYTHONWARNINGS=ignore
python3 build_tools.py
python3 fill_examples.py
python3 verify_tools.py
if command -v node >/dev/null 2>&1; then node ecma_patterns.js ../tools.json; fi
python3 render_tools_md.py
