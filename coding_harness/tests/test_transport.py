"""Tests for the Ollama HTTP transport (P0-1).

A local ``ThreadingHTTPServer`` plays Ollama so each failure mode is
deterministic: a refused connection retries then raises, a 5xx retries once
and succeeds, an empty-choices body retries, and a response that stalls past
the read timeout surfaces as ``timed out`` with no retry at all. Backoff
sleeps are patched out so the suite stays fast.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from coding_harness.models import ollama, transport

BODY = b'{"model": "x", "messages": []}'
OK_BODY = {
    "choices": [{"message": {"role": "assistant", "content": "hi"}}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
}


class _Script:
    """Per-server plan: (status, body, stall_s) per request; the last step repeats.

    A ``str`` body is an SSE prefix: it is sent, then the stream stalls."""

    def __init__(self, steps: list[tuple[int, dict | str, float]]) -> None:
        self.steps = steps
        self.hits = 0
        self.lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        script: _Script = self.server.script  # type: ignore[attr-defined]
        with script.lock:
            idx = script.hits
            script.hits += 1
        status, body, stall = script.steps[min(idx, len(script.steps) - 1)]
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if isinstance(body, str):
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            threading.Event().wait(stall)
            return
        if stall:
            # Not time.sleep: the tests patch that out for the client's backoff.
            threading.Event().wait(stall)
        data = json.dumps(body).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            return  # client gave up first; that is the point of the stall test


class TestTransport(unittest.TestCase):
    def _serve(self, steps: list[tuple[int, dict | str, float]]) -> tuple[_Script, str]:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.daemon_threads = True
        server.script = _Script(steps)  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.script, f"http://127.0.0.1:{server.server_port}"  # type: ignore[attr-defined]

    def _no_sleep(self) -> mock.MagicMock:
        patch = mock.patch.object(transport.time, "sleep")
        self.addCleanup(patch.stop)
        return patch.start()

    def test_connection_refused_retries_then_raises(self) -> None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        url = f"http://127.0.0.1:{port}/v1/chat/completions"
        calls = {"n": 0}

        def _fn():
            calls["n"] += 1
            return transport.open_stream(url, BODY, read_timeout=1.0)

        sleep = self._no_sleep()
        with self.assertRaises(RuntimeError) as ctx:
            transport.with_retries(_fn)
        self.assertIn("ollama transport error", str(ctx.exception))
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleep.call_count, 2)

    def test_stalled_read_times_out_without_retry(self) -> None:
        script, base = self._serve([(200, OK_BODY, 1.0)])
        calls = {"n": 0}

        def _fn():
            calls["n"] += 1
            with transport.open_stream(
                f"{base}/v1/chat/completions", BODY, read_timeout=0.2,
            ) as resp:
                return resp.read()

        sleep = self._no_sleep()
        with self.assertRaises(RuntimeError) as ctx:
            transport.with_retries(_fn)
        self.assertIn("timed out", str(ctx.exception))
        self.assertEqual(calls["n"], 1)
        sleep.assert_not_called()

    def test_mid_stream_stall_surfaces_without_replay(self) -> None:
        chunk = 'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n'
        script, base = self._serve([(200, chunk, 1.0)])
        deltas: list[str] = []
        sleep = self._no_sleep()
        with mock.patch.object(ollama, "OLLAMA_URL", base):
            with self.assertRaises(RuntimeError) as ctx:
                ollama.chat(
                    model="mistral-small3.2:latest", messages=[],
                    timeout=0.2, on_delta=deltas.append,
                )
        self.assertIn("timed out", str(ctx.exception))
        # The delta already forwarded is not replayed by a retry.
        self.assertEqual(deltas, ["hi"])
        self.assertEqual(script.hits, 1)
        sleep.assert_not_called()

    def test_503_then_200_succeeds_on_second_attempt(self) -> None:
        script, base = self._serve([(503, {"error": "busy"}, 0), (200, OK_BODY, 0)])
        sleep = self._no_sleep()
        with mock.patch.object(ollama, "OLLAMA_URL", base):
            msg = ollama.chat(
                model="mistral-small3.2:latest",
                messages=[{"role": "user", "content": "x"}],
            )
        self.assertEqual(msg["content"], "hi")
        self.assertEqual(msg["_usage"]["tokens_in"], 3)
        self.assertEqual(script.hits, 2)
        self.assertEqual(sleep.call_count, 1)

    def test_empty_choices_retries(self) -> None:
        script, base = self._serve([(200, {"choices": []}, 0), (200, OK_BODY, 0)])
        self._no_sleep()
        with mock.patch.object(ollama, "OLLAMA_URL", base):
            msg = ollama.chat(model="mistral-small3.2:latest", messages=[])
        self.assertEqual(msg["content"], "hi")
        self.assertEqual(script.hits, 2)

    def test_4xx_is_not_retried(self) -> None:
        script, base = self._serve([(400, {"error": "bad request"}, 0)])
        sleep = self._no_sleep()
        with mock.patch.object(ollama, "OLLAMA_URL", base):
            with self.assertRaises(RuntimeError) as ctx:
                ollama.chat(model="mistral-small3.2:latest", messages=[])
        self.assertTrue(str(ctx.exception).startswith("ollama HTTP 400: "))
        self.assertEqual(script.hits, 1)
        sleep.assert_not_called()

    def _always_empty(self) -> tuple:
        calls = {"n": 0}

        def _fn():
            calls["n"] += 1
            raise transport.EmptyResponse("no choices")

        return calls, _fn

    def test_deadline_stops_retries_that_would_overrun_it(self) -> None:
        calls, fn = self._always_empty()
        sleep = self._no_sleep()
        with self.assertRaises(RuntimeError):
            transport.with_retries(fn, deadline=time.monotonic() + 1.0)
        # The first backoff is 2 s, past the 1 s left: no sleep, no second try.
        self.assertEqual(calls["n"], 1)
        sleep.assert_not_called()
        calls["n"] = 0
        with self.assertRaises(RuntimeError):
            transport.with_retries(fn, deadline=time.monotonic() + 100.0)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleep.call_count, 2)

    def test_retry_scope_bounds_the_calls_inside_it(self) -> None:
        calls, fn = self._always_empty()
        sleep = self._no_sleep()
        with transport.retry_scope(deadline=time.monotonic() + 1.0):
            with self.assertRaises(RuntimeError):
                transport.with_retries(fn)
        self.assertEqual(calls["n"], 1)
        calls["n"] = 0
        with transport.retry_scope(attempts=1):
            with self.assertRaises(RuntimeError):
                transport.with_retries(fn)
        self.assertEqual(calls["n"], 1)
        sleep.assert_not_called()
        calls["n"] = 0
        with self.assertRaises(RuntimeError):
            transport.with_retries(fn)
        self.assertEqual(calls["n"], 3)
        self.assertIsNone(getattr(transport._scope, "limits", None))

    def test_retry_scope_restores_the_outer_limits(self) -> None:
        with transport.retry_scope(attempts=2):
            with transport.retry_scope(attempts=1):
                self.assertEqual(transport._scope.limits, (None, 1))
            self.assertEqual(transport._scope.limits, (None, 2))
        self.assertIsNone(transport._scope.limits)

    def test_read_timeout_env_respected(self) -> None:
        seen: dict[str, float | None] = {}

        def _capture(url, payload, *, connect_timeout=10.0, read_timeout=None):
            seen["read_timeout"] = read_timeout
            raise ValueError("stop here")

        with mock.patch.dict(os.environ, {"HARNESS_READ_TIMEOUT": "42"}):
            self.assertEqual(transport.read_timeout_s(), 42.0)
            with mock.patch.object(transport, "open_stream", _capture):
                with self.assertRaises(ValueError):
                    ollama.chat(model="mistral-small3.2:latest", messages=[])
                self.assertEqual(seen["read_timeout"], 42.0)
                with self.assertRaises(ValueError):
                    ollama.chat(model="mistral-small3.2:latest", messages=[], timeout=7)
                self.assertEqual(seen["read_timeout"], 7)
        with mock.patch.dict(os.environ, {"HARNESS_READ_TIMEOUT": "not-a-number"}):
            self.assertEqual(transport.read_timeout_s(), 360.0)
        with mock.patch.dict(os.environ):
            os.environ.pop("HARNESS_READ_TIMEOUT", None)
            self.assertEqual(transport.read_timeout_s(), 360.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
