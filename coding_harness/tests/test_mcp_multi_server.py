"""Tests for concurrent multi-server MCP registration.

Spawns two fake stdio MCP servers — one mimicking a Gmail-shaped surface
(``gmail_search``, ``gmail_send``), one mimicking a Calendar-shaped
surface (``calendar_list_events``, ``calendar_create_event``) — and
verifies that:

  * Both servers register concurrently into one ToolRegistry.
  * Per-server plan-safe declarations from MCPServerConfig propagate
    into ``core.mode`` so reads are visible in Plan and writes are not.
  * Pattern-based plan-safe declarations work alongside literal names.
  * The pre-classified Gmail/Calendar/Drive read names from
    ``core.mode.MCP_PLAN_SAFE_NAMES`` are still treated as plan-safe.
  * Anthropic-managed servers (Gmail/Calendar/Drive) are documented
    as "registration deferred to OAuth-DCR" — the harness can't reach
    them today; this suite proves the *infrastructure* is ready.
"""
from __future__ import annotations

import json
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from coding_harness.core import mode
from coding_harness.mcp.client import MCPClient
from coding_harness.mcp.config import MCPServerConfig, load_mcp_config
from coding_harness.tools.registry import ToolRegistry

GMAIL_SERVER = textwrap.dedent('''
    #!/usr/bin/env python3
    import json, sys
    def send(m):
        sys.stdout.write(json.dumps(m) + "\\n"); sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        req = json.loads(line)
        method = req.get("method"); mid = req.get("id")
        if method == "initialize":
            send({"jsonrpc":"2.0","id":mid,"result":{
                "protocolVersion":"2024-11-05","capabilities":{},
                "serverInfo":{"name":"fake-gmail","version":"0"}}})
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            send({"jsonrpc":"2.0","id":mid,"result":{"tools":[
                {"name":"gmail_search","description":"search","inputSchema":{"type":"object"}},
                {"name":"gmail_send","description":"send","inputSchema":{"type":"object"}},
            ]}})
        elif method == "tools/call":
            params = req.get("params") or {}
            send({"jsonrpc":"2.0","id":mid,"result":{
                "content":[{"type":"text","text":f"gmail:{params.get('name')}"}],
                "isError":False}})
        else:
            send({"jsonrpc":"2.0","id":mid,"error":{"code":-32601,"message":"unknown"}})
''').strip()


CALENDAR_SERVER = textwrap.dedent('''
    #!/usr/bin/env python3
    import json, sys
    def send(m):
        sys.stdout.write(json.dumps(m) + "\\n"); sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        req = json.loads(line)
        method = req.get("method"); mid = req.get("id")
        if method == "initialize":
            send({"jsonrpc":"2.0","id":mid,"result":{
                "protocolVersion":"2024-11-05","capabilities":{},
                "serverInfo":{"name":"fake-cal","version":"0"}}})
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            send({"jsonrpc":"2.0","id":mid,"result":{"tools":[
                {"name":"calendar_list_events","description":"list","inputSchema":{"type":"object"}},
                {"name":"calendar_create_event","description":"create","inputSchema":{"type":"object"}},
            ]}})
        elif method == "tools/call":
            params = req.get("params") or {}
            send({"jsonrpc":"2.0","id":mid,"result":{
                "content":[{"type":"text","text":f"cal:{params.get('name')}"}],
                "isError":False}})
        else:
            send({"jsonrpc":"2.0","id":mid,"error":{"code":-32601,"message":"unknown"}})
''').strip()


def _write_script(dir_path: Path, name: str, body: str) -> Path:
    path = dir_path / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


class TestMultiServerStdio(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = TemporaryDirectory()
        td = Path(self.tmpdir.name)
        self.gmail_script = _write_script(td, "fake_gmail.py", GMAIL_SERVER)
        self.cal_script = _write_script(td, "fake_cal.py", CALENDAR_SERVER)

        self.gmail_cfg = MCPServerConfig(
            name="fake-gmail",
            command=sys.executable,
            args=[str(self.gmail_script)],
            plan_safe_names=["gmail_search"],
        )
        self.cal_cfg = MCPServerConfig(
            name="fake-cal",
            command=sys.executable,
            args=[str(self.cal_script)],
            plan_safe_patterns=["calendar_list_*"],
        )
        self.gmail_client = MCPClient(self.gmail_cfg)
        self.cal_client = MCPClient(self.cal_cfg)

        self.registry = ToolRegistry(session_id="test-mcp-multi")

    def tearDown(self) -> None:
        self.registry.close_mcp_clients()
        self.tmpdir.cleanup()
        mode.reset_for_tests()

    def test_two_servers_register_concurrently(self) -> None:
        gmail_tools = self.registry.register_mcp_server(self.gmail_client)
        cal_tools = self.registry.register_mcp_server(self.cal_client)
        self.assertEqual(set(gmail_tools), {"gmail_search", "gmail_send"})
        self.assertEqual(
            set(cal_tools),
            {"calendar_list_events", "calendar_create_event"},
        )
        # Both clients are alive and distinct.
        self.assertTrue(self.gmail_client.is_alive())
        self.assertTrue(self.cal_client.is_alive())
        self.assertEqual(len(self.registry.mcp_clients), 2)

    def test_per_server_plan_safe_names_propagate(self) -> None:
        self.registry.register_mcp_server(self.gmail_client)
        # Per-server config said gmail_search is plan-safe, gmail_send is not.
        self.assertTrue(mode.is_plan_safe("gmail_search"))
        self.assertFalse(mode.is_plan_safe("gmail_send"))

    def test_per_server_patterns_propagate(self) -> None:
        self.registry.register_mcp_server(self.cal_client)
        # Pattern is calendar_list_*; matches list_events but not create_event.
        self.assertTrue(mode.is_plan_safe("calendar_list_events"))
        self.assertFalse(mode.is_plan_safe("calendar_create_event"))

    def test_round_trip_call_each_server(self) -> None:
        self.registry.register_mcp_server(self.gmail_client)
        self.registry.register_mcp_server(self.cal_client)

        # Patch sentinel to passthrough so dispatch reaches the tool body.
        from unittest import mock

        from coding_harness.security.sentinel import SentinelVerdict

        passthrough = mock.MagicMock(return_value=SentinelVerdict(
            allowed=True, reason="test", path="hook"
        ))
        with mock.patch(
            "coding_harness.tools.registry.sentinel.review", passthrough
        ):
            r1 = self.registry.dispatch("gmail_search", {"q": "x"})
            r2 = self.registry.dispatch("calendar_list_events", {})
        self.assertFalse(r1.is_error, r1.content)
        self.assertFalse(r2.is_error, r2.content)
        self.assertIn("gmail:gmail_search", r1.content)
        self.assertIn("cal:calendar_list_events", r2.content)


class TestPlanModeHidesGmailSend(unittest.TestCase):
    """AC #5: gmail_send unavailable in Plan, available in Act."""

    def setUp(self) -> None:
        self.tmpdir = TemporaryDirectory()
        td = Path(self.tmpdir.name)
        script = _write_script(td, "fake_gmail.py", GMAIL_SERVER)
        cfg = MCPServerConfig(
            name="fake-gmail",
            command=sys.executable,
            args=[str(script)],
            plan_safe_names=["gmail_search"],
        )
        self.client = MCPClient(cfg)
        self.registry = ToolRegistry(session_id="test-mcp-plan-hides-send")
        self.registry.register_mcp_server(self.client)

    def tearDown(self) -> None:
        self.registry.close_mcp_clients()
        self.tmpdir.cleanup()
        mode.reset_for_tests()

    def test_plan_mode_hides_gmail_send(self) -> None:
        self.registry.mode = mode.Mode.PLAN
        names = {t["function"]["name"]
                 for t in self.registry.to_openai_tools()}
        self.assertIn("gmail_search", names)
        self.assertNotIn("gmail_send", names)

    def test_act_mode_shows_gmail_send(self) -> None:
        self.registry.mode = mode.Mode.ACT
        names = {t["function"]["name"]
                 for t in self.registry.to_openai_tools()}
        self.assertIn("gmail_search", names)
        self.assertIn("gmail_send", names)


class TestConfigSchema(unittest.TestCase):
    """Per-server plan_safe_* keys round-trip through .mcp.json."""

    def test_load_with_per_server_plan_safe(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / ".mcp.json"
            path.write_text(json.dumps({"mcpServers": {
                "x": {
                    "command": "/bin/true",
                    "args": [],
                    "plan_safe_names": ["x_get", "x_list"],
                    "plan_safe_patterns": ["x_search_*"],
                }
            }}))
            servers = load_mcp_config(path, environ={})
            self.assertEqual(len(servers), 1)
            s = servers[0]
            self.assertEqual(s.plan_safe_names, ["x_get", "x_list"])
            self.assertEqual(s.plan_safe_patterns, ["x_search_*"])

    def test_invalid_plan_safe_names_raises(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / ".mcp.json"
            path.write_text(json.dumps({"mcpServers": {
                "x": {
                    "command": "/bin/true",
                    "plan_safe_names": [123, "x_list"],
                }
            }}))
            with self.assertRaises(ValueError):
                load_mcp_config(path, environ={})


class TestPreClassifiedRemoteServerNames(unittest.TestCase):
    """The pre-shipped MCP_PLAN_SAFE_NAMES set covers Gmail/Calendar/Drive
    read tools so when remote MCP transport eventually lands, those tools
    land plan-safe with no further wiring.
    """

    def test_gmail_search_plan_safe_out_of_box(self) -> None:
        self.assertTrue(mode.is_plan_safe("gmail_search"))
        self.assertTrue(mode.is_plan_safe("gmail_read_message"))

    def test_gmail_send_act_only_out_of_box(self) -> None:
        self.assertFalse(mode.is_plan_safe("gmail_send"))
        self.assertFalse(mode.is_plan_safe("gmail_send_message"))

    def test_calendar_list_plan_safe_out_of_box(self) -> None:
        self.assertTrue(mode.is_plan_safe("calendar_list_events"))
        self.assertFalse(mode.is_plan_safe("calendar_create_event"))
        self.assertFalse(mode.is_plan_safe("calendar_delete_event"))

    def test_drive_read_plan_safe_out_of_box(self) -> None:
        self.assertTrue(mode.is_plan_safe("drive_read_file_content"))
        self.assertTrue(mode.is_plan_safe("drive_search_files"))
        self.assertFalse(mode.is_plan_safe("drive_create_file"))
        self.assertFalse(mode.is_plan_safe("drive_copy_file"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
