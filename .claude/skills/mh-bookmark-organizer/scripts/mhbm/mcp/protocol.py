"""Protocol constants and small helpers shared by the MCP server modules."""
from __future__ import annotations

# oldest -> newest; the server speaks every revision that uses the ``initialize`` handshake
SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
LATEST = SUPPORTED_VERSIONS[-1]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
RESOURCE_NOT_FOUND = -32002        # resources/read of an unknown URI (2025-06-18 / 2025-11-25)

LOG_LEVELS = ("debug", "info", "notice", "warning", "error", "critical", "alert", "emergency")


class RpcError(Exception):
    """A JSON-RPC protocol error to send back for the current request."""

    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data

    def body(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.data is not None:
            out["data"] = self.data
        return out


def negotiate(requested) -> str:
    """The requested revision when we speak it, otherwise our newest (spec: lifecycle, version negotiation)."""
    return requested if isinstance(requested, str) and requested in SUPPORTED_VERSIONS else LATEST


def at_least(version: str, floor: str) -> bool:
    return SUPPORTED_VERSIONS.index(version) >= SUPPORTED_VERSIONS.index(floor)


def id_key(message_id):
    """Hashable identity for a request id; ``1`` and ``"1"`` are different ids, and ``True`` is not an id at all."""
    return ("i", message_id) if isinstance(message_id, int) else ("s", message_id)


def valid_id(message_id) -> bool:
    return isinstance(message_id, (str, int)) and not isinstance(message_id, bool)


def valid_token(token) -> bool:
    return isinstance(token, (str, int)) and not isinstance(token, bool)
