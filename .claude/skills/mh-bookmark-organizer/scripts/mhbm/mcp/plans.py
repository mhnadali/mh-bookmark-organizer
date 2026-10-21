"""Plan store: the two-phase "propose, then apply exactly that" flow.

A *plan tool* (``bm_organize``, ``bm_dedupe``, ...) asks the CLI to write its plan file (``--plan-out``) into ``<home>/plans/`` and returns
an opaque ``plan_id``.  ``bm_apply_plan(plan_id)`` applies that file and nothing else, only while the library is still at the revision
the plan was computed from, only once, and only within ``TTL``.  The model therefore cannot "apply" something other than what it was
shown, and a stale plan fails loudly instead of half-applying.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path

PLAN_ID = re.compile(r"^p-[0-9a-f]{16}$")
TTL = 24 * 3600


class ToolError(Exception):
    """An error the model can act on; becomes a ``CallToolResult`` with ``isError: true``."""

    def __init__(self, message: str, kind: str = "invalid", retryable: bool = False):
        super().__init__(message)
        self.kind, self.retryable = kind, retryable


class PlanStore:
    def __init__(self, home):
        self.dir = Path(home) / "plans"

    def _ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass

    def new(self):
        self._ensure()
        self.purge()
        pid = "p-" + secrets.token_hex(8)
        return pid, str(self.dir / f"{pid}.json")

    def save_meta(self, pid: str, meta: dict) -> None:
        (self.dir / f"{pid}.meta.json").write_text(json.dumps(dict(meta, created=time.time())), encoding="utf-8")

    def load(self, pid) -> tuple:
        if not isinstance(pid, str) or not PLAN_ID.match(pid):
            raise ToolError("plan_id must look like p-0123456789abcdef (it is returned by the plan tools)")
        meta_file, plan_file = self.dir / f"{pid}.meta.json", self.dir / f"{pid}.json"
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ToolError(f"no such plan: {pid} (it was applied already, expired, or never existed). Run the plan tool again.") from None
        if time.time() - meta.get("created", 0) > TTL or not plan_file.is_file():
            self.drop(pid)
            raise ToolError(f"plan {pid} has expired; run the plan tool again.")
        return str(plan_file), meta

    def drop(self, pid: str) -> None:
        for name in (f"{pid}.json", f"{pid}.meta.json"):
            try:
                (self.dir / name).unlink()
            except OSError:
                pass

    def purge(self) -> None:
        if not self.dir.is_dir():
            return
        now = time.time()
        for f in self.dir.glob("p-*.meta.json"):
            try:
                if now - json.loads(f.read_text(encoding="utf-8")).get("created", 0) > TTL:
                    self.drop(f.name[: -len(".meta.json")])
            except (OSError, ValueError):
                pass
