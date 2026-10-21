"""Differential test: every tool schema in tools.json against the real JSON Schema draft 2020-12 meta-schema (jsonschema 4.26),
and the reference validator (mcp_core.validate) against jsonschema on valid and mutated inputs.

Run with a Python that has jsonschema:  dl/venv_js/env/bin/python reference/schema_diff_test.py
"""
import copy
import json
import os
import random
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import jsonschema  # noqa: E402
from jsonschema import Draft202012Validator  # noqa: E402

import mcp_core as core  # noqa: E402

doc = json.load(open(os.path.join(HERE, "..", "tools.json"), encoding="utf-8"))
bad = 0
n_meta = n_cmp = n_dis = 0
for t in doc["tools"]:
    for key in ("inputSchema", "outputSchema"):
        try:
            Draft202012Validator.check_schema(t[key])
            n_meta += 1
        except jsonschema.SchemaError as e:
            bad += 1
            print("META-SCHEMA FAIL", t["name"], key, e.message[:200])
    for name in t["inputSchema"]["properties"]:
        if not (1 <= len(name) <= 64) or not all(c.isalnum() and c.isascii() or c in "_.-" for c in name):
            bad += 1
            print("PROPERTY NAME FAIL", t["name"], name)

rnd = random.Random(1)
SAMPLES = ["", "x", "-x", "a b", "a\x00b", "../etc", "pv_0123456789abcdef", "0123456789ab", "a" * 400, "2024-01-01", "ok", "dead", "html", "A/B ", "a,b", 'a"b']


def mutate(schema, val):
    """A few schema-aware mutations of a valid value."""
    out = [copy.deepcopy(val)]
    if isinstance(val, dict):
        for k in list(val):
            v = copy.deepcopy(val)
            v.pop(k)
            out.append(v)
            v = copy.deepcopy(val)
            v[k] = rnd.choice(SAMPLES + [None, 5, -1, True, [], {}])
            out.append(v)
        v = copy.deepcopy(val)
        v["bogus"] = 1
        out.append(v)
    return out


for t in doc["tools"]:
    s = t["inputSchema"]
    jv = Draft202012Validator(s)
    base_args = [{}]
    for ex in t["examples"]:
        if isinstance(ex.get("arguments"), dict):
            base_args.append(ex["arguments"])
    for base in base_args:
        for cand in mutate(s, base):
            mine = not core.validate(s, cand)
            theirs = jv.is_valid(cand)
            n_cmp += 1
            if mine != theirs:
                n_dis += 1
                print("DISAGREE", t["name"], json.dumps(cand, ensure_ascii=False)[:160], "mine=", mine, "jsonschema=", theirs,
                      [e.message[:90] for e in jv.iter_errors(cand)][:2], core.validate(s, cand)[:2])
print(f"meta-schema checks: {n_meta}; differential comparisons: {n_cmp}; disagreements: {n_dis}; other failures: {bad}")
sys.exit(1 if (bad or n_dis) else 0)
