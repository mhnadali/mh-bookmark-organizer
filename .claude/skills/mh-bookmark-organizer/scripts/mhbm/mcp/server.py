"""The MCP session: lifecycle, dispatch, tool workers, cancellation, progress, logging, roots.

Threading model: the main thread only reads and dispatches (so ``ping``, ``tools/list`` and ``notifications/cancelled`` are answered while a
tool runs); every ``tools/call`` and ``resources/read`` runs on its own worker thread, which owns one child process (see executor.py).
All writes to the wire go through ``Wire.send`` (one lock), so replies from different workers never interleave.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .. import __version__, schema
from . import content, registry, results, validate
from .executor import SubprocessExecutor
from .paths import PathError, PathPolicy, uri_to_path
from .plans import PlanStore, ToolError
from .protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SUPPORTED_VERSIONS,
    RpcError,
    at_least,
    id_key,
    negotiate,
    valid_id,
    valid_token,
)
from .wire import EOF, OVERSIZE, WireClosed

SERVER_NAME = "mh-bookmark-organizer"


@dataclass
class Config:
    home: str
    read_only: bool = False
    allow_dirs: tuple = ()                 # explicit --allow-dir values (win over client roots)
    no_network: bool = False
    disabled: tuple = ()
    max_result_chars: int = 30_000
    max_concurrency: int = 4
    calls_per_minute: int = 120
    timeout_scale: float = 1.0
    use_client_roots: bool = True
    max_line: int = 4 << 20

    def __post_init__(self):
        self.home = os.path.abspath(self.home)      # children run in another directory: every path handed to them must be absolute


class Ctx:
    """What tool builders may use."""

    def __init__(self, cfg: Config, executor, policy: PathPolicy):
        self.home, self.cfg, self.executor, self.policy = cfg.home, cfg, executor, policy
        self.plans = PlanStore(cfg.home)
        self.tmp = Path(cfg.home) / "tmp"

    def revision(self) -> str:
        """A fingerprint of everything a plan can depend on (bookmarks, link checks, archive, undo history): changes when the library does."""
        path = Path(self.home) / "library.db"
        if not path.is_file():
            return "none"
        try:
            db = sqlite3.connect(schema.ro_uri(path), uri=True, timeout=10)
            try:
                return schema.fingerprint(db)
            finally:
                db.close()
        except sqlite3.Error:
            raise ToolError("cannot read the library to check whether it changed (is it from a newer or older mhbm? run `mhbm doctor`)",
                            kind="library_unreadable") from None


@dataclass
class Call:
    request_id: object
    cancel: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    started: float = field(default_factory=time.monotonic)
    token: object = None
    last_progress: float = 0.0
    sent_progress: float = -1.0
    real_progress: bool = False


class Server:
    def __init__(self, cfg: Config, wire, executor=None, tools=None):
        self.cfg, self.wire = cfg, wire
        home = os.path.abspath(cfg.home)
        self.executor = executor or SubprocessExecutor(home, read_only=cfg.read_only, no_network=cfg.no_network)
        base = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        allow = list(cfg.allow_dirs) or [base]
        self.policy = PathPolicy(allow, deny_dirs=[home], base=os.getcwd())
        self.ctx = Ctx(cfg, self.executor, self.policy)
        self.tools = {t.name: t for t in (tools if tools is not None else registry.TOOLS) if self._exposed(t)}
        self.version: str | None = None
        self.client_caps: dict = {}
        self.client_info: dict = {}
        self.initialized = False
        self.inflight: dict = {}
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(max(1, cfg.max_concurrency))
        self.recent_calls: deque = deque()
        self.closing = False
        self.roots_requests: dict = {}
        self.next_server_id = 0
        self.first_line = True

    def _exposed(self, t) -> bool:
        if t.name in self.cfg.disabled:
            return False
        if self.cfg.read_only and not t.read_only:
            return False
        return not (self.cfg.no_network and t.network)

    def since(self, floor: str) -> bool:
        """True when the negotiated protocol revision is ``floor`` or newer."""
        return self.version is not None and at_least(self.version, floor)

    # ================================================================== output helpers
    def send(self, message) -> None:
        try:
            self.wire.send(message)
        except WireClosed:
            self.closing = True

    def reply(self, rid, result=None, error: RpcError | None = None) -> dict:
        if error is not None:
            return {"jsonrpc": "2.0", "id": rid, "error": error.body()}
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def notify(self, method: str, params: dict | None = None, droppable: bool = False) -> None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        try:
            (self.wire.send_droppable if droppable else self.wire.send)(msg)
        except WireClosed:
            self.closing = True

    def log(self, level: str, data) -> None:
        """Diagnostics go to stderr (the logging capability is deprecated as of protocol revision 2026-07-28)."""
        if os.environ.get("MHBM_MCP_DEBUG") or level in ("error", "critical"):
            import sys
            print(f"mhbm-mcp {level}: {json.dumps(data, default=str)}", file=sys.stderr, flush=True)

    # ================================================================== main loop
    def serve_forever(self) -> None:
        try:
            while not self.closing:
                kind, data = self.wire.read()
                if kind == EOF:
                    break
                if kind == OVERSIZE:
                    self.send(self.reply(None, error=RpcError(INVALID_REQUEST, f"message of {data} bytes exceeds the {self.wire.max_line} byte limit")))
                    continue
                self.on_line(data)
        finally:
            self.shutdown()
            self.wire.close()

    def on_line(self, raw: bytes) -> None:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            self.send(self.reply(None, error=RpcError(PARSE_ERROR, "Parse error: the message is not valid UTF-8")))
            return
        if self.first_line:
            self.first_line = False
            text = text.lstrip("﻿")
        text = text.strip()
        if not text:
            return

        def no_constant(name):
            raise ValueError(name)
        try:
            msg = json.loads(text, parse_constant=no_constant)
        except (ValueError, RecursionError):
            self.send(self.reply(None, error=RpcError(PARSE_ERROR, "Parse error")))
            return
        if isinstance(msg, list):
            self.handle_batch(msg)
            return
        resp = self.handle_one(msg)
        if resp is not None:
            self.send(resp)

    def handle_batch(self, msgs: list) -> None:
        if self.version != "2025-03-26":
            self.send(self.reply(None, error=RpcError(INVALID_REQUEST, "JSON-RPC batching is not supported (removed in protocol revision 2025-06-18)")))
            return
        if not msgs:
            self.send(self.reply(None, error=RpcError(INVALID_REQUEST, "Invalid Request: empty batch")))
            return
        out = []
        for m in msgs:
            if isinstance(m, dict) and m.get("method") == "initialize":
                out.append(self.reply(m.get("id") if valid_id(m.get("id")) else None, error=RpcError(INVALID_REQUEST, "initialize must not be part of a batch")))
                continue
            r = self.handle_one(m, sync=True)
            if r is not None:
                out.append(r)
        if out:
            self.send(out)

    # ================================================================== envelope
    def handle_one(self, msg, sync: bool = False):
        if not isinstance(msg, dict):
            return self.reply(None, error=RpcError(INVALID_REQUEST, "Invalid Request"))
        rid = msg.get("id") if valid_id(msg.get("id")) else None
        if msg.get("jsonrpc") != "2.0":
            return self.reply(rid, error=RpcError(INVALID_REQUEST, 'Invalid Request: "jsonrpc" must be "2.0"'))
        if "method" in msg:
            method = msg["method"]
            if not isinstance(method, str):
                return self.reply(rid, error=RpcError(INVALID_REQUEST, "Invalid Request: method must be a string"))
            if "id" in msg:
                if not valid_id(msg["id"]):
                    return self.reply(None, error=RpcError(INVALID_REQUEST, "Invalid Request: id must be a string or an integer"))
                return self.handle_request(msg["id"], method, msg.get("params"), sync)
            self.handle_notification(method, msg.get("params"))
            return None
        if "result" in msg or "error" in msg:
            try:
                self.handle_response(msg)
            except Exception as e:                                  # a malformed answer from the client costs nothing but a log line
                self.log("error", {"msg": "bad response from client", "type": type(e).__name__})
            return None
        return self.reply(rid, error=RpcError(INVALID_REQUEST, "Invalid Request"))

    def handle_request(self, rid, method: str, params, sync: bool):
        handler = self.METHODS.get(method)
        if handler is None:
            return self.reply(rid, error=RpcError(METHOD_NOT_FOUND, f"Method not found: {method}"))
        if params is not None and not isinstance(params, dict):
            return self.reply(rid, error=RpcError(INVALID_PARAMS, "params must be an object"))
        if self.version is None and method not in ("initialize", "ping"):
            return self.reply(rid, error=RpcError(INVALID_REQUEST, "Server not initialized: send initialize first"))
        key = id_key(rid)
        with self.lock:
            if key in self.inflight:
                return self.reply(rid, error=RpcError(INVALID_REQUEST, "A request with this id is already in progress"))
        try:
            result = handler(self, rid, params or {}, sync)
        except RpcError as e:
            return self.reply(rid, error=e)
        except Exception as e:                                   # a bug must cost one request, never the session
            self.log("error", {"msg": "internal error", "method": method, "type": type(e).__name__})
            return self.reply(rid, error=RpcError(INTERNAL_ERROR, "Internal error"))
        if result is DEFERRED:
            return None
        if isinstance(result, _Ready):
            return result.resp
        return self.reply(rid, result)

    # ================================================================== notifications / responses
    def handle_notification(self, method: str, params) -> None:
        params = params if isinstance(params, dict) else {}
        if method == "notifications/initialized":
            if self.version is None:
                return                                           # before initialize: ignore
            self.initialized = True
            if self.client_caps.get("roots") is not None:
                self.request_roots()
        elif method == "notifications/cancelled":
            rid = params.get("requestId")
            if valid_id(rid):
                with self.lock:
                    call = self.inflight.get(id_key(rid))
                if call is not None:
                    call.cancel.set()
                    self.log("info", {"msg": "cancelled by client", "reason": str(params.get("reason", ""))[:200]})
        elif method == "notifications/roots/list_changed":
            if self.initialized and self.client_caps.get("roots") is not None:
                self.request_roots()

    def request_roots(self) -> None:
        if self.cfg.allow_dirs or not self.cfg.use_client_roots:
            return
        self.next_server_id += 1
        sid = f"mhbm-{self.next_server_id}"
        self.roots_requests[sid] = True
        self.send({"jsonrpc": "2.0", "id": sid, "method": "roots/list"})

    def handle_response(self, msg: dict) -> None:
        sid = msg.get("id")
        if not valid_id(sid) or sid not in self.roots_requests:
            return                                               # not ours: ignore, never answer a response (and never trip over a list/object id)
        self.roots_requests.pop(sid, None)
        result = msg.get("result")
        roots = result.get("roots") if isinstance(result, dict) else None
        if not isinstance(roots, list):
            self.log("error", {"msg": "roots/list answered without a list of roots; allowed directories unchanged"})
            return
        paths = []
        for r in roots:
            try:
                path = uri_to_path(r.get("uri", "")) if isinstance(r, dict) else None
            except (ValueError, TypeError, AttributeError):
                path = None
            if path:
                paths.append(path)
        self.policy.set_allow(paths)                             # an empty list is an answer too: the client revoked every root
        self.log("info", {"msg": "allowed directories updated from client roots", "count": len(paths)})

    # ================================================================== methods
    def m_initialize(self, rid, params, sync):
        if self.version is not None:
            raise RpcError(INVALID_REQUEST, "Already initialized")
        pv = params.get("protocolVersion")
        if not isinstance(pv, str):
            raise RpcError(INVALID_PARAMS, "protocolVersion (string) is required", {"supported": list(SUPPORTED_VERSIONS)})
        caps, info = params.get("capabilities", {}), params.get("clientInfo", {})
        if not isinstance(caps, dict) or not isinstance(info, dict):
            raise RpcError(INVALID_PARAMS, "capabilities and clientInfo must be objects")
        self.version = negotiate(pv)
        self.client_caps, self.client_info = caps, info
        server_info = {"name": SERVER_NAME, "version": __version__}
        if self.since("2025-06-18"):
            server_info["title"] = "MH Bookmark Organizer"
        if self.since("2025-11-25"):
            server_info["description"] = "Organize, dedupe, link-check, archive and search your browser bookmarks."
        return {"protocolVersion": self.version, "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": server_info, "instructions": content.instructions(self.tools)}

    def m_ping(self, rid, params, sync):
        return {}

    @staticmethod
    def _check_cursor(params) -> None:
        if "cursor" in params and params["cursor"] is not None:
            raise RpcError(INVALID_PARAMS, "Invalid cursor: this server returns every item in one page")

    def shape_tool(self, t) -> dict:
        d = {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
        if self.since("2025-06-18"):
            d["title"] = t.title
            if t.output:
                d["outputSchema"] = t.output
        if self.since("2025-03-26"):
            d["annotations"] = t.annotations()
        if t.meta:
            d["_meta"] = t.meta
        return d

    def m_tools_list(self, rid, params, sync):
        self._check_cursor(params)
        return {"tools": [self.shape_tool(t) for t in self.tools.values()]}

    def m_resources_list(self, rid, params, sync):
        self._check_cursor(params)
        res = content.resource_list()
        if not self.since("2025-06-18"):
            res = [{k: v for k, v in r.items() if k != "title"} for r in res]
        return {"resources": res}

    def m_templates_list(self, rid, params, sync):
        self._check_cursor(params)
        tpl = content.TEMPLATES
        if not self.since("2025-06-18"):
            tpl = [{k: v for k, v in r.items() if k != "title"} for r in tpl]
        return {"resourceTemplates": tpl}

    def m_prompts_list(self, rid, params, sync):
        self._check_cursor(params)
        pl = content.prompt_list(self.tools)
        if not self.since("2025-06-18"):
            pl = [{k: v for k, v in p.items() if k != "title"} for p in pl]
        return {"prompts": pl}

    def m_prompts_get(self, rid, params, sync):
        return content.get_prompt(params.get("name"), params.get("arguments"), self.tools)

    def m_resources_read(self, rid, params, sync):
        uri = params.get("uri")

        if not self._rate_ok():
            raise RpcError(INTERNAL_ERROR, f"Rate limit exceeded ({self.cfg.calls_per_minute} requests per minute); wait a little and retry")

        def work(call):
            def run_cli(argv):
                if not self._acquire(self.slots, call):         # reading a resource starts a process like a tool call: same cap
                    raise RpcError(INTERNAL_ERROR, "the server is shutting down")
                try:
                    res = self.executor.run(argv, timeout=60, cancel=call.cancel)
                finally:
                    self.slots.release()
                obj = results.parse_json(res.stdout) if res.code == 0 else None
                if not isinstance(obj, dict):
                    raise RpcError(INTERNAL_ERROR, "cannot read the library")
                return obj
            return content.read_resource(uri, self.ctx, run_cli)
        return self.spawn(rid, work, sync, meta=params.get("_meta"))

    def m_tools_call(self, rid, params, sync):
        name = params.get("name")
        if not isinstance(name, str):
            raise RpcError(INVALID_PARAMS, "name (string) is required")
        tool = self.tools.get(name)
        if tool is None:
            raise RpcError(INVALID_PARAMS, f"Unknown tool: {name[:80]}")
        args = params.get("arguments")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise RpcError(INVALID_PARAMS, "arguments must be an object")
        meta = params.get("_meta")
        errors = validate.validate(tool.input_schema, args)
        if errors:
            return results.error_result(f"Invalid arguments for {name}: {validate.describe(errors)}", "invalid_arguments")
        if not self._rate_ok():
            return results.error_result(f"Rate limit exceeded ({self.cfg.calls_per_minute} tool calls per minute); wait a little and retry.", "rate_limited", True)
        args = validate.apply_defaults(tool.input_schema, validate.normalize(tool.input_schema, args))

        def work(call):
            return self.run_tool(call, tool, args)
        return self.spawn(rid, work, sync, meta=meta)

    METHODS = {"initialize": m_initialize, "ping": m_ping, "tools/list": m_tools_list, "tools/call": m_tools_call,
               "resources/list": m_resources_list, "resources/templates/list": m_templates_list, "resources/read": m_resources_read,
               "prompts/list": m_prompts_list, "prompts/get": m_prompts_get}

    def _rate_ok(self) -> bool:
        now = time.monotonic()
        with self.lock:
            while self.recent_calls and now - self.recent_calls[0] > 60:
                self.recent_calls.popleft()
            if len(self.recent_calls) >= self.cfg.calls_per_minute:
                return False
            self.recent_calls.append(now)
        return True

    # ================================================================== workers
    def spawn(self, rid, work, sync: bool, meta=None):
        call = Call(request_id=rid)
        token = meta.get("progressToken") if isinstance(meta, dict) else None
        call.token = token if valid_token(token) else None
        key = id_key(rid)

        def runner(send=True):
            try:
                try:
                    result = work(call)
                    resp = None if call.cancel.is_set() or result is None else self.reply(rid, result)
                except RpcError as e:
                    resp = None if call.cancel.is_set() else self.reply(rid, error=e)
                except Exception as e:
                    self.log("error", {"msg": "worker failed", "type": type(e).__name__})
                    resp = None if call.cancel.is_set() else self.reply(rid, error=RpcError(INTERNAL_ERROR, "Internal error"))
            finally:
                with self.lock:
                    self.inflight.pop(key, None)
            if resp is not None and send:
                self.send(resp)
            return resp

        with self.lock:
            self.inflight[key] = call
        if sync:
            return _Ready(runner(send=False))
        call.thread = threading.Thread(target=runner, name=f"call-{rid}", daemon=True)
        call.thread.start()
        return DEFERRED

    def _acquire(self, lock, call) -> bool:
        while not lock.acquire(timeout=0.1):
            if call.cancel.is_set() or self.closing:
                return False
        return True

    def run_tool(self, call: Call, tool, args: dict):
        prepared = None
        ok = False
        slot = held_write = False
        done = threading.Event()
        try:
            # slot and write lock FIRST: building a call copies files and reads the library revision, and neither may happen for a call
            # that is only queued (disk), nor before the writes ahead of it are finished (a plan must be judged against the library it will meet)
            if not self._acquire(self.slots, call):
                return None
            slot = True
            if tool.writes:
                if not self._acquire(self.write_lock, call):
                    return None
                held_write = True
            try:
                prepared = tool.build(args, self.ctx)
                if prepared.precheck:
                    prepared.precheck()
            except (ToolError, PathError) as e:
                return results.error_result(str(e), getattr(e, "kind", "invalid"), getattr(e, "retryable", False))
            beat = None
            if call.token is not None:
                beat = threading.Thread(target=self._heartbeat, args=(call, done), daemon=True)
                beat.start()
            try:
                res = self.executor.run(prepared.argv, timeout=tool.timeout * self.cfg.timeout_scale, cancel=call.cancel,
                                        progress=(lambda i, n, label: self.progress(call, i, n, label)) if call.token is not None else None)
            except (ValueError, UnicodeError, OSError) as e:        # e.g. an argument the operating system cannot pass to a program
                return results.error_result(f"{tool.name} could not be started ({type(e).__name__}); check the arguments", "invalid")
            result = results.map_exec(tool, res, home=self.cfg.home, limit=self.cfg.max_result_chars, post=prepared.post)
            ok = result is not None and not result.get("isError")
            return result
        finally:
            done.set()
            if held_write:
                self.write_lock.release()
            if slot:
                self.slots.release()
            if prepared is not None:
                for fn in prepared.cleanup + ([] if ok else prepared.on_fail):
                    try:
                        fn()
                    except Exception:
                        pass

    # ----------------------------------------------------------------- progress
    def progress(self, call: Call, i: float, n: float | None, message: str) -> None:
        if call.token is None or self.closing:
            return
        now = time.monotonic()
        if i <= call.sent_progress or (now - call.last_progress < 0.5 and not (n and i >= n)):
            return
        call.real_progress = True
        call.sent_progress, call.last_progress = i, now
        params = {"progressToken": call.token, "progress": i, "message": message[:200]}
        if n:
            params["total"] = n
        self.notify("notifications/progress", params, droppable=True)

    def _heartbeat(self, call: Call, done: threading.Event) -> None:
        """Tools that print no progress still tell the client they are alive every 5 s (keeps idle watchdogs and UIs honest)."""
        while not done.wait(5.0):
            if call.real_progress or call.cancel.is_set():
                continue
            elapsed = int(time.monotonic() - call.started)
            call.sent_progress, call.last_progress = float(elapsed), time.monotonic()
            self.notify("notifications/progress", {"progressToken": call.token, "progress": float(elapsed), "message": f"still working ({elapsed} s)"}, droppable=True)

    # ----------------------------------------------------------------- shutdown
    def shutdown(self) -> None:
        self.closing = True
        with self.lock:
            calls = list(self.inflight.values())
        for c in calls:
            c.cancel.set()
        for c in calls:
            if c.thread is not None:
                c.thread.join(6.0)


DEFERRED = object()


class _Ready:
    """A response already produced synchronously (used for batches); ``resp`` is None when the call was cancelled."""

    def __init__(self, resp):
        self.resp = resp


def serve(home: str, *, read_only: bool = False, allow_dirs=None, **options) -> None:
    """Entry point used by ``mhbm mcp``."""
    import signal

    from .wire import Wire
    registry.selfcheck()
    unknown = sorted(set(options.get("disabled") or ()) - set(registry.BY_NAME))
    if unknown:                                         # a typo must not leave a tool exposed that the operator meant to hide
        import difflib
        hints = "; ".join(f"{n!r}" + (f" (did you mean {difflib.get_close_matches(n, list(registry.BY_NAME), 1)[0]!r}?)" if difflib.get_close_matches(n, list(registry.BY_NAME), 1) else "")
                          for n in unknown)
        raise SystemExit(f"mhbm-mcp: unknown tool name(s) to disable: {hints}")
    cfg = Config(home=home, read_only=read_only, allow_dirs=tuple(allow_dirs or ()), **options)
    wire = Wire(max_line=cfg.max_line)
    server = Server(cfg, wire)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(0)))
    if not cfg.read_only:
        Path(home).mkdir(parents=True, exist_ok=True)
    server.serve_forever()
