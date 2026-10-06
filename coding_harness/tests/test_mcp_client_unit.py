"""Unit tests for the MCP stdio client + tool adapter.

The fake server is a tiny Python script that speaks JSON-RPC 2.0 over stdio.
It supports just enough of MCP for the harness:

  * ``initialize`` → returns a server stub
  * ``tools/list`` → returns one ``echo`` tool and one ``carryall_write_document``
                     tool whose response embeds an ``audit_id``
  * ``tools/call`` → echoes args back as a text content block, except for
                     ``carryall_write_document`` which returns a structured
                     reply with an ``audit_id``
  * ``boom`` (test-only method) → raises a JSON-RPC error
  * ``crash`` (test-only) → server exits the process

Running it as a subprocess gives us a faithful round-trip including framing.
"""
from __future__ import annotations

import json
import sys
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from coding_harness.mcp.client import MCPClient, MCPClientError
from coding_harness.mcp.config import MCPServerConfig, load_mcp_config
from coding_harness.mcp.tool_adapter import MCPTool

FAKE_SERVER = textwrap.dedent('''
    #!/usr/bin/env python3
    """Fake stdio MCP server for harness unit tests."""
    import json, sys

    def send(msg):
        sys.stdout.write(json.dumps(msg) + "\\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        method = req.get("method")
        msg_id = req.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "fake", "version": "0"},
            }})
        elif method == "notifications/initialized":
            pass  # no response
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [
                {"name": "echo", "description": "echo args",
                 "inputSchema": {"type": "object", "properties": {
                     "text": {"type": "string"}}}},
                {"name": "carryall_write_document",
                 "description": "fake carryall write",
                 "inputSchema": {"type": "object"}},
            ]}})
        elif method == "tools/call":
            params = req.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo":
                send({"jsonrpc": "2.0", "id": msg_id, "result": {
                    "content": [{"type": "text",
                                 "text": "echo:" + json.dumps(args)}],
                    "isError": False,
                }})
            elif name == "carryall_write_document":
                payload = json.dumps({"audit_id": "fake-audit-1234",
                                      "ok": True})
                send({"jsonrpc": "2.0", "id": msg_id, "result": {
                    "content": [{"type": "text", "text": payload}],
                    "isError": False,
                }})
            elif name == "broken":
                send({"jsonrpc": "2.0", "id": msg_id, "error": {
                    "code": -32000, "message": "boom"}})
            else:
                send({"jsonrpc": "2.0", "id": msg_id, "error": {
                    "code": -32601, "message": "unknown tool"}})
        elif method == "crash":
            sys.exit(7)
        else:
            send({"jsonrpc": "2.0", "id": msg_id, "error": {
                "code": -32601, "message": "method not found"}})
''').strip()


def _write_fake_server(tmp_path: Path) -> Path:
    script = tmp_path / "fake_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    script.chmod(0o755)
    return script


def _config_for(script: Path) -> MCPServerConfig:
    return MCPServerConfig(
        name="fake",
        command=sys.executable,
        args=[str(script)],
        cwd=None,
        env={},
    )


class TestConfigLoader(unittest.TestCase):
    def test_loads_and_expands_env(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as td:
            cfg_path = Path(td) / ".mcp.json"
            cfg_path.write_text(json.dumps({"mcpServers": {
                "x": {
                    "command": "${HOME_OVERRIDE}/bin/x",
                    "args": ["--flag", "${VAL}"],
                    "cwd": "${HOME_OVERRIDE}",
                    "env": {"K": "${VAL}"},
                }
            }}))
            servers = load_mcp_config(
                cfg_path, environ={"HOME_OVERRIDE": "/tmp", "VAL": "v"}
            )
            self.assertEqual(len(servers), 1)
            self.assertEqual(servers[0].name, "x")
            self.assertEqual(servers[0].command, "/tmp/bin/x")
            self.assertEqual(servers[0].args, ["--flag", "v"])
            self.assertEqual(servers[0].cwd, "/tmp")
            self.assertEqual(servers[0].env, {"K": "v"})

    def test_missing_command_raises(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as td:
            cfg_path = Path(td) / ".mcp.json"
            cfg_path.write_text(json.dumps({"mcpServers": {"x": {}}}))
            with self.assertRaises(ValueError):
                load_mcp_config(cfg_path, environ={})

    def test_no_config_file_anywhere_returns_empty(self) -> None:
        # When no path is passed and discovery walks up without finding a
        # .mcp.json, the loader returns []. We force discovery to fail by
        # asking it to start from a deep tmp path with no .mcp.json above it.
        from tempfile import TemporaryDirectory

        from coding_harness.mcp.config import find_config
        with TemporaryDirectory() as td:
            self.assertIsNone(find_config(Path(td)))

    def test_explicit_missing_path_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_mcp_config(Path("/nonexistent/.mcp.json"), environ={})


class TestStdioRoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        from tempfile import TemporaryDirectory
        self.tmpdir = TemporaryDirectory()
        self.script = _write_fake_server(Path(self.tmpdir.name))
        self.client = MCPClient(_config_for(self.script))
        self.client.start()

    def tearDown(self) -> None:
        self.client.close()
        self.tmpdir.cleanup()

    def test_list_tools_returns_two(self) -> None:
        tools = self.client.list_tools()
        names = sorted(t["name"] for t in tools)
        self.assertEqual(names, ["carryall_write_document", "echo"])

    def test_call_tool_echo(self) -> None:
        result = self.client.call_tool("echo", {"text": "hi"})
        self.assertFalse(result.get("isError"))
        text = result["content"][0]["text"]
        self.assertIn('"text": "hi"', text)

    def test_call_tool_unknown_raises(self) -> None:
        with self.assertRaises(MCPClientError):
            self.client.call_tool("nope", {})

    def test_call_tool_explicit_error(self) -> None:
        with self.assertRaises(MCPClientError) as ctx:
            self.client.call_tool("broken", {})
        self.assertIn("boom", str(ctx.exception))

    def test_close_is_idempotent(self) -> None:
        self.client.close()
        self.client.close()  # second call must not raise


class TestMCPToolAdapter(unittest.TestCase):
    def setUp(self) -> None:
        from tempfile import TemporaryDirectory
        self.tmpdir = TemporaryDirectory()
        self.script = _write_fake_server(Path(self.tmpdir.name))
        self.client = MCPClient(_config_for(self.script))
        self.client.start()

    def tearDown(self) -> None:
        self.client.close()
        self.tmpdir.cleanup()

    def test_adapter_routes_through_client(self) -> None:
        descriptors = self.client.list_tools()
        echo_desc = next(d for d in descriptors if d["name"] == "echo")
        tool = MCPTool.from_descriptor(self.client, echo_desc)
        result = tool.run({"text": "ping"})
        self.assertFalse(result.is_error)
        self.assertIn("ping", result.content)
        self.assertEqual(result.metadata["mcp_server"], "fake")

    def test_adapter_extracts_carryall_audit_id(self) -> None:
        descriptors = self.client.list_tools()
        write_desc = next(
            d for d in descriptors if d["name"] == "carryall_write_document"
        )
        tool = MCPTool.from_descriptor(self.client, write_desc)
        result = tool.run({"foo": "bar"})
        self.assertEqual(result.metadata.get("carryall_audit_id"),
                         "fake-audit-1234")

    def test_adapter_surfaces_mcp_error_as_tool_result(self) -> None:
        # Build a descriptor for a tool that the server will error on. The
        # adapter must return is_error=True instead of raising.
        tool = MCPTool(
            client=self.client,
            name="broken",
            description="raises",
            parameters={"type": "object"},
        )
        result = tool.run({})
        self.assertTrue(result.is_error)
        self.assertIn("MCP error", result.content)


class TestSubprocessDeath(unittest.TestCase):
    def test_subprocess_exit_unblocks_callers(self) -> None:
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as td:
            script = _write_fake_server(Path(td))
            client = MCPClient(_config_for(script))
            client.start()
            try:
                # Tell the server to exit, then try a call. The reader thread
                # should observe stdout closing and unblock the future.
                client._notify("crash", {})
                # Give the OS a moment to deliver the exit.
                deadline = time.time() + 2.0
                while client.is_alive() and time.time() < deadline:
                    time.sleep(0.05)
                with self.assertRaises(MCPClientError):
                    client.call_tool("echo", {"text": "x"}, timeout=2.0)
            finally:
                client.close()


class TestMCPToolCategory(unittest.TestCase):
    def test_category_follows_plan_safety(self) -> None:
        def make(name: str) -> MCPTool:
            return MCPTool(
                client=mock.MagicMock(), name=name, description="",
                parameters={"type": "object"},
            )

        self.assertEqual(make("carryall_list_vaults").category, "read")
        self.assertEqual(make("carryall_write_document").category, "execute")
        self.assertEqual(make("never_declared_tool").category, "execute")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
