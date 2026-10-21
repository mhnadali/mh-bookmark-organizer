#!/usr/bin/env python3
"""The release gate that does not need a build: does the tag, the code and the documentation say the same version?

    python tools/release_check.py v1.6.0 [--notes release-notes.md]

Checks that the tag, ``mhbm.__version__``, the extension manifest, the two plugin manifests (Claude Code and Codex), the README ("Version X") and the CHANGELOG
(a ``## X`` heading) agree, and writes that CHANGELOG section as the release notes. Under GitHub Actions it also sets the step outputs
``version`` and ``prerelease``. Standard library only; reads files, never imports the package.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAG = re.compile(r"^v(\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?)$")     # a release (1.6.0) or a pre-release (1.6.0rc1); .post/.dev versions are not released from here
BOUNDARY = r"(?![\w+-]|\.\d)"                                        # "1.5.2" must not match inside 1.5.20, 1.5.2.1 or 1.5.2-rc (a full stop after it is fine)


def declared_version(root: Path = ROOT) -> str:
    m = re.search(r'(?m)^__version__\s*=\s*"([^"]+)"\s*$', (root / "scripts" / "mhbm" / "__init__.py").read_text(encoding="utf-8"))
    if not m:
        raise SystemExit("cannot find __version__ in scripts/mhbm/__init__.py")
    return m.group(1)


def changelog_section(version: str, root: Path = ROOT) -> str | None:
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"(?ms)^## {re.escape(version)}{BOUNDARY}[^\n]*\n(.*?)(?=^## |\Z)", text)
    return m.group(1).strip() if m else None


def problems(tag: str, root: Path = ROOT) -> tuple:
    """(version, [problem, ...]) for ``tag`` against the files under ``root``."""
    found: list = []
    m = TAG.match(tag)
    if not m:
        return "", [f"tag {tag!r} is not v<major>.<minor>.<patch>[a|b|rc<N>]"]
    version = m.group(1)
    code = declared_version(root)
    if code != version:
        found.append(f"scripts/mhbm/__init__.py says {code}, the tag says {version}")
    for label, path, key in (("extension/manifest.json", root / "extension" / "manifest.json", "version"),
                             (".claude-plugin/plugin.json", root / ".claude-plugin" / "plugin.json", "version"),
                             (".codex-plugin/plugin.json", root / ".codex-plugin" / "plugin.json", "version")):
        if not path.exists():
            found.append(f"{label} is missing")
        elif json.loads(path.read_text(encoding="utf-8")).get(key) != version:
            found.append(f"{label} says {json.loads(path.read_text(encoding='utf-8')).get(key)}, the tag says {version}")
    if not re.search(rf"Version {re.escape(version)}{BOUNDARY}", (root / "README.md").read_text(encoding="utf-8")):
        found.append(f"README.md does not contain 'Version {version}'")
    if changelog_section(version, root) is None:
        found.append(f"CHANGELOG.md has no '## {version}' section")
    return version, found


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("tag")
    ap.add_argument("--notes", help="write the CHANGELOG section for this version here")
    ap.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    root = Path(args.root)
    version, found = problems(args.tag, root)
    for line in found:
        print(f"::error::{line}" if os.environ.get("GITHUB_ACTIONS") else f"error: {line}", file=sys.stderr)
    if found:
        return 1
    if args.notes:
        Path(args.notes).write_text((changelog_section(version, root) or "") + "\n", encoding="utf-8")
    prerelease = "true" if re.search(r"(a|b|rc)\d+$", version) else "false"
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"version={version}\nprerelease={prerelease}\n")
    print(f"ok: {args.tag} agrees with the code, the manifests, the README and the CHANGELOG (prerelease={prerelease})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
