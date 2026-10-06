"""Outbound MCP (Model Context Protocol) client for the coding harness.

The harness speaks MCP over two transports — stdio (subprocess) and
HTTP+SSE (remote, OAuth-authenticated). Servers are declared in
``.mcp.json``; each server's tools register through the same
``ToolRegistry.dispatch`` path as the built-in tools, so Sentinel review
and the hash-chained audit log apply uniformly regardless of transport.

Public surface:

  * ``MCPClient``        — transport-agnostic JSON-RPC for one MCP server.
  * ``MCPTransport``     — Protocol for stdio / HTTP implementations.
  * ``StdioTransport``   — subprocess + framed JSONL on stdin/stdout.
  * ``HTTPTransport``    — POST + SSE with optional OAuth.
  * ``OAuthSession``     — Auth Code + PKCE + DCR for remote servers.
  * ``MCPTool``          — adapts one MCP-server tool as a harness Tool.
  * ``load_mcp_config``  — read ``.mcp.json`` with ``${ENV}`` expansion.
  * ``build_transport``  — pick the right transport for a server config.
"""
from coding_harness.mcp.client import MCPClient, MCPClientError
from coding_harness.mcp.config import MCPServerConfig, load_mcp_config
from coding_harness.mcp.oauth import (
    OAuthError,
    OAuthRequiresConsent,
    OAuthSession,
)
from coding_harness.mcp.tool_adapter import MCPTool
from coding_harness.mcp.transport import (
    HTTPTransport,
    MCPTransport,
    StdioTransport,
    TransportError,
    build_transport,
)

__all__ = [
    "HTTPTransport",
    "MCPClient",
    "MCPClientError",
    "MCPServerConfig",
    "MCPTool",
    "MCPTransport",
    "OAuthError",
    "OAuthRequiresConsent",
    "OAuthSession",
    "StdioTransport",
    "TransportError",
    "build_transport",
    "load_mcp_config",
]
