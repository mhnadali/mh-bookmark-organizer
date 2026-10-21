"""Units under ``mhbm.mcp`` and the pieces of the library it relies on: the validator, the size cap, the read-only library, the entry points."""
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import common  # noqa: F401  (puts scripts/ on sys.path, sets MHBM_LANG)
import mcpdrive
from mcpdrive import Server, count, new_home, tree_state
from mhbm import mcp_server
from mhbm.mcp import content, executor, registry, results, validate
from mhbm.store import Library

try:
    import jsonschema
except ImportError:
    jsonschema = None


class Validator(unittest.TestCase):
    def test_nothing_is_coerced(self):
        schema = {"type": "object", "properties": {"n": {"type": "integer"}, "b": {"type": "boolean"}, "s": {"type": "string"}}, "additionalProperties": False}
        for bad in ({"n": "5"}, {"n": True}, {"n": 1.5}, {"b": "false"}, {"b": 0}, {"s": 5}, {"x": 1}):
            self.assertTrue(validate.validate(schema, bad), bad)
        for good in ({}, {"n": 5}, {"n": 5.0}, {"b": False}, {"s": ""}):
            self.assertEqual(validate.validate(schema, good), [], good)
        self.assertEqual(validate.normalize(schema, {"n": 5.0}), {"n": 5})

    def test_json_equality_is_not_python_equality(self):
        self.assertFalse(validate.equal(True, 1))
        self.assertFalse(validate.equal(0, False))
        self.assertTrue(validate.equal(1, 1.0))
        self.assertTrue(validate.equal([1, {"a": [2]}], [1.0, {"a": [2.0]}]))
        self.assertTrue(validate.validate({"enum": [1]}, True))
        self.assertTrue(validate.validate({"type": "array", "uniqueItems": True}, [1, True, 1.0]))

    def test_check_schema_rejects_what_it_would_not_enforce(self):
        for bad in ({"type": "strin"}, {"minLenght": 3}, {"pattern": "("}, {"$ref": "#/x"}, {"anyOf": [{"type": "string"}]}, 5):
            with self.assertRaises(validate.SchemaError, msg=bad):
                validate.check_schema(bad)
        validate.check_schema({"type": "object", "properties": {"a": {"anyOf": [{"type": "string"}, {"type": "null"}]}}})

    def test_deep_and_wide_inputs_are_bounded(self):
        deep = cur = {}
        for _ in range(200):
            cur["a"] = {}
            cur = cur["a"]
        schema = {"type": "object"}
        self.assertEqual(validate.validate(schema, deep), [])
        recursive = {"type": "array", "items": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}}
        errors = validate.validate(recursive, [[["x"]]] * 500)
        self.assertLessEqual(len(errors), validate.MAX_ERRORS)

    def test_every_registered_schema_passes_the_conventions(self):
        registry.selfcheck()
        self.assertEqual(len({t.name for t in registry.TOOLS}), len(registry.TOOLS))
        self.assertLessEqual(len(content.INSTRUCTIONS), 4096)       # Claude Code truncates server instructions beyond 4096 characters
        for t in registry.TOOLS:
            self.assertTrue(t.title and t.description, t.name)

    @unittest.skipUnless(jsonschema, "the reference validator is optional")
    def test_agrees_with_the_reference_implementation(self):  # noqa: C901 - a random schema generator
        rnd = random.Random(7)
        scalars = [None, True, False, 0, 1, -1, 2, 10, 100, 1.0, 2.0, 1.5, -0.5, "", "a", "abc", "-x", "ABC", "12", "é", "x" * 12]

        def value(depth=0):
            c = rnd.random()
            if depth >= 2 or c < 0.55:
                return rnd.choice(scalars)
            if c < 0.78:
                return [value(depth + 1) for _ in range(rnd.randint(0, 4))]
            return {rnd.choice("abc"): value(depth + 1) for _ in range(rnd.randint(0, 4))}

        def schema(depth=0):  # noqa: C901
            s = {}
            if rnd.random() < 0.8:
                s["type"] = rnd.choice(validate.TYPES)
            if rnd.random() < 0.15:
                s["enum"] = [value(1) for _ in range(rnd.randint(1, 3))]
            if rnd.random() < 0.25:
                s[rnd.choice(["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"])] = rnd.choice([-1, 0, 1, 2, 10, 1.5])
            if rnd.random() < 0.25:
                s[rnd.choice(["minLength", "maxLength"])] = rnd.randint(0, 5)
            if rnd.random() < 0.15:
                s["pattern"] = rnd.choice(["^[a-z]+$", "^[^-]", "^\\d+$", "a"])
            if rnd.random() < 0.25:
                s[rnd.choice(["minItems", "maxItems"])] = rnd.randint(0, 4)
            if rnd.random() < 0.1:
                s["uniqueItems"] = True
            if depth < 2:
                if rnd.random() < 0.3:
                    s["items"] = schema(depth + 1)
                if rnd.random() < 0.4:
                    s["properties"] = {n: schema(depth + 1) for n in rnd.sample("abc", rnd.randint(1, 3))}
                    if rnd.random() < 0.5:
                        s["required"] = rnd.sample(list(s["properties"]) + ["z"], 1)
                    s["additionalProperties"] = rnd.choice([False, True, schema(depth + 1)])
                for k in ("anyOf", "oneOf", "allOf"):
                    if rnd.random() < 0.06:
                        s[k] = [schema(depth + 1) for _ in range(rnd.randint(1, 3))]
                if rnd.random() < 0.04:
                    s["not"] = schema(depth + 1)
            return s

        for _ in range(1500):
            sch = schema()
            validate.check_schema(sch, root=False)
            ref = jsonschema.Draft202012Validator(sch)
            for _ in range(6):
                inst = value()
                self.assertEqual(ref.is_valid(inst), validate.is_valid(sch, inst), (sch, inst))


class Defaults(unittest.TestCase):
    def test_advertised_defaults_are_applied_before_the_command_is_built(self):
        """bm_check_links said 'default 50' and ran against EVERY bookmark when called without a limit."""
        for name, flag in (("bm_check_links", "--limit=50"), ("bm_archive", "--limit=25"), ("bm_search", "--limit=20")):
            tool = registry.BY_NAME[name]
            args = validate.apply_defaults(tool.input_schema, {"query": "x"} if name == "bm_search" else {})
            self.assertIn(flag, tool.build(args, None).argv, name)
        schema = registry.BY_NAME["bm_check_links"].input_schema
        self.assertEqual(validate.apply_defaults(schema, {"limit": 7})["limit"], 7)         # the caller's own value wins
        given = {}
        self.assertIsNot(validate.apply_defaults(registry.BY_NAME["bm_prune"].input_schema, given)["status"], registry.BY_NAME["bm_prune"].properties["status"]["default"])

    def test_text_that_cannot_be_passed_to_a_program_is_refused_by_the_schema(self):
        schema = registry.BY_NAME["bm_search"].input_schema
        for bad in ("a\u0000b", "a\ud800b"):
            self.assertTrue(validate.validate(schema, {"query": bad}), repr(bad))
        self.assertEqual(validate.validate(schema, {"query": "\u0634\u0637\u0631\u0646\u062c \U0001f600"}), [])


class Shrink(unittest.TestCase):
    def test_a_big_answer_stays_valid_json_of_the_same_shape(self):
        obj = {"results": [{"id": i, "title": "t" * 100, "snippet": "s" * 500} for i in range(200)], "total": 200}
        small, info = results.shrink(obj, 6000)
        self.assertLessEqual(len(results.compact(small)), 6000)
        self.assertEqual(set(small) - {"_truncated"}, {"results", "total"})
        self.assertEqual(small["total"], 200)
        self.assertTrue(0 < len(small["results"]) < 200)
        self.assertEqual(info["originalChars"], len(results.compact(obj)))
        same, none = results.shrink({"a": 1}, 6000)
        self.assertEqual((same, none), ({"a": 1}, None))

    def test_ok_result_carries_structured_content_only_for_objects(self):
        res = results.ok_result({"a": 1}, structured=True, limit=1000)
        self.assertEqual((res["structuredContent"], res["isError"]), ({"a": 1}, False))
        self.assertNotIn("structuredContent", results.ok_result({"a": 1}, structured=False, limit=1000))

    def test_stderr_is_reduced_to_the_useful_line_without_paths(self):
        stderr = "usage: mhbm tag [-h]\n  --id X\nTraceback (most recent call last):\n  File x\nmhbm: error: no bookmark with id zz in /srv/me/lib\n"
        self.assertEqual(results.clean_stderr(stderr, "/srv/me/lib"), "no bookmark with id zz in <library>")


class ReadOnlyLibrary(unittest.TestCase):
    def test_nothing_changes_on_disk_and_writes_fail(self):
        home = new_home()
        c = sqlite3.connect(home + "/library.db")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
        before = tree_state(home)
        with Library(home, read_only=True) as lib:
            self.assertGreater(lib.count(), 0)
            self.assertTrue(lib.search("chisel"))
            self.assertEqual(lib.journal_mode, "read-only")
            with self.assertRaises(sqlite3.OperationalError):
                lib.db.execute("DELETE FROM bookmarks")
        changed = {k for k in mcpdrive.diff_state(before, tree_state(home)) if not k.endswith(("-wal", "-shm"))}
        self.assertEqual(changed, set())

    def test_environment_switch_and_missing_library(self):
        home = new_home()
        os.environ["MHBM_READONLY"] = "1"
        try:
            with Library(home) as lib:
                self.assertTrue(lib.read_only)
            missing = tempfile.mkdtemp(dir=mcpdrive.scratch()) + "/nothing"
            with self.assertRaises(FileNotFoundError):
                Library(missing)
            self.assertFalse(os.path.exists(missing))              # not even the directory is created
        finally:
            del os.environ["MHBM_READONLY"]

    def test_an_old_layout_is_refused_not_migrated(self):
        home = new_home()
        c = sqlite3.connect(home + "/library.db")
        c.execute("PRAGMA user_version = 2")
        c.commit()
        c.close()
        with self.assertRaises(ValueError) as cm:
            Library(home, read_only=True)
        self.assertIn("upgrade", str(cm.exception))
        c = sqlite3.connect(home + "/library.db")
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 2)
        c.close()

    def test_model_cache_is_read_but_never_written(self):
        from mhbm.commands.common import model_for
        home = new_home()
        with Library(home, read_only=True) as lib:
            model_for(lib, type("A", (), {"quiet": True, "json": True})(), lib.all(snippet=0))
        self.assertFalse(Path(home, "model.json").exists())


class EntryPoints(unittest.TestCase):
    def handshake(self, command, env_extra=None, cwd=None):
        s = Server(new_home(), command=command, env_extra=env_extra, cwd=cwd)
        try:
            init = s.init("2025-06-18")
            self.assertEqual(init["result"]["serverInfo"]["name"], "mh-bookmark-organizer")
            tools = s.rpc("tools/list", id=2)["result"]["tools"]
            call = s.rpc("tools/call", {"name": "bm_stats", "arguments": {}}, id=3, timeout=60)
        finally:
            code = s.close()
        self.assertEqual((code, s.stderr()), (0, ""))
        return {t["name"] for t in tools}, call

    def test_module_entry_point_serves_read_only(self):
        home = new_home()
        names, call = self.handshake([sys.executable, "-B", "-m", "mhbm.mcp", "--home", home, "--read-only"])
        self.assertEqual(names, {t.name for t in registry.TOOLS if t.read_only})
        self.assertFalse(call["result"]["isError"])

    def test_options_can_come_from_the_environment(self):
        """A launcher such as a Claude Code plugin passes ``env`` values but cannot choose which flags to add."""
        cmd = [sys.executable, "-B", "-m", "mhbm.mcp", "--home"]
        for env, expected in (({"MHBM_MCP_READ_ONLY": "true"}, {t.name for t in registry.TOOLS if t.read_only}),
                              ({"MHBM_MCP_READ_ONLY": "false"}, {t.name for t in registry.TOOLS}),
                              ({"MHBM_MCP_NO_NETWORK": "1"}, {t.name for t in registry.TOOLS if not t.network}),
                              ({"MHBM_MCP_DISABLE_TOOLS": "bm_undo, bm_export"}, {t.name for t in registry.TOOLS} - {"bm_undo", "bm_export"})):
            home = new_home()
            names, _call = self.handshake(cmd + [home], env)
            self.assertEqual(names, expected, env)

    def test_home_comes_from_the_environment_and_may_be_relative(self):
        parent = Path(mcpdrive.scratch())
        shutil.copytree(new_home(), parent / "relative-lib")
        names, call = self.handshake([sys.executable, "-B", "-m", "mhbm.mcp"], {"MHBM_HOME": "relative-lib"}, cwd=str(parent))
        self.assertIn("bm_stats", names)
        self.assertEqual(call["result"]["structuredContent"]["total"], 592)

    def test_relative_home_works_for_plan_tools(self):
        """The default home is ``.mhbm`` (relative): plan files are written by a child process that runs elsewhere."""
        parent = Path(mcpdrive.scratch())
        shutil.copytree(new_home(), parent / "lib")
        s = Server("lib", cwd=str(parent))
        try:
            self.assertIn("result", s.init("2025-11-25"))
            r = s.rpc("tools/call", {"name": "bm_dedupe", "arguments": {}}, id=5, timeout=120)
            self.assertFalse(r["result"]["isError"], r)
            plan = r["result"]["structuredContent"]
            self.assertEqual(plan["entries"], 12)
            self.assertTrue((parent / "lib" / "plans" / (plan["plan_id"] + ".json")).is_file())
            r = s.rpc("tools/call", {"name": "bm_apply_plan", "arguments": {"plan_id": plan["plan_id"]}}, id=6, timeout=120)
            self.assertFalse(r["result"]["isError"], r)
        finally:
            s.close()
        self.assertEqual(count(str(parent / "lib")), 592 - 6)

    def test_the_old_module_name_still_imports(self):
        self.assertIn("bm_stats", mcp_server.TOOLS)
        self.assertTrue(callable(mcp_server.serve))

    def test_child_processes_find_the_package_in_a_source_checkout(self):
        parent = executor._package_parent()
        self.assertTrue(parent is None or os.path.isdir(os.path.join(parent, "mhbm")))
        env = executor.child_env()
        if parent:
            self.assertIn(parent, env["PYTHONPATH"].split(os.pathsep))
        self.assertNotIn("HTTP_PROXY_SECRET", env)


class Content(unittest.TestCase):
    def test_bookmark_uri_rejects_anything_but_an_id(self):
        class Ctx:
            home = new_home()
        with self.assertRaises(content.RpcError):
            content.read_resource("mhbm://bookmark/../../etc/passwd", Ctx(), lambda argv: {})
        with self.assertRaises(content.RpcError):
            content.read_resource("mhbm://bookmark/" + "a" * 200, Ctx(), lambda argv: {})

    def test_prompt_arguments_are_strings_and_bounded(self):
        out = content.get_prompt("find-bookmark", {"topic": "x" * 1000})
        self.assertLess(len(out["messages"][0]["content"]["text"]), 1000)
        with self.assertRaises(content.RpcError):
            content.get_prompt("tidy-unfiled", {"focus": 3})


if __name__ == "__main__":
    unittest.main()
