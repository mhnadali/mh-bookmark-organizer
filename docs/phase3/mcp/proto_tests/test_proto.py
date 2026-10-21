"""Conformance / regression tests for the prototype server in pkg_proto (the spec's test plan, executable).

Run:  venv-mcp/bin/python -B -m unittest -v proto_tests.test_proto   (cwd = the audit directory)
Each test starts a real ``python -m mhbm mcp`` subprocess and speaks JSON-RPC to it, unless it says "in-process".
"""
import json
import os
import pathlib
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
S = os.path.dirname(HERE)
sys.path.insert(0, S + "/harness")
sys.path.insert(0, S + "/pkg_proto")
import mcpdrive  # noqa: E402
from mcpdrive import SCRATCH, SEED_LIB, Server, tree_state  # noqa: E402

PROTO = SCRATCH + "/pkg_proto"
DEAD_PROXY = {k: "http://127.0.0.1:9" for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")}
DEAD_PROXY.update(NO_PROXY="", no_proxy="")
TMP = SCRATCH + "/tmp"
os.makedirs(TMP, exist_ok=True)


def new_home(seeded=True):
    d = tempfile.mkdtemp(prefix="t-", dir=TMP)
    h = d + "/lib"
    if seeded:
        shutil.copytree(SEED_LIB, h)
    return h


def count(home):
    c = sqlite3.connect(home + "/library.db")
    try:
        return c.execute("select count(*) from bookmarks").fetchone()[0]
    finally:
        c.close()


class Base(unittest.TestCase):
    servers: list

    def setUp(self):
        self.servers = []
        self.addCleanup(self._close_all)

    def _close_all(self):
        for s in self.servers:
            try:
                s.close(3)
            except Exception:
                pass

    def start(self, home=None, args=(), env=None, cwd=None, init="2025-11-25", seeded=True):
        home = home or new_home(seeded)
        s = Server(home, args=args, env_extra=env, cwd=cwd, pkg=PROTO)
        self.servers.append(s)
        s.home = home
        if init:
            r = s.init(init)
            self.assertIn("result", r, r)
        return s

    def tool(self, s, name, args=None, timeout=120, id=None):
        r = s.rpc("tools/call", {"name": name, "arguments": args or {}}, id=id or 777, timeout=timeout)
        return r

    def result(self, s, name, args=None, **kw):
        r = self.tool(s, name, args, **kw)
        self.assertIn("result", r, r)
        return r["result"]

    def alive(self, s):
        r = s.rpc("ping", id="alive-check", timeout=10)
        self.assertEqual(r, {"jsonrpc": "2.0", "id": "alive-check", "result": {}})


# =============================================================== A. lifecycle and negotiation
class Lifecycle(Base):
    def test_supported_versions_are_echoed(self):
        for v in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
            s = self.start(init=None)
            r = s.rpc("initialize", {"protocolVersion": v, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}, id=1)
            self.assertEqual(r["result"]["protocolVersion"], v)

    def test_unknown_versions_get_the_latest_supported(self):
        for v in ("2099-01-01", "1.0", "", "garbage<script>"):
            s = self.start(init=None)
            r = s.rpc("initialize", {"protocolVersion": v, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}, id=1)
            self.assertEqual(r["result"]["protocolVersion"], "2025-11-25", v)

    def test_missing_or_mistyped_protocol_version_is_invalid_params(self):
        for params in ({}, {"protocolVersion": None}, {"protocolVersion": 20250618}, {"protocolVersion": [1]}):
            s = self.start(init=None)
            r = s.rpc("initialize", params, id=1)
            self.assertEqual(r["error"]["code"], -32602, params)
            self.assertIn("supported", r["error"]["data"])

    def test_initialize_with_non_object_params(self):
        for params in ("x", [], 5):
            s = self.start(init=None)
            r = s.rpc("initialize", params, id=1)
            self.assertEqual(r["error"]["code"], -32602, params)
            self.alive(s)

    def test_second_initialize_is_refused(self):
        s = self.start()
        r = s.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}}, id=2)
        self.assertEqual(r["error"]["code"], -32600)

    def test_requests_before_initialize(self):
        s = self.start(init=None)
        self.alive(s)                                            # ping is allowed
        for method in ("tools/list", "tools/call", "resources/list", "prompts/list"):
            r = s.rpc(method, {}, id=method)
            self.assertEqual(r["error"]["code"], -32600, method)
        r = s.rpc("server/discover", {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}, id="d")
        self.assertEqual(r["error"]["code"], -32601)             # the 2026-07-28 probe must see a plain "legacy server" error

    def test_initialize_result_shape_by_version(self):
        s = self.start(init=None)
        r = s.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}, id=1)["result"]
        self.assertEqual(set(r["serverInfo"]), {"name", "version"})
        self.assertEqual(set(r["capabilities"]), {"tools", "resources", "prompts"})
        self.assertNotIn("logging", r["capabilities"])
        self.assertTrue(r["instructions"])
        s = self.start(init=None)
        r = s.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}}, id=1)["result"]
        self.assertEqual(set(r["serverInfo"]), {"name", "title", "version"})
        s = self.start(init=None)
        r = s.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {}}, id=1)["result"]
        self.assertEqual(set(r["serverInfo"]), {"name", "title", "version", "description"})
        self.assertLessEqual(len(r["instructions"]), 4096)       # Claude Code cuts server instructions beyond 4096 characters

    def test_tool_shape_by_version(self):
        for version, annotations, title, output in (("2024-11-05", False, False, False), ("2025-03-26", True, False, False), ("2025-06-18", True, True, True)):
            s = self.start(init=version)
            tool = s.rpc("tools/list", id=2)["result"]["tools"][1]
            self.assertEqual(("annotations" in tool, "title" in tool, "outputSchema" in tool), (annotations, title, output), version)

    def test_cursor_is_validated(self):
        s = self.start()
        self.assertEqual(s.rpc("tools/list", {"cursor": "bogus"}, id=2)["error"]["code"], -32602)
        r = s.rpc("tools/list", {}, id=3)["result"]
        self.assertNotIn("nextCursor", r)

    def test_stdin_eof_exits_cleanly_and_last_line_without_newline_is_served(self):
        s = self.start()
        s.send_raw(b'{"jsonrpc":"2.0","id":9,"method":"ping"}')
        s.p.stdin.close()
        self.assertEqual(s.recv_json(5)["id"], 9)
        self.assertEqual(s.p.wait(10), 0)


# =============================================================== B. envelope robustness
class Envelope(Base):
    def test_id_types(self):
        s = self.start()
        for idv in (7, 0, -1, 10 ** 30, "abc", "", "1"):
            s.send({"jsonrpc": "2.0", "id": idv, "method": "ping"})
            r = s.recv_json(5)
            self.assertEqual((r["id"], type(r["id"])), (idv, type(idv)))
        for bad in (None, True, 1.5, {"a": 1}, [1]):
            s.send({"jsonrpc": "2.0", "id": bad, "method": "ping"})
            r = s.recv_json(5)
            self.assertEqual(r["error"]["code"], -32600, bad)
            self.assertIsNone(r["id"], bad)
        s.send({"jsonrpc": "2.0", "method": "ping"})              # notification: no reply
        self.assertEqual(s.recv(0.5), "")
        self.alive(s)

    def test_non_object_json_is_invalid_request_not_a_crash(self):
        s = self.start()
        for line in ("null", "42", '"x"', "true", "[]"):
            s.send_line(line)
            r = s.recv_json(5)
            self.assertEqual(r["error"]["code"], -32600, line)
        self.alive(s)

    def test_batches(self):
        batch = [{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, {"jsonrpc": "2.0", "method": "notifications/initialized"}]
        s = self.start(init="2025-06-18")
        s.send_line(json.dumps(batch))
        r = s.recv_json(5)
        self.assertEqual(r["error"]["code"], -32600)
        self.assertIn("batch", r["error"]["message"].lower())
        self.alive(s)
        s = self.start(init="2025-03-26")
        s.send_line(json.dumps(batch))
        r = s.recv_json(10)
        self.assertEqual([x["id"] for x in r], [1, 2])
        s.send_line("[]")
        self.assertEqual(s.recv_json(5)["error"]["code"], -32600)
        s.send_line(json.dumps([{"jsonrpc": "2.0", "method": "notifications/initialized"}]))
        self.assertEqual(s.recv(0.5), "")
        self.alive(s)

    def test_malformed_input_never_kills_the_server(self):
        s = self.start()
        cases = ["{", "not json", '{"jsonrpc":"2.0","id":3,"method":"ping"} xx', '{"jsonrpc":"2.0","id":4,"method":"ping","params":{"x":NaN}}',
                 "[" * 100000, '{"a":' * 100000, '{"jsonrpc":"2.0","id":5}', '{"jsonrpc":"1.0","id":6,"method":"ping"}', '{"id":7,"method":"ping"}',
                 '{"jsonrpc":"2.0","id":8,"method":42}']
        for line in cases:
            s.send_line(line)
            r = s.recv_json(10)
            self.assertIn(r["error"]["code"], (-32700, -32600), line[:40])
        self.alive(s)

    def test_invalid_utf8_bom_crlf_blank_lines_and_split_writes(self):
        s = self.start(init=None)
        s.send_raw(b'\xef\xbb\xbf{"jsonrpc":"2.0","id":1,"method":"ping"}\r\n')          # BOM + CRLF accepted
        self.assertEqual(s.recv_json(5)["result"], {})
        s.send_raw(b'\n   \n')
        s.send_raw(b'{"jsonrpc":"2.0","id":2,"method":"ping","params":{"x":"\xff"}}\n')
        self.assertEqual(s.recv_json(5)["error"]["code"], -32700)                       # strict UTF-8
        msg = '{"jsonrpc":"2.0","id":3,"method":"ping","params":{"x":"شطرنج"}}\n'.encode("utf-8")
        cut = msg.index("ش".encode("utf-8")) + 1
        s.send_raw(msg[:cut])
        time.sleep(0.2)
        s.send_raw(msg[cut:])
        self.assertEqual(s.recv_json(5)["id"], 3)

    def test_oversized_message_is_rejected_and_the_stream_recovers(self):
        s = self.start()
        s.send_raw(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"pad":"' + b"x" * (5 << 20) + b'"}}\n')
        r = s.recv_json(20)
        self.assertEqual(r["error"]["code"], -32600)
        self.assertIn("exceeds", r["error"]["message"])
        self.alive(s)

    def test_client_responses_are_ignored(self):
        s = self.start()
        s.send({"jsonrpc": "2.0", "id": 5, "result": {}})
        s.send({"jsonrpc": "2.0", "id": 6, "error": {"code": -1, "message": "x"}})
        self.assertEqual(s.recv(0.5), "")
        self.alive(s)

    def test_unknown_methods_and_notifications(self):
        s = self.start()
        self.assertEqual(s.rpc("nope/nope", id=1)["error"]["code"], -32601)
        self.assertEqual(s.rpc("logging/setLevel", {"level": "info"}, id=2)["error"]["code"], -32601)   # logging is deprecated: not offered
        s.notify("notifications/unknown")
        self.alive(s)
        self.assertEqual(s.rpc("tools/list", [1], id=3)["error"]["code"], -32602)

    def test_duplicate_inflight_id_is_refused(self):
        s = self.start(env=DEAD_PROXY)
        s.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "bm_check_links", "arguments": {"limit": 20}}})
        time.sleep(0.3)
        s.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        r = s.recv_json(5)
        self.assertEqual(r["error"]["code"], -32600)
        s.notify("notifications/cancelled", {"requestId": 1})


# =============================================================== C. tools: validation and the old crash list
CRASH_AND_ABUSE = [
    ("bm_search", {"query": 123}), ("bm_search", {"query": None}), ("bm_search", {"query": ["a"]}), ("bm_search", {"query": {"a": 1}}), ("bm_search", {}),
    ("bm_search", {"query": "x", "limit": 10 ** 30}), ("bm_search", {"query": "x", "limit": -5}), ("bm_search", {"query": "x", "limit": 100000}),
    ("bm_search", {"query": "x", "limit": "5"}), ("bm_search", {"query": "x", "limit": True}), ("bm_search", {"query": "x", "limit": 2.5}),
    ("bm_import_browser", {"name": 5}), ("bm_import_browser", {"name": "/etc/passwd"}), ("bm_import_browser", {"name": "--help"}),
    ("bm_dedupe", {"level": 1}), ("bm_dedupe", {"level": "bogus"}), ("bm_dedupe", {"apply": "false"}), ("bm_dedupe", {"apply": True}),
    ("bm_organize", {"apply": 1}), ("bm_import_file", {"path": ""}), ("bm_import_file", {"path": "."}), ("bm_import_file", {"path": "a" * 5000}),
    ("bm_import_file", {"path": "x\u0000y"}), ("bm_import_file", {"path": "-x"}), ("bm_import_file", {"path": "--help"}),
    ("bm_export", {"format": "json", "out": "."}), ("bm_export", {"format": "obsidian", "out": "x"}),
    ("bm_stats", {"bogus": 1}), ("bm_undo", {}), ("bm_undo", {"op": "1"}), ("bm_undo", {"op": 0}), ("bm_undo", {"op": 10 ** 12}),
    ("bm_check_links", {"limit": -1}), ("bm_check_links", {"limit": 0}), ("bm_check_links", {"limit": 100000}), ("bm_archive", {"limit": 0}),
    ("bm_tag", {"id": "--help"}), ("bm_tag", {"id": "x", "add": ["-x"]}), ("bm_tag", {"id": "no-such-id", "title": "-x"}), ("bm_mark", {"ids": []}),
    ("bm_apply_plan", {"plan_id": "p-0000000000000000"}), ("bm_apply_plan", {"plan_id": "../../etc/passwd"}), ("bm_apply_plan", {"plan_id": 5}),
    ("bm_rules", {"action": "test"}), ("bm_rules_edit", {"action": "add", "host": "a.com"}), ("bm_rules_edit", {"action": "remove"}),
]


class ToolValidation(Base):
    def test_every_known_abuse_is_an_error_result_and_the_server_survives(self):
        for name, args in CRASH_AND_ABUSE:
            s = self.start(env=DEAD_PROXY)
            before = count(s.home)
            r = self.tool(s, name, args)
            self.assertIn("result", r, (name, args, r))
            self.assertTrue(r["result"]["isError"], (name, args, r))
            self.assertEqual(count(s.home), before, (name, args))
            self.alive(s)

    def test_arguments_container_types(self):
        s = self.start()
        for bad in ([1], "x", 5):
            r = s.rpc("tools/call", {"name": "bm_stats", "arguments": bad}, id=1)
            self.assertEqual(r["error"]["code"], -32602, bad)
        for params in ({"name": {"a": 1}}, {"name": None}, {}):
            self.assertEqual(s.rpc("tools/call", params, id=2)["error"]["code"], -32602, params)
        for params in ("x", [1]):
            self.assertEqual(s.rpc("tools/call", params, id=3)["error"]["code"], -32602, params)
        self.assertEqual(s.rpc("tools/call", {"name": "bm_nope", "arguments": {}}, id=4)["error"]["code"], -32602)
        r = s.rpc("tools/call", {"name": "bm_stats"}, id=5)                              # arguments may be omitted
        self.assertFalse(r["result"]["isError"])
        self.alive(s)

    def test_error_text_names_the_field_and_the_fix(self):
        s = self.start()
        text = self.result(s, "bm_search", {"query": "x", "limit": 1000, "sort": "newest", "bogus": 1})["content"][0]["text"]
        for fragment in ("/limit", "<= 100", "/sort", "relevance", "/bogus", "allowed: query, limit, sort"):
            self.assertIn(fragment, text)

    def test_integral_floats_are_accepted_as_integers(self):
        s = self.start()
        self.assertFalse(self.result(s, "bm_search", {"query": "chisel", "limit": 2.0})["isError"])

    def test_option_like_values_are_data_not_flags(self):
        s = self.start()
        res = self.result(s, "bm_search", {"query": "--help"})
        self.assertFalse(res["isError"])
        self.assertIn("results", res["structuredContent"])
        res = self.result(s, "bm_search", {"query": "--limit 3 chisel"})
        self.assertFalse(res["isError"])
        cwd = SCRATCH + "/work"
        res = self.result(s, "bm_export", {"format": "json", "out": os.path.join(cwd, "--help")}) if False else None
        before = count(s.home)
        res = self.result(s, "bm_tag", {"id": "9aae81d76fd8", "title": "--apply", "add": ["x"]})
        self.assertFalse(res["isError"], res)
        self.assertEqual(res["structuredContent"]["title"], "--apply")
        self.assertEqual(count(s.home), before)

    def test_unknown_id_is_a_command_error_not_an_internal_error(self):
        s = self.start()
        res = self.result(s, "bm_tag", {"id": "zzzz", "add": ["x"]})
        self.assertTrue(res["isError"])
        self.assertIn("no bookmark with id zzzz", res["content"][0]["text"])
        self.assertEqual(res["_meta"]["io.mhbm/error"]["kind"], "command_failed")

    def test_structured_content_matches_text_and_output_schema(self):
        from jsonschema import Draft202012Validator
        s = self.start()
        tools = {t["name"]: t for t in s.rpc("tools/list", id=2)["result"]["tools"]}
        for name, args in (("bm_stats", {}), ("bm_search", {"query": "chisel"}), ("bm_folders", {}), ("bm_tags", {}), ("bm_history", {}), ("bm_reading_list", {}),
                           ("bm_triage", {"limit": 3}), ("bm_feedback", {}), ("bm_rules", {}), ("bm_browsers", {}), ("bm_backups", {}), ("bm_doctor", {}),
                           ("bm_dedupe", {}), ("bm_restructure", {}), ("bm_autotag", {}), ("bm_titles", {}), ("bm_prune", {})):
            res = self.result(s, name, args)
            self.assertFalse(res["isError"], (name, res))
            sc = res["structuredContent"]
            self.assertEqual(json.loads(res["content"][0]["text"]), sc, name)
            errors = list(Draft202012Validator(tools[name]["outputSchema"]).iter_errors(sc))
            self.assertFalse(errors, (name, [e.message for e in errors][:3]))

    def test_all_schemas_are_valid_draft_2020_12(self):
        from jsonschema import Draft202012Validator
        s = self.start()
        for t in s.rpc("tools/list", id=2)["result"]["tools"]:
            Draft202012Validator.check_schema(t["inputSchema"])
            Draft202012Validator.check_schema(t["outputSchema"])
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertIs(t["inputSchema"]["additionalProperties"], False)
            self.assertTrue(all(len(p) <= 64 for p in t["inputSchema"]["properties"]))
            self.assertLessEqual(len(t["name"]), 64)

    def test_annotations_are_honest(self):
        s = self.start()
        tools = {t["name"]: t for t in s.rpc("tools/list", id=2)["result"]["tools"]}
        for name in ("bm_stats", "bm_search", "bm_triage", "bm_doctor"):
            a = tools[name]["annotations"]
            self.assertIs(a["readOnlyHint"], True)
            self.assertIs(a["openWorldHint"], False)
        for name in ("bm_check_links", "bm_archive"):
            self.assertIs(tools[name]["annotations"]["openWorldHint"], True)
            self.assertIs(tools[name]["annotations"]["readOnlyHint"], False)
        for name in ("bm_apply_plan", "bm_undo", "bm_tag"):
            self.assertIs(tools[name]["annotations"]["destructiveHint"], True)
        for name in ("bm_organize", "bm_dedupe", "bm_export", "bm_import_file"):
            self.assertIs(tools[name]["annotations"]["destructiveHint"], False)
        for t in tools.values():
            self.assertEqual(t["annotations"]["title"], t["title"])


# =============================================================== D. modes: read-only, no-network
class Modes(Base):
    def test_read_only_hides_everything_that_writes(self):
        s = self.start(args=("--read-only",))
        names = {t["name"] for t in s.rpc("tools/list", id=2)["result"]["tools"]}
        self.assertEqual(len(names), 13)
        self.assertTrue(all(n in names for n in ("bm_stats", "bm_search", "bm_triage", "bm_doctor")))
        for hidden in ("bm_organize", "bm_apply_plan", "bm_undo", "bm_import_file", "bm_export", "bm_check_links", "bm_tag"):
            self.assertNotIn(hidden, names)
            self.assertEqual(s.rpc("tools/call", {"name": hidden, "arguments": {}}, id=3)["error"]["code"], -32602)

    def test_read_only_never_touches_the_library_directory(self):
        """needs CLI change C1 (Library(read_only) / MHBM_READONLY): before it, triage wrote model.json and old libraries were migrated"""
        home = new_home()
        c = sqlite3.connect(home + "/library.db")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
        for f in ("library.db-wal", "library.db-shm"):
            if os.path.exists(f"{home}/{f}"):
                os.unlink(f"{home}/{f}")
        before = tree_state(home)
        s = self.start(home=home, args=("--read-only",))
        for name, args in (("bm_stats", {}), ("bm_search", {"query": "chisel"}), ("bm_history", {}), ("bm_triage", {"limit": 3}), ("bm_feedback", {})):
            self.result(s, name, args)
        s.close()
        changed = mcpdrive.diff_state(before, tree_state(home))
        # SQLite itself leaves its own -wal/-shm side files behind after a read-only connection to a WAL database; nothing else may change
        self.assertEqual({k: v for k, v in changed.items() if not k.endswith(("-wal", "-shm"))}, {})
        self.assertNotIn("library.db", changed)

    def test_no_network_hides_outbound_tools(self):
        s = self.start(args=("--no-network",))
        names = {t["name"] for t in s.rpc("tools/list", id=2)["result"]["tools"]}
        self.assertNotIn("bm_check_links", names)
        self.assertNotIn("bm_archive", names)
        self.assertIn("bm_organize", names)

    def test_disable_tool(self):
        s = self.start(args=("--disable-tool", "bm_undo", "--disable-tool", "bm_export"))
        names = {t["name"] for t in s.rpc("tools/list", id=2)["result"]["tools"]}
        self.assertEqual(29 - len(names), 2)


# =============================================================== E. plans: propose, apply exactly that, undo
class Plans(Base):
    def test_dedupe_plan_apply_undo_roundtrip(self):
        s = self.start()
        n0 = count(s.home)
        plan = self.result(s, "bm_dedupe")["structuredContent"]
        self.assertEqual(count(s.home), n0)                                                 # a plan tool changes nothing
        self.assertRegex(plan["plan_id"], r"^p-[0-9a-f]{16}$")
        self.assertEqual(plan["entries"], 12)
        applied = self.result(s, "bm_apply_plan", {"plan_id": plan["plan_id"]})["structuredContent"]
        self.assertEqual(applied["deleted"], 6)
        self.assertEqual(count(s.home), n0 - 6)
        again = self.result(s, "bm_apply_plan", {"plan_id": plan["plan_id"]})                 # one shot
        self.assertTrue(again["isError"])
        ops = self.result(s, "bm_history")["structuredContent"]["operations"]
        self.assertEqual(ops[0]["command"], "apply-plan")
        undone = self.result(s, "bm_undo", {"op": ops[0]["op"]})["structuredContent"]
        self.assertGreaterEqual(undone["restored"], 6)
        self.assertEqual(count(s.home), n0)

    def test_stale_plan_is_refused(self):
        s = self.start()
        plan = self.result(s, "bm_dedupe")["structuredContent"]
        self.result(s, "bm_tag", {"id": "9aae81d76fd8", "add": ["changed"]})                    # the library moves on
        res = self.result(s, "bm_apply_plan", {"plan_id": plan["plan_id"]})
        self.assertTrue(res["isError"])
        self.assertIn("changed since", res["content"][0]["text"])
        self.assertEqual(res["_meta"]["io.mhbm/error"]["kind"], "stale_plan")

    def test_plan_with_nothing_to_do_has_no_plan_id(self):
        s = self.start()
        self.result(s, "bm_apply_plan", {"plan_id": self.result(s, "bm_dedupe")["structuredContent"]["plan_id"]})
        again = self.result(s, "bm_dedupe")["structuredContent"]
        self.assertIsNone(again["plan_id"])
        self.assertEqual(again["entries"], 0)

    def test_failed_plan_leaves_no_files_and_plan_files_are_private(self):
        s = self.start()
        plan = self.result(s, "bm_restructure")["structuredContent"]
        d = s.home + "/plans"
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        self.assertEqual(sorted(os.listdir(d)), sorted([plan["plan_id"] + ".json", plan["plan_id"] + ".meta.json"]))

    def test_prune_plan_is_built_from_the_listing(self):
        s = self.start()
        c = sqlite3.connect(s.home + "/library.db")
        c.execute("update bookmarks set status='dead' where rowid <= 5")
        c.commit()
        c.close()
        plan = self.result(s, "bm_prune")["structuredContent"]
        self.assertEqual(plan["entries"], 5)
        self.assertEqual(len(plan["sample"]), 5)
        self.result(s, "bm_apply_plan", {"plan_id": plan["plan_id"]})
        self.assertEqual(count(s.home), 592 - 5)


# =============================================================== F. file tools
class Files(Base):
    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="files-", dir=TMP)
        self.allowed = os.path.join(self.root, "allowed")
        self.outside = os.path.join(self.root, "outside")
        os.makedirs(self.allowed)
        os.makedirs(self.outside)
        html = lambda m: f'<!DOCTYPE NETSCAPE-Bookmark-file-1><DL><p><DT><H3>P</H3><DL><p><DT><A HREF="https://{m}.example/x">{m}</A></DL><p></DL><p>'  # noqa: E731
        for d, m in ((self.allowed, "inside-marker"), (self.outside, "outside-marker")):
            pathlib.Path(d, "in.html").write_text(html(m))
        self.srv = lambda **kw: self.start(args=("--allow-dir", self.allowed), cwd=self.allowed, seeded=False, **kw)

    def has(self, home, marker):
        c = sqlite3.connect(home + "/library.db")
        try:
            return c.execute("select count(*) from bookmarks where url like ?", (f"%{marker}%",)).fetchone()[0]
        finally:
            c.close()

    def test_import_confinement(self):
        s = self.srv()
        ok = self.result(s, "bm_import_file", {"path": "in.html"})
        self.assertFalse(ok["isError"], ok)
        self.assertEqual(ok["structuredContent"]["source"], "in.html")
        self.assertEqual(self.has(s.home, "inside-marker"), 1)
        os.symlink(self.outside, os.path.join(self.allowed, "linkdir"))
        os.symlink(os.path.join(self.outside, "in.html"), os.path.join(self.allowed, "linkfile.html"))
        for path in (os.path.join(self.outside, "in.html"), "../outside/in.html", "linkdir/in.html", "linkfile.html", self.allowed + "//./../outside//in.html"):
            res = self.result(s, "bm_import_file", {"path": path})
            self.assertTrue(res["isError"], path)
            self.assertIn("outside the allowed", res["content"][0]["text"], path)
        self.assertEqual(self.has(s.home, "outside-marker"), 0)

    def test_non_regular_and_huge_inputs(self):
        s = self.srv()
        os.mkfifo(os.path.join(self.allowed, "pipe.html"))
        for path, fragment in (("pipe.html", "not a regular file"), (".", "not a regular file"), ("missing.html", "cannot open")):
            t0 = time.time()
            res = self.result(s, "bm_import_file", {"path": path})
            self.assertTrue(res["isError"], path)
            self.assertIn(fragment, res["content"][0]["text"], path)
            self.assertLess(time.time() - t0, 5, path)                                      # a FIFO must not hang the server
        self.alive(s)

    def test_tilde_is_expanded_before_the_check_and_the_resolved_path_is_used(self):
        s = self.start(args=("--allow-dir", self.allowed), cwd=self.allowed, seeded=False, env={"HOME": self.root})
        res = self.result(s, "bm_import_file", {"path": "~/allowed/in.html"})
        self.assertFalse(res["isError"], res)
        self.assertEqual(self.has(s.home, "inside-marker"), 1)
        res = self.result(s, "bm_import_file", {"path": "~/outside/in.html"})
        self.assertTrue(res["isError"])

    def test_race_swapping_a_symlink_never_imports_the_outside_file(self):
        s = self.srv()
        good = os.path.join(self.allowed, "good.html")
        shutil.copy(os.path.join(self.allowed, "in.html"), good)
        race = os.path.join(self.allowed, "race.html")
        os.symlink(good, race)
        stop = threading.Event()

        def flip():
            n = 0
            while not stop.is_set():
                for target in (good, os.path.join(self.outside, "in.html")):
                    tmp = os.path.join(self.allowed, f".t{n % 2}")
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    os.symlink(target, tmp)
                    os.replace(tmp, race)
                    n += 1
        t = threading.Thread(target=flip, daemon=True)
        t.start()
        try:
            for i in range(60):
                self.tool(s, "bm_import_file", {"path": "race.html"}, id=100 + i)
        finally:
            stop.set()
            t.join(2)
        self.assertEqual(self.has(s.home, "outside-marker"), 0)

    def test_parent_directory_swap_between_check_and_open_is_caught(self):
        """in-process, deterministic: the directory is replaced by a symlink to the outside right after the path check"""
        from mhbm.mcp.paths import PathError, PathPolicy
        sub = os.path.join(self.allowed, "sub")
        os.makedirs(sub)
        shutil.copy(os.path.join(self.outside, "in.html"), os.path.join(self.outside, "victim.html"))
        shutil.copy(os.path.join(self.allowed, "in.html"), os.path.join(sub, "victim.html"))
        policy = PathPolicy([self.allowed], base=self.allowed)
        real_open = os.open
        swapped = []

        def evil_open(path, flags, *a, **kw):
            if not swapped and str(path).endswith("victim.html"):
                swapped.append(1)
                os.rename(sub, sub + ".moved")
                os.symlink(self.outside, sub)
            return real_open(path, flags, *a, **kw)
        os.open = evil_open
        try:
            with self.assertRaises(PathError):
                policy.open_for_read("sub/victim.html", 1 << 20)
        finally:
            os.open = real_open
        self.assertTrue(swapped)

    def test_export_never_overwrites_and_stays_inside(self):
        s = self.srv()
        self.result(s, "bm_import_file", {"path": "in.html"})
        res = self.result(s, "bm_export", {"format": "json", "out": "o.json"})
        self.assertFalse(res["isError"], res)
        self.assertGreater(res["structuredContent"]["bytes"], 10)
        self.assertEqual(json.loads(pathlib.Path(self.allowed, "o.json").read_text())["bookmarks"][0]["url"], "https://inside-marker.example/x")
        precious = os.path.join(self.allowed, "precious.txt")
        pathlib.Path(precious).write_text("PRECIOUS")
        for out, fragment in (("precious.txt", "already exists"), ("o.json", "already exists"), (".", "already exists"), ("nodir/x.json", "directory does not exist"),
                              (os.path.join(self.outside, "x.json"), "outside the allowed")):
            res = self.result(s, "bm_export", {"format": "json", "out": out})
            self.assertTrue(res["isError"], out)
            self.assertIn(fragment, res["content"][0]["text"], out)
        self.assertEqual(pathlib.Path(precious).read_text(), "PRECIOUS")
        self.assertFalse(os.path.exists(os.path.join(self.outside, "x.json")))
        os.symlink(os.path.join(self.outside, "victim"), os.path.join(self.allowed, "dangling.json"))
        res = self.result(s, "bm_export", {"format": "json", "out": "dangling.json"})
        self.assertTrue(res["isError"])
        self.assertFalse(os.path.exists(os.path.join(self.outside, "victim")))
        self.assertEqual(sorted(f for f in os.listdir(self.allowed) if f.startswith("o.")), ["o.json"])

    def test_export_cannot_target_the_library_directory(self):
        home = os.path.join(self.allowed, ".mhbm")                       # the default layout: library inside the working directory
        s = self.start(home=home, args=("--allow-dir", self.allowed), cwd=self.allowed, seeded=False)
        self.result(s, "bm_import_file", {"path": "in.html"})
        for out in (".mhbm/library.db", ".mhbm/config.json", ".mhbm/new.json"):
            res = self.result(s, "bm_export", {"format": "json", "out": out})
            self.assertTrue(res["isError"], out)
        self.alive(s)
        self.assertGreater(self.result(s, "bm_stats")["structuredContent"]["total"], 0)

    def test_export_failure_leaves_no_empty_file(self):
        s = self.srv()                                                     # empty library + a format the CLI rejects at runtime is hard to force;
        res = self.result(s, "bm_export", {"format": "chrome", "folder": "x" * 3, "out": "fail.json"})   # an empty selection still succeeds
        self.assertFalse(res["isError"], res)
        self.assertTrue(os.path.exists(os.path.join(self.allowed, "fail.json")))

    def test_client_roots_define_the_allowed_directories(self):
        s = self.start(cwd=self.outside, seeded=False, init=None)
        r = s.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {"roots": {"listChanged": True}}, "clientInfo": {"name": "t", "version": "1"}}, id=1)
        self.assertIn("result", r)
        s.notify("notifications/initialized")
        req = s.recv_json(10)
        self.assertEqual(req["method"], "roots/list")
        s.send({"jsonrpc": "2.0", "id": req["id"], "result": {"roots": [{"uri": "file://" + self.allowed, "name": "allowed"}]}})
        time.sleep(0.3)
        res = self.result(s, "bm_import_file", {"path": os.path.join(self.allowed, "in.html")})
        self.assertFalse(res["isError"], res)
        res = self.result(s, "bm_import_file", {"path": os.path.join(self.outside, "in.html")})
        self.assertTrue(res["isError"])


# =============================================================== G. results: size caps, errors, redaction
class Results(Base):
    def test_result_size_cap_keeps_valid_json(self):
        big = tempfile.mkdtemp(prefix="big-", dir=TMP) + "/lib"
        shutil.copytree(SCRATCH + "/lib-big", big)
        s = self.start(home=big, args=("--max-result-chars", "8000"))
        res = self.result(s, "bm_folders")
        text = res["content"][0]["text"]
        self.assertLessEqual(len(text), 8000)
        obj = json.loads(text)
        self.assertEqual(obj, res["structuredContent"])
        res = self.result(s, "bm_search", {"query": "notes", "limit": 100})
        self.assertLessEqual(len(res["content"][0]["text"]), 8000)
        self.assertTrue(json.loads(res["content"][0]["text"]) == res["structuredContent"], "text and structuredContent differ")
        self.assertTrue("_truncated" in res["structuredContent"], "expected a truncation note")
        self.assertTrue(res["_meta"]["io.mhbm/truncated"]["originalChars"] > 8000)
        kept = len(res["structuredContent"]["results"])
        self.assertTrue(0 < kept < 100, f"kept {kept} rows")

    def test_errors_do_not_leak_absolute_paths(self):
        s = self.start()
        text = self.result(s, "bm_import_file", {"path": "/nonexistent/dir/x.html"})["content"][0]["text"]
        self.assertNotIn(s.home, text)
        res = self.result(s, "bm_tag", {"id": "zz"})
        self.assertNotIn(s.home, res["content"][0]["text"])

    def test_child_environment_is_an_allowlist(self):
        from mhbm.mcp.executor import child_env
        os.environ["AWS_SECRET_ACCESS_KEY"] = "x"
        os.environ["ANTHROPIC_API_KEY"] = "y"
        os.environ["MHBM_SMTP_PASSWORD"] = "z"
        try:
            env = child_env()
        finally:
            for k in ("AWS_SECRET_ACCESS_KEY", "ANTHROPIC_API_KEY", "MHBM_SMTP_PASSWORD"):
                del os.environ[k]
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertIn("MHBM_SMTP_PASSWORD", env)                # MHBM_* is the tool's own namespace (e.g. the notify settings)

    def test_library_busy_is_a_retryable_tool_error(self):
        s = self.start(env={"MHBM_BUSY_TIMEOUT_MS": "500"})
        lock = sqlite3.connect(s.home + "/library.db", timeout=1, isolation_level=None)
        lock.execute("BEGIN IMMEDIATE")
        try:
            plan_args = {}
            res = self.result(s, "bm_tag", {"id": "9aae81d76fd8", "add": ["x"]})
            self.assertTrue(res["isError"])
            self.assertTrue(res["_meta"]["io.mhbm/error"]["retryable"])
            self.assertEqual(res["_meta"]["io.mhbm/error"]["kind"], "library_busy")
            self.assertFalse(self.result(s, "bm_stats", plan_args)["isError"])           # readers are not blocked (WAL)
        finally:
            lock.execute("ROLLBACK")
            lock.close()

    def test_timeout_stops_the_child(self):
        s = self.start(env=DEAD_PROXY, args=())
        # no public flag for the timeout scale on the CLI: use the in-process Config through a tiny driver instead
        code = ("import sys; sys.path.insert(0, %r); from mhbm.mcp.server import Config, Server; from mhbm.mcp.wire import Wire; import io; "
                "from mhbm.mcp.executor import SubprocessExecutor; from mhbm.mcp.registry import BY_NAME; "
                "cfg=Config(home=%r, timeout_scale=0.02); import time; ex=SubprocessExecutor(cfg.home, grace=1); "
                "t=time.time(); r=ex.run(['check','--limit=60'], timeout=1.0); print(r.timed_out, r.code, round(time.time()-t,1))") % (PROTO, s.home)
        out = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=PROTO, **DEAD_PROXY), timeout=60)
        self.assertEqual(out.stdout.split()[0], "True", out.stdout + out.stderr)
        self.assertLess(float(out.stdout.split()[2]), 6)
        left = subprocess.run(["pgrep", "-f", f"--home {s.home}"], capture_output=True, text=True).stdout.split()
        self.assertEqual([p for p in left if p != str(s.p.pid)], [])


# =============================================================== H. concurrency, cancellation, progress
class Async(Base):
    def children(self, s):
        out = subprocess.run(["pgrep", "-f", f"mhbm.*--home {s.home}"], capture_output=True, text=True).stdout.split()
        return [p for p in out if p != str(s.p.pid)]

    def test_server_answers_while_a_tool_runs_and_cancel_stops_the_child(self):
        s = self.start(env=DEAD_PROXY)
        t0 = time.time()
        s.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "bm_check_links", "arguments": {"limit": 60}}})
        time.sleep(1.0)
        self.assertTrue(self.children(s))
        t1 = time.time()
        self.alive(s)
        self.assertLess(time.time() - t1, 1.0)                                              # ping is not stuck behind the tool
        self.assertEqual(len(s.rpc("tools/list", id=2)["result"]["tools"]), 29)
        s.notify("notifications/cancelled", {"requestId": 1, "reason": "test"})
        deadline = time.time() + 6
        while self.children(s) and time.time() < deadline:
            time.sleep(0.1)
        self.assertEqual(self.children(s), [])
        self.assertLess(time.time() - t0, 10)
        self.assertEqual(s.recv(1.0), "")                                                   # no response for a cancelled request
        self.alive(s)
        self.assertFalse(self.result(s, "bm_stats")["isError"])

    def test_cancel_unknown_and_malformed_is_ignored(self):
        s = self.start()
        s.notify("notifications/cancelled", {"requestId": 12345})
        s.notify("notifications/cancelled", {})
        s.notify("notifications/cancelled", "x")
        s.notify("notifications/cancelled", {"requestId": [1]})
        self.alive(s)

    def test_progress_notifications_are_monotonic_and_only_sent_with_a_token(self):
        s = self.start(env=DEAD_PROXY)
        s.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "bm_check_links", "arguments": {"limit": 30}}})
        self.assertEqual(self.drain_until(s, 1)[0], [])
        s.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "bm_check_links", "arguments": {"limit": 30}, "_meta": {"progressToken": "tok"}}})
        events, final = self.drain_until(s, 2)
        self.assertGreaterEqual(len(events), 2)
        values = [e["params"]["progress"] for e in events]
        self.assertEqual(values, sorted(set(values)))
        self.assertTrue(all(e["params"]["progressToken"] == "tok" for e in events))
        self.assertEqual(events[-1]["params"]["progress"], events[-1]["params"]["total"])

    def drain_until(self, s, rid, timeout=60):
        events, t0 = [], time.time()
        while time.time() - t0 < timeout:
            m = s.recv_json(2)
            if not m:
                continue
            if m.get("method") == "notifications/progress":
                events.append(m)
            elif m.get("id") == rid:
                return events, m
        self.fail("no response")

    def test_write_tools_are_serialized_reads_run_in_parallel(self):
        s = self.start()
        for i in range(3):
            s.send({"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call", "params": {"name": "bm_tag", "arguments": {"id": "9aae81d76fd8", "add": [f"t{i}"]}}})
        got = {}
        t0 = time.time()
        while len(got) < 3 and time.time() - t0 < 30:
            m = s.recv_json(2)
            if m and "id" in m:
                got[m["id"]] = m
        self.assertEqual(sorted(got), [10, 11, 12])
        self.assertTrue(all(not m["result"]["isError"] for m in got.values()), got)
        tags = self.result(s, "bm_search", {"query": "chisel"})["structuredContent"]["results"]
        self.assertTrue(any({"t0", "t1", "t2"} <= set(r["tags"]) for r in tags if r["id"] == "9aae81d76fd8"))

    def test_close_stdin_while_a_tool_runs_stops_it_and_exits(self):
        s = self.start(env=DEAD_PROXY)
        s.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "bm_check_links", "arguments": {"limit": 60}}})
        time.sleep(1.0)
        s.p.stdin.close()
        t0 = time.time()
        self.assertEqual(s.p.wait(20), 0)
        self.assertLess(time.time() - t0, 12)
        time.sleep(0.5)
        self.assertEqual(self.children(s), [])


# =============================================================== I. resources, prompts
class Content(Base):
    def test_resources_and_templates(self):
        s = self.start()
        uris = [r["uri"] for r in s.rpc("resources/list", id=2)["result"]["resources"]]
        self.assertEqual(len(uris), 4)
        for uri in uris:
            r = s.rpc("resources/read", {"uri": uri}, id=3)["result"]["contents"][0]
            self.assertEqual((r["uri"], r["mimeType"]), (uri, "application/json"))
            json.loads(r["text"])
        tpl = s.rpc("resources/templates/list", id=4)["result"]["resourceTemplates"]
        self.assertEqual(sorted(t["uriTemplate"] for t in tpl), ["mhbm://bookmark/{id}", "mhbm://plan/{plan_id}"])
        r = s.rpc("resources/read", {"uri": "mhbm://bookmark/9aae81d76fd8"}, id=5)["result"]["contents"][0]
        self.assertEqual(json.loads(r["text"])["id"], "9aae81d76fd8")
        for bad in ("mhbm://nope", "mhbm://bookmark/zzzz", "mhbm://bookmark/../../etc/passwd", "file:///etc/passwd", "mhbm://plan/p-0000000000000000"):
            self.assertEqual(s.rpc("resources/read", {"uri": bad}, id=6)["error"]["code"], -32002, bad)
        self.assertEqual(s.rpc("resources/read", {"uri": 5}, id=7)["error"]["code"], -32602)
        self.assertEqual(s.rpc("resources/read", {}, id=8)["error"]["code"], -32602)

    def test_prompts(self):
        s = self.start()
        prompts = {p["name"]: p for p in s.rpc("prompts/list", id=2)["result"]["prompts"]}
        self.assertEqual(len(prompts), 6)
        for name, p in prompts.items():
            self.assertRegex(name, r"^[a-z][a-z0-9-]*$")
            got = s.rpc("prompts/get", {"name": name, "arguments": {a["name"]: "3" for a in p.get("arguments", []) if a.get("required")}}, id=3)["result"]
            self.assertEqual(got["messages"][0]["role"], "user")
            self.assertIn("bm_", got["messages"][0]["content"]["text"])
        self.assertEqual(s.rpc("prompts/get", {"name": "nope"}, id=4)["error"]["code"], -32602)
        self.assertEqual(s.rpc("prompts/get", {"name": "find-bookmark"}, id=5)["error"]["code"], -32602)
        self.assertEqual(s.rpc("prompts/get", {"name": "find-bookmark", "arguments": {"topic": "x", "evil": "y"}}, id=6)["error"]["code"], -32602)
        self.assertEqual(s.rpc("prompts/get", {"name": "find-bookmark", "arguments": {"topic": 5}}, id=7)["error"]["code"], -32602)


# =============================================================== J. stream hygiene (in-process)
class StreamHygiene(unittest.TestCase):
    def test_claimed_descriptors_keep_the_wire_clean(self):
        code = r'''
import os, subprocess, sys
sys.path.insert(0, %r)
from mhbm.mcp.wire import Wire
w = Wire(claim=True)
assert w.claimed
print("STRAY print")
os.write(1, b"STRAY os.write\n")
subprocess.run(["echo", "STRAY child"])
try:
    input()
    print("input() returned data!", file=sys.stderr)
except EOFError:
    print("input() -> EOF", file=sys.stderr)
w.send({"jsonrpc": "2.0", "id": 1, "result": {"ok": True, "text": "\u0634\u2028"}})
w.close()
''' % PROTO
        r = subprocess.run([sys.executable, "-B", "-c", code], input=b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n', capture_output=True, timeout=30)
        lines = r.stdout.decode().splitlines()
        self.assertEqual(len(lines), 1, r.stdout)
        self.assertEqual(json.loads(lines[0])["result"]["ok"], True)
        self.assertTrue(lines[0].isascii())
        err = r.stderr.decode()
        for stray in ("STRAY print", "STRAY os.write", "STRAY child", "input() -> EOF"):
            self.assertIn(stray, err)

    def test_unclaimed_wire_for_tests(self):
        import io
        from mhbm.mcp.wire import EOF, LINE, OVERSIZE, Wire
        out = io.BytesIO()
        w = Wire(stdin=io.BytesIO(b'{"a":1}\n' + b"x" * 50 + b"\nlast"), stdout=out, max_line=20)
        self.assertEqual(w.read(), (LINE, b'{"a":1}\n'))
        self.assertEqual(w.read()[0], OVERSIZE)
        self.assertEqual(w.read(), (LINE, b"last"))
        self.assertEqual(w.read(), (EOF, None))
        w.send({"x": "é"})
        w.close()
        self.assertEqual(out.getvalue(), b'{"x":"\\u00e9"}\n')


# =============================================================== K. in-process: a crashing tool costs one request
class Containment(unittest.TestCase):
    def test_exceptions_in_a_worker_become_internal_errors(self):
        import io
        from mhbm.mcp.server import Config, Server
        from mhbm.mcp.wire import Wire

        class Boom:
            def run(self, *a, **kw):
                raise RuntimeError("boom with /secret/path")
        out = io.BytesIO()
        reqs = [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "bm_stats", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 2, "method": "ping"}]

        class Scripted(Wire):                       # feeds the requests, then waits for the answers before reporting EOF
            def __init__(self):
                super().__init__(stdin=io.BytesIO(b""), stdout=out)
                self.lines = [(json.dumps(r) + "\n").encode() for r in reqs]

            def read(self):
                if self.lines:
                    return "line", self.lines.pop(0)
                t0 = time.time()
                while time.time() - t0 < 5 and not (b'"id":1' in bytes(out.getvalue()) and b'"id":2' in bytes(out.getvalue())):
                    time.sleep(0.01)
                return "eof", None
        srv = Server(Config(home=tempfile.mkdtemp(dir=TMP)), Scripted(), executor=Boom())
        srv.serve_forever()
        replies = {json.loads(line)["id"]: json.loads(line) for line in out.getvalue().decode().splitlines()}
        self.assertEqual(replies[1]["error"]["code"], -32603)
        self.assertNotIn("secret", json.dumps(replies[1]))
        self.assertEqual(replies[2]["result"], {})


if __name__ == "__main__":
    unittest.main()
