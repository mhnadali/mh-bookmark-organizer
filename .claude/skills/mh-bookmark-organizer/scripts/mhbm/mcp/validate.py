"""A small, strict, standard-library JSON Schema validator (draft 2020-12 subset) for MCP tool arguments.

Only the keywords listed in ``VALIDATION_KEYWORDS`` are enforced; annotation keywords are ignored; anything else in a schema is a
programming error that ``check_schema`` reports (the tool registry runs it at start-up, so a typo can never silently disable a rule).

No coercion is ever done: ``"5"`` is not an integer and ``"false"`` is not a boolean (a string that is truthy in Python would
otherwise turn a dry run into a write).  ``bool`` is neither ``integer`` nor ``number``.  ``1.0`` is an ``integer`` (as in the
spec: a JSON number with no fractional part) and is converted to ``int`` by ``normalize``.
"""
from __future__ import annotations

import copy
import math
import re

VALIDATION_KEYWORDS = frozenset({
    "type", "enum", "const", "properties", "required", "additionalProperties", "items", "minItems", "maxItems", "uniqueItems",
    "minLength", "maxLength", "pattern", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "anyOf", "oneOf", "allOf", "not"})
ANNOTATION_KEYWORDS = frozenset({"$schema", "$id", "$comment", "title", "description", "default", "examples", "deprecated", "format"})
TYPES = ("null", "boolean", "integer", "number", "string", "array", "object")
MAX_DEPTH = 32
MAX_ERRORS = 20


class SchemaError(ValueError):
    """The schema itself is outside the supported subset."""


def check_schema(schema, path: str = "#", root: bool = True) -> None:
    """Raise ``SchemaError`` when ``schema`` uses anything this validator does not enforce."""
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        raise SchemaError(f"{path}: a schema must be an object or boolean")
    _check_keywords(schema, path, root)
    for name, sub in (schema.get("properties") or {}).items():
        check_schema(sub, f"{path}/properties/{name}", False)
    for k in ("items", "not") + (("additionalProperties",) if isinstance(schema.get("additionalProperties"), dict) else ()):
        if k in schema:
            check_schema(schema[k], f"{path}/{k}", False)
    for k in ("anyOf", "oneOf", "allOf"):
        for i, sub in enumerate(schema.get(k) or []):
            check_schema(sub, f"{path}/{k}/{i}", False)


def _check_keywords(schema: dict, path: str, root: bool) -> None:
    for key in schema:
        if key not in VALIDATION_KEYWORDS and key not in ANNOTATION_KEYWORDS:
            raise SchemaError(f"{path}: unsupported keyword {key!r}")
    t = schema.get("type")
    for one in ([t] if isinstance(t, str) else t or []):
        if one not in TYPES:
            raise SchemaError(f"{path}: unknown type {one!r}")
    if root and ({"anyOf", "oneOf", "allOf", "not"} & schema.keys()):
        raise SchemaError(f"{path}: combinators are not allowed at the schema root (Claude API restriction)")
    if "pattern" in schema:
        try:
            re.compile(schema["pattern"])
        except re.error as e:
            raise SchemaError(f"{path}: bad pattern: {e}") from e


def _type_ok(value, t: str) -> bool:
    if t == "null":
        return value is None
    if t == "boolean":
        return isinstance(value, bool)
    if t == "integer":
        return (isinstance(value, int) and not isinstance(value, bool)) or (isinstance(value, float) and math.isfinite(value) and value.is_integer())
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and (not isinstance(value, float) or math.isfinite(value))
    if t == "string":
        return isinstance(value, str)
    if t == "array":
        return isinstance(value, list)
    if t == "object":
        return isinstance(value, dict)
    return False


def _kind(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    for typ, name in ((str, "string"), (list, "array"), (dict, "object")):
        if isinstance(value, typ):
            return name
    return type(value).__name__


def equal(a, b) -> bool:
    """JSON equality: ``true != 1``, ``1 == 1.0``, deep for containers."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(equal(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


def _show(value) -> str:
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


class _Run:
    def __init__(self):
        self.errors: list = []

    def add(self, path: str, message: str) -> None:
        if len(self.errors) < MAX_ERRORS:
            self.errors.append((path or "/", message))


def validate(schema, instance) -> list:
    """Return ``[(json_pointer, message), ...]`` (empty = valid)."""
    run = _Run()
    _check(schema, instance, "", run, 0)
    return run.errors


def is_valid(schema, instance) -> bool:
    return not validate(schema, instance)


def _check(schema, value, path: str, run: _Run, depth: int) -> bool:
    """Validate; return True when this (sub)schema accepted the value."""
    before = len(run.errors)
    if depth > MAX_DEPTH:
        run.add(path, "nested too deeply")
        return False
    if schema is True:
        return True
    if schema is False:
        run.add(path, "no value is allowed here")
        return False
    types = schema.get("type")
    if types is not None:
        wanted = [types] if isinstance(types, str) else list(types)
        if not any(_type_ok(value, t) for t in wanted):
            run.add(path, f"must be {' or '.join(wanted)} (got {_kind(value)})")
            return False                        # the keywords below only make sense for the right type
    _values(schema, value, path, run)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        _numbers(schema, value, path, run)
    elif isinstance(value, str):
        _strings(schema, value, path, run)
    elif isinstance(value, list):
        _arrays(schema, value, path, run, depth)
    elif isinstance(value, dict):
        _objects(schema, value, path, run, depth)
    _combinators(schema, value, path, run, depth)
    return len(run.errors) == before


def _values(schema, value, path, run) -> None:
    if "enum" in schema and not any(equal(value, e) for e in schema["enum"]):
        run.add(path, "must be one of " + ", ".join(_show(e) for e in schema["enum"]) + f" (got {_show(value)})")
    if "const" in schema and not equal(value, schema["const"]):
        run.add(path, f"must equal {_show(schema['const'])} (got {_show(value)})")


def _combinators(schema, value, path, run, depth) -> None:
    for sub in schema.get("allOf", ()):
        _check(sub, value, path, run, depth + 1)
    if "anyOf" in schema and not _any_valid(schema["anyOf"], value, depth):
        run.add(path, "does not match any of the allowed shapes")
    if "oneOf" in schema:
        n = sum(1 for sub in schema["oneOf"] if _any_valid([sub], value, depth))
        if n != 1:
            run.add(path, f"must match exactly one of the allowed shapes (matches {n})")
    if "not" in schema and _any_valid([schema["not"]], value, depth):
        run.add(path, "matches a shape that is not allowed")


def _any_valid(subs, value, depth) -> bool:
    for sub in subs:
        probe = _Run()
        if _check(sub, value, "", probe, depth + 1):
            return True
    return False


def _numbers(schema, value, path, run) -> None:
    if "minimum" in schema and value < schema["minimum"]:
        run.add(path, f"must be >= {schema['minimum']} (got {_show(value)})")
    if "maximum" in schema and value > schema["maximum"]:
        run.add(path, f"must be <= {schema['maximum']} (got {_show(value)})")
    if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
        run.add(path, f"must be > {schema['exclusiveMinimum']} (got {_show(value)})")
    if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
        run.add(path, f"must be < {schema['exclusiveMaximum']} (got {_show(value)})")


def _strings(schema, value, path, run) -> None:
    n = len(value)
    if "\x00" in value:
        run.add(path, "must not contain a NUL character")
    else:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            run.add(path, "must not contain an unpaired surrogate (not valid text)")
    if "minLength" in schema and n < schema["minLength"]:
        run.add(path, f"must have at least {schema['minLength']} character(s) (has {n})")
    if "maxLength" in schema and n > schema["maxLength"]:
        run.add(path, f"must have at most {schema['maxLength']} characters (has {n})")
    if "pattern" in schema and re.search(schema["pattern"], value) is None:
        run.add(path, f"must match the pattern {schema['pattern']!r}")


def _arrays(schema, value, path, run, depth) -> None:
    n = len(value)
    if "minItems" in schema and n < schema["minItems"]:
        run.add(path, f"must have at least {schema['minItems']} item(s) (has {n})")
    if "maxItems" in schema and n > schema["maxItems"]:
        run.add(path, f"must have at most {schema['maxItems']} items (has {n})")
    if schema.get("uniqueItems"):
        for i in range(n):
            if any(equal(value[i], value[j]) for j in range(i)):
                run.add(path, "items must be unique")
                break
    if "items" in schema:
        for i, item in enumerate(value):
            _check(schema["items"], item, f"{path}/{i}", run, depth + 1)


def _objects(schema, value, path, run, depth) -> None:
    props = schema.get("properties") or {}
    for name in schema.get("required", ()):
        if name not in value:
            run.add(f"{path}/{name}", "is required")
    for name, sub in props.items():
        if name in value:
            _check(sub, value[name], f"{path}/{name}", run, depth + 1)
    extra = schema.get("additionalProperties", True)
    if extra is not True:
        for name in value:
            if name in props:
                continue
            if extra is False:
                run.add(f"{path}/{name}", "is not a known property (allowed: " + (", ".join(props) or "none") + ")")
            else:
                _check(extra, value[name], f"{path}/{name}", run, depth + 1)


def normalize(schema, value):
    """Return ``value`` with integral floats turned into ints where the schema says ``integer`` (after validation succeeded)."""
    if not isinstance(schema, dict):
        return value
    t = schema.get("type")
    wanted = [t] if isinstance(t, str) else list(t or [])
    if isinstance(value, float) and "integer" in wanted and "number" not in wanted and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        return {k: normalize(props.get(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(schema.get("items"), v) for v in value]
    return value


def apply_defaults(schema, args: dict) -> dict:
    """``args`` plus the ``default`` of every top-level property the caller left out - the schema promises these values, so they are applied
    (a tool that was advertised as 'default 50' must not run unbounded). Call after validation."""
    out = dict(args)
    for name, sub in ((schema.get("properties") or {}) if isinstance(schema, dict) else {}).items():
        if name not in out and isinstance(sub, dict) and "default" in sub:
            out[name] = copy.deepcopy(sub["default"])
    return out


def describe(errors) -> str:
    """One compact, model-readable sentence for a list of ``(pointer, message)`` pairs."""
    return "; ".join(f"{p}: {m}" if p != "/" else m for p, m in errors)
