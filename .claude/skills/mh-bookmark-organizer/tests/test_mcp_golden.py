"""Conformance-style transcript test: replay a scripted client session and compare every reply with a stored one.

    UPDATE_GOLDEN=1 python -m unittest discover -s tests -p test_mcp_golden.py     # (re)write fixtures/mcp-golden/*.jsonl

Only values that legitimately vary are normalised (server version, random plan ids); everything else must match exactly.
A change to a tool description, schema or message shows up here as a diff: review it, then regenerate.
"""
import json
import os
import pathlib
import re
import unittest

import common  # noqa: F401  (puts scripts/ on sys.path, sets MHBM_LANG)
from mcpdrive import Server, new_home

GOLDEN = pathlib.Path(__file__).resolve().parent / "fixtures" / "mcp-golden"

SESSION = [  # (label, request) - ids are fixed so replies are comparable
    ("ping", {"jsonrpc": "2.0", "id": 1, "method": "ping"}),
    ("unknown method", {"jsonrpc": "2.0", "id": 2, "method": "nope/nope"}),
    ("tools/list", {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}),
    ("stats", {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "bm_stats", "arguments": {}}}),
    ("search", {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "bm_search", "arguments": {"query": "chisel", "limit": 2}}}),
    ("invalid arguments", {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "bm_search", "arguments": {"query": 123, "limit": 1000, "x": 1}}}),
    ("string boolean is not a boolean", {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "bm_dedupe", "arguments": {"apply": "false"}}}),
    ("unknown tool", {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "bm_nope", "arguments": {}}}),
    ("bad id", {"jsonrpc": "2.0", "id": None, "method": "ping"}),
    ("params not object", {"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": [1]}),
    ("bad cursor", {"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": {"cursor": "x"}}),
    ("prompts/list", {"jsonrpc": "2.0", "id": 11, "method": "prompts/list"}),
    ("prompts/get", {"jsonrpc": "2.0", "id": 12, "method": "prompts/get", "params": {"name": "find-bookmark", "arguments": {"topic": "chisel"}}}),
    ("prompts/get missing arg", {"jsonrpc": "2.0", "id": 13, "method": "prompts/get", "params": {"name": "find-bookmark"}}),
    ("resources/list", {"jsonrpc": "2.0", "id": 14, "method": "resources/list"}),
    ("resources/templates/list", {"jsonrpc": "2.0", "id": 15, "method": "resources/templates/list"}),
    ("resources/read", {"jsonrpc": "2.0", "id": 16, "method": "resources/read", "params": {"uri": "mhbm://library/summary"}}),
    ("resources/read unknown", {"jsonrpc": "2.0", "id": 17, "method": "resources/read", "params": {"uri": "mhbm://nope"}}),
    ("logging/setLevel (not offered)", {"jsonrpc": "2.0", "id": 18, "method": "logging/setLevel", "params": {"level": "info"}}),
]
RAW = [("malformed json", "{"), ("non-object json", "[1,2]"), ("null", "null")]


def normalise(obj):
    text = json.dumps(obj, sort_keys=True)
    text = re.sub(r'"version": "\d+\.\d+\.\d+"', '"version": "X.Y.Z"', text)
    text = re.sub(r"p-[0-9a-f]{16}", "p-PLANID", text)
    return json.loads(text)


def run_session(version):
    s = Server(new_home())
    out = []
    try:
        init = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "golden", "version": "1"}}}
        s.send(init)
        out.append(("initialize", init, s.recv_json(20)))
        s.notify("notifications/initialized")
        for label, req in SESSION:
            s.send(req)
            out.append((label, req, s.recv_json(60)))
        for label, line in RAW:
            s.send_line(line)
            out.append((label, line, s.recv_json(20)))
    finally:
        s.close()
    return out


class Golden(unittest.TestCase):
    def test_transcripts(self):
        for version in ("2025-11-25", "2025-06-18", "2025-03-26"):
            session = run_session(version)
            lines = [json.dumps({"label": lab, "request": req, "response": normalise(resp)}, sort_keys=True, ensure_ascii=True) for lab, req, resp in session]
            path = GOLDEN / f"session-{version}.jsonl"
            if os.environ.get("UPDATE_GOLDEN"):
                GOLDEN.mkdir(exist_ok=True)
                path.write_text("\n".join(lines) + "\n")
                continue
            expected = path.read_text().splitlines()
            self.assertEqual(len(expected), len(lines), version)
            for exp, got in zip(expected, lines):
                self.assertEqual(json.loads(exp), json.loads(got), f"{version}: {json.loads(exp)['label']}")


if __name__ == "__main__":
    unittest.main()
