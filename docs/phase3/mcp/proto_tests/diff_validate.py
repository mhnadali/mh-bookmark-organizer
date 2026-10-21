"""Differential test: mhbm.mcp.validate vs the reference `jsonschema` (Draft 2020-12) on random schemas and instances."""
import json, random, sys
sys.path.insert(0, "/tmp/claude-0/-home-user-adding-skills/e94ea44a-2022-55bc-9ae0-034b4c065efe/scratchpad/phase3/mcp-audit/pkg_proto")
from jsonschema import Draft202012Validator
from mhbm.mcp import validate as V

R = random.Random(int(sys.argv[1]) if len(sys.argv) > 1 else 1)
N = int(sys.argv[2]) if len(sys.argv) > 2 else 20000
SCALARS = [None, True, False, 0, 1, -1, 2, 3, 10, 100, 1.0, 2.0, 1.5, -0.5, 99.9, "", "a", "abc", "a-b", "-x", "ABC", "12", "é", "شطرنج", "x" * 12]
PATTERNS = ["^[a-z]+$", "^[^-]", "^\\d+$", "a", "^$"]

def rand_value(depth=0):
    c = R.random()
    if depth >= 2 or c < 0.55:
        return R.choice(SCALARS)
    if c < 0.78:
        return [rand_value(depth + 1) for _ in range(R.randint(0, 4))]
    return {R.choice(["a", "b", "c", "limit", "name"]): rand_value(depth + 1) for _ in range(R.randint(0, 4))}

def rand_schema(depth=0):
    s = {}
    r = R.random()
    if r < 0.8:
        s["type"] = R.choice(V.TYPES) if R.random() < 0.8 else R.sample(V.TYPES, 2)
    if R.random() < 0.15: s["enum"] = [rand_value(1) for _ in range(R.randint(1, 4))]
    if R.random() < 0.08: s["const"] = rand_value(1)
    if R.random() < 0.25:
        for k in R.sample(["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"], R.randint(1, 2)):
            s[k] = R.choice([-1, 0, 1, 2, 10, 1.5, 100])
    if R.random() < 0.25:
        for k in R.sample(["minLength", "maxLength"], R.randint(1, 2)): s[k] = R.randint(0, 5)
    if R.random() < 0.15: s["pattern"] = R.choice(PATTERNS)
    if R.random() < 0.25:
        for k in R.sample(["minItems", "maxItems"], R.randint(1, 2)): s[k] = R.randint(0, 4)
    if R.random() < 0.1: s["uniqueItems"] = R.choice([True, False])
    if depth < 2:
        if R.random() < 0.3: s["items"] = rand_schema(depth + 1)
        if R.random() < 0.4:
            s["properties"] = {n: rand_schema(depth + 1) for n in R.sample(["a", "b", "c", "limit", "name"], R.randint(1, 3))}
            if R.random() < 0.5: s["required"] = R.sample(list(s["properties"]) + ["zz"], R.randint(1, 2))
            s["additionalProperties"] = R.choice([False, True, rand_schema(depth + 1)]) if R.random() < 0.7 else True
        for k in ("anyOf", "oneOf", "allOf"):
            if R.random() < 0.06: s[k] = [rand_schema(depth + 1) for _ in range(R.randint(1, 3))]
        if R.random() < 0.04: s["not"] = rand_schema(depth + 1)
    return s

bad, checked, valid_count = 0, 0, 0
for i in range(N):
    schema = rand_schema()
    V.check_schema(schema, root=False)
    ref = Draft202012Validator(schema)
    for _ in range(8):
        inst = rand_value()
        a = ref.is_valid(inst)
        b = V.is_valid(schema, inst)
        checked += 1; valid_count += a
        if a != b:
            bad += 1
            if bad <= 8:
                print("MISMATCH ref=%s mine=%s\n  schema=%s\n  instance=%s\n  mine errors=%s" % (a, b, json.dumps(schema), json.dumps(inst), V.validate(schema, inst)[:3]))
print(f"seed={sys.argv[1] if len(sys.argv)>1 else 1} schemas={N} checks={checked} valid_share={valid_count/checked:.2f} mismatches={bad}")
sys.exit(1 if bad else 0)
