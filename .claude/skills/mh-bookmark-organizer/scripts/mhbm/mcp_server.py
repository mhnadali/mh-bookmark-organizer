"""Compatibility shim: the MCP server now lives in :mod:`mhbm.mcp` (see mhbm/mcp/server.py)."""
from .mcp.registry import BY_NAME as TOOLS  # noqa: F401
from .mcp.server import Config, Server, serve  # noqa: F401
