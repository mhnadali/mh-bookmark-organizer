"""The OpenAI Codex plugin: the files agree with each other and with the code, the launcher is safe on its own, and (when the ``codex``
command is installed) the real Codex loads the plugin, starts the server and can call a tool through it.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import common  # noqa: F401
import mhbm
from mhbm.mcp import codex as launcher
from mhbm.mcp.registry import BY_NAME

SKILL = Path(__file__).resolve().parents[1]
REPO = SKILL.parents[2]
MARKETPLACE = REPO / ".claude-plugin" / "marketplace.json"
PLUGIN = SKILL / ".codex-plugin" / "plugin.json"
CODEX_SKILL = SKILL / "codex" / "skills" / "mh-bookmark-organizer"


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class Structure(unittest.TestCase):
    def setUp(self):
        self.plugin = load(PLUGIN)
        self.mcp = load(SKILL / self.plugin["mcpServers"])["mcpServers"]

    def test_manifest_matches_the_release_and_the_claude_plugin(self):
        claude = load(SKILL / ".claude-plugin" / "plugin.json")
        self.assertEqual(self.plugin["version"], mhbm.__version__)
        self.assertEqual(self.plugin["name"], claude["name"])
        self.assertEqual(self.plugin["repository"], claude["repository"])
        self.assertRegex(self.plugin["name"], r"^[a-z0-9.-]+$")

    def test_license_is_not_invented(self):
        if not any((REPO / n).exists() for n in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING")):
            self.assertNotIn("license", self.plugin)

    def test_paths_are_plugin_relative_and_exist(self):
        for key in ("skills", "mcpServers"):
            value = self.plugin[key]
            self.assertTrue(value.startswith("./") and value != "./" and ".." not in value, value)
            self.assertTrue((SKILL / value).exists(), value)

    def test_interface_has_what_the_codex_ui_shows(self):
        ui = self.plugin["interface"]
        for key in ("displayName", "shortDescription", "longDescription", "developerName", "category", "defaultPrompt"):
            self.assertTrue(ui[key], key)
        for prompt in ui["defaultPrompt"]:
            self.assertIn("$" + self.plugin["name"], prompt)
            self.assertLessEqual(len(prompt), 128)
        self.assertLessEqual(len(ui["defaultPrompt"]), 3)

    def test_the_codex_skill_is_a_real_skill_that_names_only_real_tools(self):
        text = (CODEX_SKILL / "SKILL.md").read_text(encoding="utf-8")
        front = text.split("---")[1]
        self.assertRegex(front, rf"(?m)^name:\s*{re.escape(self.plugin['name'])}\s*$")
        self.assertRegex(front, r"(?m)^description:\s*\S")
        for name in set(re.findall(r"\bbm_[a-z_]+\b", text)):
            self.assertIn(name, BY_NAME, name)
        self.assertNotIn("CLAUDE_", text)
        self.assertNotIn("${", text)
        self.assertIn("allow_implicit_invocation", (CODEX_SKILL / "agents" / "openai.yaml").read_text(encoding="utf-8"))

    def test_the_claude_plugin_does_not_pick_up_the_codex_skill(self):
        """Claude Code loads the root SKILL.md only; a ``skills/`` folder would make it a second skill."""
        self.assertFalse((SKILL / "skills").exists())
        self.assertNotIn("skills", load(SKILL / ".claude-plugin" / "plugin.json"))

    def test_server_command_is_what_codex_accepts_and_starts_this_checkout(self):
        server = self.mcp["bookmarks"]
        self.assertEqual(server["command"], "python3")                # Codex takes a bare executable name or a ./ path, no placeholders
        self.assertEqual(server["args"], ["codex_mcp.py"])             # a script, not -m: the working folder never comes first on the module path
        self.assertEqual(server["cwd"], "./scripts")                  # relative to the plugin: Codex has no ${PLUGIN_ROOT} for args or env
        self.assertTrue((SKILL / "scripts" / server["args"][0]).is_file())
        self.assertNotIn("env", server)
        self.assertNotIn("${", json.dumps(self.mcp))

    def test_marketplace_entry_is_shared_with_claude_code(self):
        if not MARKETPLACE.exists():
            self.skipTest("not running from a checkout of the repository")
        entry = load(MARKETPLACE)["plugins"][0]
        self.assertEqual((REPO / entry["source"]).resolve(), SKILL.resolve())
        self.assertEqual(entry["name"], self.plugin["name"])


class Launcher(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="mhbm-cx-home-")
        self.addCleanup(shutil.rmtree, self.home, True)

    def test_defaults_without_any_working_folder(self):
        library, allowed = launcher.defaults({}, self.home)
        self.assertEqual(library, os.path.join(self.home, ".mh-bookmark-organizer"))
        self.assertEqual(allowed, [])

    def test_defaults_use_the_folders_that_exist_and_never_the_library(self):
        for name in ("Downloads", "Desktop"):
            os.mkdir(os.path.join(self.home, name))
        library, allowed = launcher.defaults({}, self.home)
        self.assertEqual([os.path.basename(p) for p in allowed], ["Downloads", "Desktop"])
        self.assertFalse(any(library.startswith(p) or p.startswith(library) for p in allowed))

    def test_environment_wins(self):
        os.mkdir(os.path.join(self.home, "Downloads"))
        lib, allowed = launcher.defaults({"MHBM_HOME": "/x/lib", "MHBM_MCP_ALLOW_DIRS": os.pathsep.join(["/a", "/b"])}, self.home)
        self.assertEqual((lib, allowed), ("/x/lib", ["/a", "/b"]))

    def test_import_path_is_pinned_to_the_engine(self):
        saved = list(sys.path)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), saved))
        code = launcher.code_dir()
        self.assertTrue((Path(code) / "mhbm" / "__init__.py").is_file())
        sys.path[:] = ["", os.getcwd(), "/somewhere", code]
        launcher.pin_import_path(code)
        self.assertEqual(sys.path, [code, "/somewhere"])


class Process(unittest.TestCase):
    """Start the server exactly as the manifest says (``python3 codex_mcp.py`` with the scripts folder as cwd)."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="mhbm-cx-proc-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        (self.home / "Downloads").mkdir()
        shutil.copy(SKILL / "tests" / "fixtures" / "messy-library.html", self.home / "Downloads" / "b.html")
        (self.home / "Downloads" / "mhbm").mkdir()                    # a folder the server works in must not be able to replace the engine
        (self.home / "Downloads" / "mhbm" / "__init__.py").write_text("raise SystemExit('shadowed')\n")
        self.marker = self.home / "ran"

    def session(self, calls):
        env = {k: v for k, v in os.environ.items() if not k.startswith("MHBM_") and k != "PYTHONPATH"}
        env.update(HOME=str(self.home), USERPROFILE=str(self.home), MHBM_LANG="en", PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.Popen([sys.executable, "codex_mcp.py"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                cwd=SKILL / "scripts", env=env)
        out = {}
        try:
            requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
                        {"jsonrpc": "2.0", "method": "notifications/initialized"}]
            requests += [{"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": n, "arguments": a}} for i, (n, a) in enumerate(calls, 2)]
            for request in requests:                                  # one at a time: closing stdin early would cancel a call in flight
                proc.stdin.write(json.dumps(request) + "\n")
                proc.stdin.flush()
                if "id" in request:
                    out[request["id"]] = json.loads(proc.stdout.readline())
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
            err = proc.stderr.read()
            proc.stdout.close()
            proc.stderr.close()
        return err, out

    def test_import_from_downloads_works_and_other_folders_are_refused(self):
        err, out = self.session([("bm_import_file", {"path": "b.html"}), ("bm_import_file", {"path": "/etc/hostname"}), ("bm_stats", {})])
        self.assertEqual(out[1]["result"]["serverInfo"]["name"], "mh-bookmark-organizer", err)
        self.assertFalse(out[2]["result"]["isError"], out[2])
        self.assertGreater(out[2]["result"]["structuredContent"]["imported"], 100)
        self.assertTrue(out[3]["result"]["isError"])
        self.assertIn("outside the allowed directories", out[3]["result"]["content"][0]["text"])
        self.assertFalse(out[4]["result"]["isError"], out[4])
        self.assertTrue((self.home / ".mh-bookmark-organizer" / "bookmarks.db").exists() or any((self.home / ".mh-bookmark-organizer").iterdir()))
        self.assertNotIn("shadowed", err)

    def test_without_a_working_folder_nothing_outside_the_plugin_is_readable(self):
        shutil.rmtree(self.home / "Downloads")
        outside = self.home / "x.html"
        shutil.copy(SKILL / "tests" / "fixtures" / "messy-library.html", outside)
        err, out = self.session([("bm_import_file", {"path": str(outside)})])
        self.assertTrue(out[2]["result"]["isError"], err)
        self.assertIn("outside the allowed directories", out[2]["result"]["content"][0]["text"])


@unittest.skipUnless(MARKETPLACE.exists() and shutil.which("codex"), "needs the repository checkout and the codex command")
class RealCodex(unittest.TestCase):
    """The authority: Codex itself reads the marketplace, installs the plugin, starts the server and relays a tool call."""

    def test_codex_installs_the_plugin_and_calls_a_tool(self):
        base = Path(tempfile.mkdtemp(prefix="mhbm-codex-"))
        self.addCleanup(shutil.rmtree, base, True)
        (base / "home" / "Downloads").mkdir(parents=True)
        (base / "codex").mkdir()
        shutil.copy(SKILL / "tests" / "fixtures" / "messy-library.html", base / "home" / "Downloads" / "b.html")
        env = dict(os.environ, HOME=str(base / "home"), CODEX_HOME=str(base / "codex"))
        for argv in (["plugin", "marketplace", "add", str(REPO)], ["plugin", "add", "mh-bookmark-organizer@mhnadali-skills"]):
            p = subprocess.run(["codex", *argv], capture_output=True, text=True, env=env, timeout=120)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        server = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)

        def stop():
            server.terminate()
            server.wait(timeout=30)
            server.stdin.close()
            server.stdout.close()

        self.addCleanup(stop)
        counter = iter(range(1, 100))

        def call(method, params):
            ident = next(counter)
            server.stdin.write(json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}) + "\n")
            server.stdin.flush()
            deadline = time.time() + 90
            while time.time() < deadline:
                message = json.loads(server.stdout.readline())
                if message.get("id") == ident and "method" not in message:
                    return message
            self.fail(f"no answer to {method}")

        call("initialize", {"clientInfo": {"name": "mhbm-test", "version": "0"}})
        server.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "initialized"}) + "\n")
        server.stdin.flush()
        plugin = call("plugin/read", {"marketplacePath": str(MARKETPLACE), "pluginName": "mh-bookmark-organizer"})["result"]["plugin"]
        self.assertEqual(plugin["mcpServers"], ["bookmarks"])
        self.assertEqual([s["name"] for s in plugin["skills"]], ["mh-bookmark-organizer:mh-bookmark-organizer"])
        status = call("mcpServerStatus/list", {})["result"]["data"][0]
        self.assertIsNone(status["toolsError"])
        self.assertEqual(set(status["tools"]), set(BY_NAME))
        thread = call("thread/start", {})["result"]["thread"]["id"]
        reply = call("mcpServer/tool/call", {"server": "bookmarks", "tool": "bm_import_file", "arguments": {"path": "b.html"}, "threadId": thread})["result"]
        self.assertFalse(reply["isError"], reply)
        self.assertGreater(reply["structuredContent"]["imported"], 100)


if __name__ == "__main__":
    unittest.main()
