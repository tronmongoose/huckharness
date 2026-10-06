"""The bundled subprocess gate and the in-process policy carry one denied-command list."""
from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest

from coding_harness.security import policy

GATE = Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "sentinel-gate.py"


def _gate():
    """The bundled gate script loaded as a module."""
    spec = importlib.util.spec_from_file_location("sentinel_gate", GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gate_list_equals_the_policy_lists() -> None:
    assert list(_gate()._BLOCK) == list(policy.UNBYPASSABLE + policy.LEVEL_GATED)


@pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]"])
def test_gate_blocks_unreadable_input(monkeypatch: pytest.MonkeyPatch, stdin: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    assert _gate().main() == 2


def test_gate_blocks_sudo_and_allows_ls(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _gate()
    monkeypatch.setattr("sys.stdin", io.StringIO('{"tool_name": "Bash", "tool_input": {"command": "sudo ls"}}'))
    assert gate.main() == 2
    monkeypatch.setattr("sys.stdin", io.StringIO('{"tool_name": "Bash", "tool_input": {"command": "ls"}}'))
    assert gate.main() == 0
