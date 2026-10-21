"""Turn a finished child process into a ``CallToolResult``: JSON -> structuredContent + text, errors -> ``isError``, hard size caps."""
from __future__ import annotations

import json
import os
import re

MAX_ERROR_CHARS = 4000


def compact(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def parse_json(raw: bytes):
    """Strict parse of the CLI's ``--json`` output; None when it is not a JSON document."""
    def refuse(name):
        raise ValueError(name)
    try:
        return json.loads(raw.decode("utf-8", "replace"), parse_constant=refuse)
    except (ValueError, RecursionError):
        return None


def redact(text: str, home: str) -> str:
    """Do not hand the model absolute paths of the library or the user's home directory."""
    for real, mask in ((os.path.realpath(home), "<library>"), (home, "<library>"), (os.path.expanduser("~"), "~")):
        if real and real not in ("/", "~"):
            text = text.replace(real, mask)
    return text


def redact_obj(obj, home: str):
    """``redact`` for every string inside a JSON value (keys are left alone)."""
    if isinstance(obj, str):
        return redact(obj, home)
    if isinstance(obj, list):
        return [redact_obj(v, home) for v in obj]
    if isinstance(obj, dict):
        return {k: redact_obj(v, home) for k, v in obj.items()}
    return obj


def error_result(message: str, kind: str = "command_failed", retryable: bool = False) -> dict:
    return {"content": [{"type": "text", "text": message[:MAX_ERROR_CHARS]}], "isError": True,
            "_meta": {"io.mhbm/error": {"kind": kind, "retryable": retryable}}}


def _size(node) -> int:
    return len(compact(node))


def shrink(obj, limit: int):
    """Make ``obj`` serialize to at most ``limit`` characters by halving the largest list / dict / string; returns ``(obj, info|None)``.
    The shape of the answer is kept (still valid JSON, same keys); what was dropped is reported in ``info``."""
    full = _size(obj)
    if full <= limit:
        return obj, None
    import copy
    work = copy.deepcopy(obj)
    dropped: list = []
    for _ in range(200):
        if _size(work) <= limit - 400:                       # leave room for the _truncated note itself
            break
        best = _largest(work, "") or _largest(work, "", root_ok=True)     # the top-level keys are only dropped when nothing else can shrink
        if best is None:
            break
        parent, key, path, node = best
        new: object
        if isinstance(node, str):
            new = node[: max(200, len(node) // 2)] + " ...[cut]"
            dropped.append({"path": path or "/", "chars": len(node)})
        elif isinstance(node, list):
            keep = max(1, len(node) // 2) if len(node) > 1 else 0
            new = node[:keep]
            dropped.append({"path": path or "/", "kept": keep, "of": len(node)})
        else:
            keys = list(node)[: max(1, len(node) // 2)] if len(node) > 1 else []
            new = {k: node[k] for k in keys}
            dropped.append({"path": path or "/", "kept": len(new), "of": len(node)})
        if parent is None:
            work = new
        else:
            parent[key] = new
    info = {"originalChars": full, "reduced": dropped[-8:], "hint": "the answer was cut to fit the size limit; ask for fewer rows (limit/ids) or narrow the query"}
    if isinstance(work, dict):
        work["_truncated"] = info
    return work, info


def _largest(node, path, parent=None, key=None, root_ok=False):
    """The (parent, key, path, node) of the biggest reducible container or long string below ``node``."""
    best, best_size = None, 0

    def walk(n, p, par, k):
        nonlocal best, best_size
        if isinstance(n, str) and len(n) > 400:
            size = len(n)
        elif isinstance(n, (list, dict)) and len(n) > 1 and (root_ok or par is not None or not isinstance(n, dict)):
            size = _size(n)
        else:
            size = 0
        if size > best_size:
            best, best_size = (par, k, p, n), size
        if isinstance(n, list):
            for i, v in enumerate(n):
                walk(v, f"{p}/{i}", n, i)
        elif isinstance(n, dict):
            for kk, v in n.items():
                walk(v, f"{p}/{kk}", n, kk)
    walk(node, path, parent, key)
    return best


def ok_result(obj, *, structured: bool, limit: int) -> dict:
    obj, info = shrink(obj, limit)
    text = compact(obj)
    res: dict = {"content": [{"type": "text", "text": text}], "isError": False}
    if structured and isinstance(obj, dict):
        res["structuredContent"] = obj
    if info:
        res["_meta"] = {"io.mhbm/truncated": {"originalChars": info["originalChars"]}}
    return res


def text_result(text: str, limit: int) -> dict:
    res = {"content": [{"type": "text", "text": text[:limit] + (f"\n...[cut {len(text) - limit} characters]" if len(text) > limit else "")}], "isError": False}
    return res


_USAGE = re.compile(r"^(usage:|\s{2,}\S)", re.I)


def clean_stderr(stderr: str, home: str) -> str:
    """The useful last line of a failed command (argparse usage blocks and tracebacks are noise to the model)."""
    lines = [ln.rstrip() for ln in stderr.strip().splitlines() if ln.strip()]
    useful = [ln for ln in lines if not _USAGE.match(ln) and not ln.startswith("Traceback")]
    text = ""
    for ln in reversed(useful):
        if "error:" in ln:
            text = ln.split("error:", 1)[1].strip()
            break
    text = text or (useful[-1] if useful else "the command failed without a message")
    return redact(text, home)


def map_exec(tool, res, *, home: str, limit: int, post=None):
    """``None`` when the call was cancelled (the spec says: send no response), else a CallToolResult dict."""
    if res.cancelled:
        return None
    if res.timed_out:
        return error_result(f"{tool.name} was stopped after {int(res.elapsed)} s (time limit). Nothing is half-applied: changes are transactional. "
                            "Retry with a smaller limit.", "timeout", True)
    if res.overflow:
        return error_result("The command produced more than 16 MiB of output and was stopped; narrow the request.", "output_too_large")
    if res.code in tool.ok_exit:
        obj = parse_json(res.stdout)
        if isinstance(obj, dict):
            obj = redact_obj(obj, home)             # the model gets '<library>', not where the library lives on this machine
            if post:
                try:
                    obj = post(obj)
                except (OSError, ValueError, KeyError, TypeError) as e:       # e.g. the command wrote something other than the file we expected
                    return error_result(f"Internal error in {tool.name} while finishing the result ({type(e).__name__})", "internal")
            return ok_result(obj, structured=tool.output is not None, limit=limit)
        text = res.stdout.decode("utf-8", "replace").strip()
        return text_result(redact(text, home), limit) if text else ok_result({}, structured=False, limit=limit)
    if res.code == 75:
        return error_result("The library is busy: another mhbm process (a scheduled maintenance job, the web UI, the browser extension) is writing "
                            "to it. Nothing was changed. Retry in a few seconds.", "library_busy", True)
    crashed = "Traceback (most recent call last)" in res.stderr or res.code not in (1, 2)
    msg = clean_stderr(res.stderr, home)
    if crashed:
        return error_result(f"Internal error in {tool.name} (exit {res.code}): {msg}", "internal")
    return error_result(msg, "command_failed")
