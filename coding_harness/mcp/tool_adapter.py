"""Adapt one MCP-server tool to the harness ``Tool`` interface.

Each ``MCPTool`` instance corresponds to a single tool advertised by a single
``MCPClient``. The adapter:

  * carries the model-facing name, description, and JSON Schema verbatim from
    the server's ``tools/list`` response (so the LLM sees the same shape it
    would see talking to the server directly);
  * routes ``run(args)`` through ``MCPClient.call_tool`` and translates the
    MCP result envelope into a ``ToolResult``;
  * surfaces any Carryall-style ``audit_id`` / ``envelope_id`` from the result
    payload as ``ToolResult.metadata['carryall_audit_id']`` so the registry's
    audit append can cross-reference it.

Translation rules:

  * ``isError=true`` → ``ToolResult(is_error=True)`` with concatenated text.
  * Multiple ``content`` blocks are joined with newlines; non-text blocks are
    described as ``[<type> block]`` placeholders so the model still gets
    something readable.
  * Network/protocol failures from the client (``MCPClientError``) become
    ``is_error=True`` results, never re-raised — the registry firewalls
    exceptions but keeping the dispatch path uniform helps tests.
"""
from __future__ import annotations

import json
from typing import Any

from coding_harness.core.mode import is_plan_safe
from coding_harness.mcp.client import MCPClient, MCPClientError
from coding_harness.tools.base import Tool, ToolResult

# Result-payload keys that, when present, carry a Carryall audit row id we
# want to cross-reference into the harness audit chain. Carryall's MCP
# responses include these on write-class tools today; reads omit them.
CARRYALL_AUDIT_KEYS = ("audit_id", "envelope_id", "audit_trail_id")


class MCPTool(Tool):
    """Wraps one MCP-server tool. Construct via ``MCPTool.from_descriptor``."""

    def __init__(
        self,
        *,
        client: MCPClient,
        name: str,
        description: str,
        parameters: dict[str, Any],
        timeout: float = 30.0,
    ):
        self._client = client
        self.name = name
        self.description = description
        self.parameters = parameters
        self._timeout = timeout
        # Plan-safety is the only read-only signal an MCP server gives us;
        # anything not enumerated as plan-safe is treated as execute.
        self.category = "read" if is_plan_safe(name) else "execute"

    @classmethod
    def from_descriptor(
        cls,
        client: MCPClient,
        descriptor: dict[str, Any],
        *,
        timeout: float = 30.0,
    ) -> MCPTool:
        name = descriptor.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"MCP tool descriptor missing 'name': {descriptor!r}")
        description = descriptor.get("description") or ""
        if not isinstance(description, str):
            description = str(description)
        schema = descriptor.get("inputSchema") or descriptor.get("input_schema") or {}
        if not isinstance(schema, dict):
            raise ValueError(
                f"MCP tool '{name}' inputSchema must be an object"
            )
        # Ensure a JSON-Schema object with a 'type' field — required by
        # OpenAI tool-calling and by Anthropic's tool-use shape.
        if "type" not in schema:
            schema = {"type": "object", **schema}
        return cls(
            client=client,
            name=name,
            description=description,
            parameters=schema,
            timeout=timeout,
        )

    def run(self, args: dict[str, Any]) -> ToolResult:
        try:
            envelope = self._client.call_tool(
                self.name, args, timeout=self._timeout
            )
        except MCPClientError as e:
            return ToolResult(
                content=f"MCP error: {e}",
                is_error=True,
                metadata={"mcp_error": True},
            )

        is_error = bool(envelope.get("isError"))
        content_blocks = envelope.get("content") or []
        text = _flatten_content(content_blocks)
        metadata: dict[str, Any] = {"mcp_server": self._client.config.name}

        # Pull a Carryall audit id out of the structured payload when the
        # server includes one. Two places to look:
        #   1. A top-level structured field on the result envelope, which
        #      Carryall sets for write tools.
        #   2. A JSON object embedded in the first text content block, which
        #      is how some Carryall handlers serialize their reply.
        carryall_audit_id = _extract_carryall_audit_id(envelope, content_blocks)
        if carryall_audit_id:
            metadata["carryall_audit_id"] = carryall_audit_id

        return ToolResult(content=text, is_error=is_error, metadata=metadata)


def _flatten_content(blocks: list[Any]) -> str:
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        block_type = block.get("type", "text")
        if block_type == "text":
            value = block.get("text", "")
            parts.append(value if isinstance(value, str) else str(value))
        else:
            parts.append(f"[{block_type} block]")
    return "\n".join(parts)


def _extract_carryall_audit_id(
    envelope: dict[str, Any], blocks: list[Any]
) -> str | None:
    for key in CARRYALL_AUDIT_KEYS:
        value = envelope.get(key)
        if isinstance(value, (str, int)) and str(value):
            return str(value)
    structured = envelope.get("structuredContent")
    if isinstance(structured, dict):
        for key in CARRYALL_AUDIT_KEYS:
            value = structured.get(key)
            if isinstance(value, (str, int)) and str(value):
                return str(value)
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str) or not text.strip().startswith("{"):
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            for key in CARRYALL_AUDIT_KEYS:
                value = parsed.get(key)
                if isinstance(value, (str, int)) and str(value):
                    return str(value)
    return None
