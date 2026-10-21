#!/usr/bin/env python3
"""Make the plugin's copy of the subagent from the project's: ``python tools/sync_agent.py`` (writes agents/) or ``--check`` (exit 1 when stale).

The project agent (``.claude/agents/mh-bookmark-organizer.md``) names the skill by its repository path and runs the engine from there. Inside the
plugin that path does not exist, so the plugin copy points at ``${CLAUDE_PLUGIN_ROOT}`` and gets the library folder the user configured
(``${user_config.library_dir}``) - Claude Code fills both in when it loads the agent. Everything else is identical, and a test keeps it so.
"""
from __future__ import annotations

import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
CANONICAL = SKILL.parents[1] / "agents" / "mh-bookmark-organizer.md"
PLUGIN_COPY = SKILL / "agents" / "mh-bookmark-organizer.md"
PROJECT_SENTENCE = "Read `.claude/skills/mh-bookmark-organizer/SKILL.md` first"
PLUGIN_SENTENCE = "Read `${CLAUDE_PLUGIN_ROOT}/SKILL.md` first"
PLUGIN_SECTION = """
## Running as the Claude Code plugin

The engine lives in `${CLAUDE_PLUGIN_ROOT}` and the library is the folder the user chose when enabling the plugin: `${user_config.library_dir}`.
Run every command as

    PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/scripts" "${user_config.python}" -m mhbm --home "${user_config.library_dir}" <command>

and always pass `--home`: never rely on the default `./.mhbm`, which would be a different (empty) library inside the user's project. The `bm_*`
MCP tools belong to the main conversation; this agent works through the command line. The same safety rules apply to both.
"""


def plugin_agent(canonical: str) -> str:
    if PROJECT_SENTENCE not in canonical:
        raise SystemExit(f"the project agent no longer contains {PROJECT_SENTENCE!r}; update tools/sync_agent.py")
    return canonical.replace(PROJECT_SENTENCE, PLUGIN_SENTENCE, 1).rstrip("\n") + "\n" + PLUGIN_SECTION


def main(argv=None) -> int:
    expected = plugin_agent(CANONICAL.read_text(encoding="utf-8"))
    if "--check" in (argv if argv is not None else sys.argv[1:]):
        if PLUGIN_COPY.read_text(encoding="utf-8") != expected:
            print("agents/mh-bookmark-organizer.md is stale: run python tools/sync_agent.py", file=sys.stderr)
            return 1
        return 0
    PLUGIN_COPY.write_text(expected, encoding="utf-8")
    print(f"wrote {PLUGIN_COPY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
