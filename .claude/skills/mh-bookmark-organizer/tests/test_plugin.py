"""The Claude Code plugin and its marketplace entry: the files are consistent with each other and with the code they start.

``claude plugin validate --strict`` is the authority on the manifest format; it runs here when the ``claude`` command is installed
(otherwise those two tests are skipped and the checks below still hold the structure together).
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import common  # noqa: F401
import mhbm

SKILL = Path(__file__).resolve().parents[1]
REPO = SKILL.parents[2]
MARKETPLACE = REPO / ".claude-plugin" / "marketplace.json"
PLUGIN = SKILL / ".claude-plugin" / "plugin.json"


def load_tool(name):
    spec = importlib.util.spec_from_file_location(f"mhbm_tool_{name}", SKILL / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


@unittest.skipUnless(MARKETPLACE.exists(), "not running from a checkout of the repository")
class Structure(unittest.TestCase):
    def setUp(self):
        self.plugin = load(PLUGIN)
        self.market = load(MARKETPLACE)
        self.mcp = load(SKILL / ".mcp.json")["mcpServers"]

    def test_plugin_version_is_the_release_version(self):
        self.assertEqual(self.plugin["version"], mhbm.__version__)

    def test_the_skill_folder_is_the_plugin_root_and_loads_as_one_skill(self):
        frontmatter = (SKILL / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
        self.assertRegex(frontmatter, rf"(?m)^name:\s*{re.escape(self.plugin['name'])}\s*$")      # a root skill must name itself
        self.assertFalse((SKILL / "skills").exists(), "a skills/ folder would turn the root SKILL.md into a second skill")
        self.assertNotIn("skills", self.plugin)

    def test_marketplace_entry_points_at_the_plugin(self):
        self.assertEqual(len(self.market["plugins"]), 1)
        entry = self.market["plugins"][0]
        self.assertEqual(entry["name"], self.plugin["name"])
        self.assertRegex(entry["source"], r"^\./[^\\]+$")
        self.assertNotIn("..", entry["source"])
        self.assertEqual((REPO / entry["source"]).resolve(), SKILL.resolve())
        self.assertTrue(entry["defaultEnabled"])                 # the manifest says False so that the clone's own skills-dir copy stays off
        self.assertFalse(self.plugin["defaultEnabled"])
        self.assertNotIn(self.market["name"], {"inline", "builtin", "skills-dir", "synced"})
        self.assertNotIn("claude", self.market["name"].lower())
        self.assertNotEqual(self.market["name"], "mhnadali-mh-bookmark-organizer")      # Claude Code reserves <owner>-<repo> for the download folder

    def test_license_is_not_invented(self):
        """The owner picks the license; until a LICENSE file exists the manifest must not claim one."""
        if not any((REPO / n).exists() for n in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING")):
            self.assertNotIn("license", self.plugin)
            self.assertNotIn("license", self.market["plugins"][0])

    def test_mcp_server_starts_the_module_this_checkout_ships(self):
        server = self.mcp["bookmarks"]
        self.assertEqual(server["args"][0], "-c")                    # not -m: see test_a_project_folder_cannot_shadow_the_engine
        self.assertIn("run_module('mhbm.mcp'", server["args"][1])
        self.assertIn("sys.path.pop(0)", server["args"][1])
        self.assertTrue((SKILL / "scripts" / "mhbm" / "mcp" / "__main__.py").is_file())
        self.assertEqual(server["env"]["PYTHONPATH"], "${CLAUDE_PLUGIN_ROOT}/scripts")
        self.assertNotIn("--allow-dir", server["args"])         # the client's roots (the project folder) decide, not a path baked into the plugin

    def test_every_user_config_reference_is_declared_and_every_option_is_used(self):
        text = (SKILL / ".mcp.json").read_text(encoding="utf-8")
        used = set(re.findall(r"\$\{user_config\.(\w+)\}", text))
        self.assertEqual(used, set(self.plugin["userConfig"]))
        for key, spec in self.plugin["userConfig"].items():
            self.assertTrue(spec["title"] and spec["description"], key)
        self.assertTrue(self.plugin["userConfig"]["library_dir"]["required"])
        self.assertEqual(self.plugin["userConfig"]["library_dir"]["type"], "directory")

    def test_environment_settings_are_read_by_the_server(self):
        cli = (SKILL / "scripts" / "mhbm" / "mcp" / "cli.py").read_text(encoding="utf-8")
        for name in self.mcp["bookmarks"]["env"]:
            if name.startswith("MHBM_"):
                self.assertIn(name, cli, name)

    def test_the_plugin_copy_of_the_agent_is_made_from_the_project_agent(self):
        """Same text, except that the plugin copy points at the plugin folder and the configured library (tools/sync_agent.py makes it)."""
        sync = load_tool("sync_agent")
        project = (REPO / ".claude" / "agents" / "mh-bookmark-organizer.md").read_text(encoding="utf-8")
        copy = (SKILL / "agents" / "mh-bookmark-organizer.md").read_text(encoding="utf-8")
        self.assertEqual(copy, sync.plugin_agent(project), "run: python tools/sync_agent.py")
        self.assertIn("${user_config.library_dir}", copy)
        self.assertIn("--home", copy)

    def test_a_project_folder_cannot_shadow_the_engine(self):
        """Claude Code starts the server in the user's project folder. With ``python -m`` that folder would come first on the module path, and a
        project that has its own ``mhbm/`` package would run THAT code with the user's permissions. The shipped command line must not."""
        server = self.mcp["bookmarks"]
        project = Path(tempfile.mkdtemp(prefix="mhbm-shadow-"))
        self.addCleanup(shutil.rmtree, project, True)
        marker = project / "ran"
        (project / "mhbm").mkdir()
        (project / "mhbm" / "__init__.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
        library = project / "lib"
        env = dict(os.environ, PYTHONPATH=str(SKILL / "scripts"), MHBM_LANG="en", PYTHONDONTWRITEBYTECODE="1")
        argv = [sys.executable, server["args"][0], server["args"][1], "--home", str(library)]
        p = subprocess.run(argv, input='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}\n',
                           capture_output=True, text=True, cwd=project, env=env, timeout=60)
        self.assertFalse(marker.exists(), "the project's own mhbm package was imported")
        self.assertIn('"mh-bookmark-organizer"', p.stdout, p.stdout + p.stderr)

    def test_nothing_in_the_plugin_points_outside_it(self):
        for path in (SKILL / ".mcp.json", PLUGIN, SKILL / "agents" / "mh-bookmark-organizer.md"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("../", text, path.name)
            self.assertNotRegex(text, r"\.claude/skills/", path.name)      # the folder has another name once installed


@unittest.skipUnless(MARKETPLACE.exists() and shutil.which("claude"), "needs the repository checkout and the claude command")
class RealValidator(unittest.TestCase):
    def validate(self, target):
        home = tempfile.mkdtemp(prefix="mhbm-claude-")
        self.addCleanup(shutil.rmtree, home, True)
        env = dict(os.environ, HOME=home, USERPROFILE=home, CLAUDE_CONFIG_DIR=os.path.join(home, ".claude"))
        p = subprocess.run(["claude", "plugin", "validate", "--strict", str(target)], capture_output=True, text=True, env=env, timeout=180)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("Validation passed", p.stdout)
        # newer versions of the validator add an advisory block about the plugin *directory* (license, icon); it does not change the result
        text = re.sub(r"(?is)plugin directory checks for .*?(?=validation passed)", "", p.stdout)
        self.assertNotIn("warning", text.lower().replace("passed with warnings", "x"), p.stdout)

    def test_plugin_passes_strict_validation(self):
        self.validate(SKILL)

    def test_marketplace_passes_strict_validation(self):
        self.validate(REPO)


if __name__ == "__main__":
    unittest.main()
