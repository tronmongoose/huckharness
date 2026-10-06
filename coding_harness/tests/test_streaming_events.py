"""Tests for token streaming (P1).

Two layers:
  1. ``ollama._chat_streaming`` correctly parses an OpenAI-compat SSE stream —
     assembling content, folding tool-call fragments by index, preserving the
     ``_usage`` telemetry the non-streaming path carries (nightshift depends on
     it, sl-zvi3).
  2. ``Session`` fans ``assistant_delta`` events out to its event_sink while
     keeping the final assembled ``assistant_message`` identical.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coding_harness.core.session import Session
from coding_harness.models import ollama, transport
from coding_harness.modes.print_mode import SYSTEM_PROMPT, build_registry
from coding_harness.security import audit


def _sse(obj) -> str:
    return f"data: {json.dumps(obj)}\n"


class _FakeSSEResponse:
    """Minimal stand-in for the transport.open_stream response: a context
    manager whose iteration yields raw ``bytes`` lines, like the real HTTP
    response object does."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = [ln.encode("utf-8") for ln in lines]

    def __enter__(self) -> _FakeSSEResponse:
        return self

    def __exit__(self, *exc) -> None:
        return None

    def __iter__(self):
        return iter(self._lines)


class TestOllamaStreamParse(unittest.TestCase):
    def _run(self, lines: list[str], on_delta):
        fake = _FakeSSEResponse(lines)
        with mock.patch.object(transport, "open_stream", return_value=fake):
            # url/body are unused by the fake; pass sentinels.
            return ollama._chat_streaming("http://x/v1/chat/completions", b"{}", 30, on_delta)

    def test_content_deltas_assemble(self) -> None:
        deltas: list[str] = []
        msg = self._run(
            [
                _sse({"choices": [{"delta": {"content": "Hel"}}]}),
                _sse({"choices": [{"delta": {"content": "lo"}}]}),
                _sse({"usage": {"prompt_tokens": 5, "completion_tokens": 2}}),
                "data: [DONE]\n",
            ],
            deltas.append,
        )
        self.assertEqual(msg["content"], "Hello")
        self.assertEqual(deltas, ["Hel", "lo"])
        self.assertEqual(msg["_usage"]["tokens_in"], 5)
        self.assertEqual(msg["_usage"]["tokens_out"], 2)

    def test_tool_calls_fold_by_index(self) -> None:
        msg = self._run(
            [
                _sse({"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "c1",
                     "function": {"name": "Read", "arguments": '{"file'}}
                ]}}]}),
                _sse({"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": '_path":"/x"}'}}
                ]}}]}),
                "data: [DONE]\n",
            ],
            lambda _t: None,
        )
        self.assertEqual(len(msg["tool_calls"]), 1)
        tc = msg["tool_calls"][0]
        self.assertEqual(tc["id"], "c1")
        self.assertEqual(tc["function"]["name"], "Read")
        self.assertEqual(tc["function"]["arguments"], '{"file_path":"/x"}')

    def test_blank_and_non_data_lines_ignored(self) -> None:
        msg = self._run(
            [
                "\n",
                ": keepalive\n",
                _sse({"choices": [{"delta": {"content": "hi"}}]}),
                "data: [DONE]\n",
            ],
            lambda _t: None,
        )
        self.assertEqual(msg["content"], "hi")


class TestSessionStreamingEvents(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(audit, "AUDIT_PATH", tmp_path / "audit.jsonl"),
            mock.patch.object(audit, "ANCHORS_PATH", tmp_path / "anchors.jsonl"),
            mock.patch.object(audit, "META_DIR", tmp_path),
            mock.patch("coding_harness.core.session.SESSIONS_DIR", tmp_path / "s"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_session_emits_deltas_and_turn_bounds(self) -> None:
        events: list[tuple[str, dict]] = []

        def _sink(ev) -> None:
            events.append((ev.kind, ev.payload))

        def _chat(*, model, messages, tools, on_delta=None, **_kw):
            content = "hello world"
            if on_delta is not None:
                for tok in content.split(" "):
                    on_delta(tok + " ")
            return {
                "role": "assistant", "content": content, "tool_calls": [],
                "_usage": {"tokens_in": 3, "tokens_out": 2, "thinking_tokens": None},
            }

        registry = build_registry(event_sink=None, enable_mcp=False)
        session = Session(
            model="mistral-small3.2:latest",
            registry=registry,
            system_prompt=SYSTEM_PROMPT,
            event_sink=_sink,
            force_local=True,
        )
        with mock.patch(
            "coding_harness.core.session.ollama.chat", side_effect=_chat
        ):
            result = session.run_turn("go")

        kinds = [k for k, _ in events]
        self.assertIn("turn_start", kinds)
        self.assertIn("assistant_delta", kinds)
        self.assertIn("turn_done", kinds)
        deltas = [p["text"] for k, p in events if k == "assistant_delta"]
        self.assertEqual("".join(deltas).strip(), "hello world")
        self.assertEqual(result.halted_reason, "model_done")
        self.assertEqual(result.final_text, "hello world")

    def test_non_streaming_backend_emits_final_text_fallback(self) -> None:
        # A backend that ignores on_delta (non-streaming API path, strict
        # test double) must still surface its reply as one delta event —
        # the UI renders thread text only from deltas.
        events: list[tuple[str, dict]] = []

        def _sink(ev) -> None:
            events.append((ev.kind, ev.payload))

        def _chat(*, model, messages, tools, on_delta=None, **_kw):
            return {
                "role": "assistant", "content": "quiet reply", "tool_calls": [],
                "_usage": {"tokens_in": 1, "tokens_out": 1, "thinking_tokens": None},
            }

        registry = build_registry(event_sink=None, enable_mcp=False)
        session = Session(
            model="mistral-small3.2:latest",
            registry=registry,
            system_prompt=SYSTEM_PROMPT,
            event_sink=_sink,
            force_local=True,
        )
        with mock.patch(
            "coding_harness.core.session.ollama.chat", side_effect=_chat
        ):
            result = session.run_turn("go")

        deltas = [p["text"] for k, p in events if k == "assistant_delta"]
        self.assertEqual(deltas, ["quiet reply"])
        self.assertEqual(result.final_text, "quiet reply")

    def test_no_event_sink_skips_streaming(self) -> None:
        # Without an event_sink, on_delta is never passed — nightshift-style
        # callers keep the non-streaming path.
        seen = {}

        def _chat(*, model, messages, tools, on_delta=None, **_kw):
            seen["on_delta"] = on_delta
            return {
                "role": "assistant", "content": "x", "tool_calls": [],
                "_usage": {"tokens_in": 0, "tokens_out": 0, "thinking_tokens": None},
            }

        registry = build_registry(event_sink=None, enable_mcp=False)
        session = Session(
            model="mistral-small3.2:latest",
            registry=registry,
            system_prompt=SYSTEM_PROMPT,
            event_sink=None,
            force_local=True,
        )
        with mock.patch(
            "coding_harness.core.session.ollama.chat", side_effect=_chat
        ):
            session.run_turn("go")
        self.assertIsNone(seen["on_delta"])


class _FakeJSONResponse:
    """Non-streaming stand-in for transport.open_stream: a context manager with read()."""

    def __init__(self, body: dict) -> None:
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self) -> _FakeJSONResponse:
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class TestChatPayloadResponseFormat(unittest.TestCase):
    """``response_format`` rides the request body only when given, on both paths."""

    def _payload(self, streaming: bool, **chat_kw) -> dict:
        seen: dict = {}

        def _open(url, body, **_kw):
            seen["payload"] = json.loads(body.decode("utf-8"))
            if streaming:
                return _FakeSSEResponse([
                    _sse({"choices": [{"delta": {"content": "{}"}}]}), "data: [DONE]\n",
                ])
            return _FakeJSONResponse({
                "choices": [{"message": {"role": "assistant", "content": "{}"}}],
            })

        with mock.patch.object(transport, "open_stream", side_effect=_open):
            ollama.chat(
                model="mistral-small3.2:latest", messages=[],
                on_delta=(lambda _t: None) if streaming else None, **chat_kw,
            )
        return seen["payload"]

    def test_non_streaming_carries_response_format(self) -> None:
        payload = self._payload(False, response_format={"type": "json_object"})
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertFalse(payload["stream"])

    def test_streaming_carries_response_format(self) -> None:
        payload = self._payload(True, response_format={"type": "json_object"})
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertTrue(payload["stream"])

    def test_omitted_by_default_on_both_paths(self) -> None:
        self.assertNotIn("response_format", self._payload(False))
        self.assertNotIn("response_format", self._payload(True))

    def test_top_p_rides_the_body_only_when_set(self) -> None:
        for streaming in (False, True):
            self.assertEqual(self._payload(streaming, top_p=0.95)["top_p"], 0.95)
            self.assertNotIn("top_p", self._payload(streaming))
            self.assertNotIn("top_p", self._payload(streaming, top_p=None))

    def test_sampling_defaults_unchanged(self) -> None:
        payload = self._payload(False, temperature=0.15, max_tokens=4096)
        self.assertEqual((payload["temperature"], payload["max_tokens"]), (0.15, 4096))
        payload = self._payload(False)
        self.assertEqual((payload["temperature"], payload["max_tokens"]), (0.2, 2048))


class TestFinishReason(unittest.TestCase):
    """``finish_reason`` rides ``_usage`` on both transport paths (P1-3)."""

    def test_non_streaming_surfaces_choice_finish_reason(self) -> None:
        fake = _FakeJSONResponse({
            "choices": [{
                "message": {"role": "assistant", "content": "partial"},
                "finish_reason": "length",
            }],
            "usage": {"prompt_tokens": 4, "completion_tokens": 9},
        })
        with mock.patch.object(transport, "open_stream", return_value=fake):
            msg = ollama.chat(model="mistral-small3.2:latest", messages=[])
        self.assertEqual(msg["_usage"]["finish_reason"], "length")
        self.assertEqual(msg["_usage"]["tokens_out"], 9)
        self.assertNotIn("finish_reason", msg)

    def test_non_streaming_missing_finish_reason_is_none(self) -> None:
        fake = _FakeJSONResponse({
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        })
        with mock.patch.object(transport, "open_stream", return_value=fake):
            msg = ollama.chat(model="mistral-small3.2:latest", messages=[])
        self.assertIsNone(msg["_usage"]["finish_reason"])

    def test_streaming_keeps_last_non_empty_finish_reason(self) -> None:
        fake = _FakeSSEResponse([
            _sse({"choices": [{"delta": {"content": "a"}, "finish_reason": None}]}),
            _sse({"choices": [{"delta": {"content": "b"}, "finish_reason": "length"}]}),
            _sse({"choices": [{"delta": {}, "finish_reason": None}]}),
            _sse({"usage": {"prompt_tokens": 1, "completion_tokens": 2}}),
            "data: [DONE]\n",
        ])
        with mock.patch.object(transport, "open_stream", return_value=fake):
            msg = ollama._chat_streaming("http://x", b"{}", 30, lambda _t: None)
        self.assertEqual(msg["content"], "ab")
        self.assertEqual(msg["_usage"]["finish_reason"], "length")
        self.assertEqual(msg["_usage"]["tokens_out"], 2)

    def test_streaming_without_finish_reason_is_none(self) -> None:
        fake = _FakeSSEResponse([
            _sse({"choices": [{"delta": {"content": "a"}}]}),
            "data: [DONE]\n",
        ])
        with mock.patch.object(transport, "open_stream", return_value=fake):
            msg = ollama._chat_streaming("http://x", b"{}", 30, lambda _t: None)
        self.assertIsNone(msg["_usage"]["finish_reason"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
