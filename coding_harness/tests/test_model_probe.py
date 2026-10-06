"""The forced tool-call probe: parsing, the digest-keyed cache, one run at a time."""
from __future__ import annotations

import threading
from unittest import mock

from coding_harness.modes import model_probe


def _reply(name="report", args='{"value": 42}', content=""):
    return {"role": "assistant", "content": content,
            "tool_calls": [{"function": {"name": name, "arguments": args}}] if name else []}


def test_only_the_exact_call_passes() -> None:
    assert model_probe._passed(_reply())
    assert model_probe._passed(_reply(args={"value": 42}))
    assert not model_probe._passed(_reply(args='{"value": 7}'))
    assert not model_probe._passed(_reply(name="other"))
    assert not model_probe._passed(_reply(name=None, content="I can't directly modify files"))


def test_verdicts_are_cached_per_digest() -> None:
    with mock.patch("coding_harness.models.ollama.chat", return_value=_reply()):
        assert model_probe.probe("gpt-oss:20b", "d1")["agent"] is True
    with mock.patch("coding_harness.models.ollama.chat",
                    return_value=_reply(name=None, content="As an AI text-based model...")):
        assert model_probe.probe("phi4-mini:latest", "d2")["agent"] is False
    assert model_probe.cached("gpt-oss:20b", "d1") is True
    assert model_probe.cached("phi4-mini:latest", "d2") is False
    assert model_probe.cached("gpt-oss:20b", "re-pulled") is None


def test_a_probe_that_errors_is_recorded_as_not_an_agent() -> None:
    with mock.patch("coding_harness.models.ollama.chat", side_effect=RuntimeError("timed out")):
        out = model_probe.probe("gemma4:26b", "d3")
    assert out["agent"] is False and "timed out" in out["detail"]


def test_start_runs_one_probe_per_model_at_a_time() -> None:
    gate = threading.Event()

    def slow(**_kw):
        gate.wait(5)
        return _reply()

    with mock.patch("coding_harness.models.ollama.chat", side_effect=slow):
        assert model_probe.start("m", "d") is True
        assert model_probe.probing("m") and model_probe.start("m", "d") is False
        gate.set()
        for _ in range(100):
            if not model_probe.probing("m"):
                break
            threading.Event().wait(0.02)
    assert model_probe.cached("m", "d") is True
