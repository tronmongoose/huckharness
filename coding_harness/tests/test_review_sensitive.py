"""A sensitive session never sends its diffs to the claude-cli reviewer."""
from __future__ import annotations

import unittest
from unittest import mock

from coding_harness.core import review
from coding_harness.core.review import ReviewResult
from coding_harness.models import claude_cli, ollama
from coding_harness.tests import test_review_session as rs
from coding_harness.tests.test_review import _env, _fake_chat
from coding_harness.tests.test_review_session import (
    _edit_call,
    _fifty_lines,
    _Scripted,
    _terminal,
)


def _boom(*_a, **_kw):
    raise AssertionError("a sensitive session must not reach claude-cli")


def test_sensitive_forces_the_local_reviewer(monkeypatch) -> None:
    _env(monkeypatch, HARNESS_REVIEW_BACKEND="claude-cli", HARNESS_REVIEW_MODEL="opus")
    monkeypatch.setattr(claude_cli, "chat", _boom)
    monkeypatch.setattr(review, "_tag_present", lambda _m: True)
    seen: dict = {}
    monkeypatch.setattr(ollama, "chat", _fake_chat(seen, '{"verdict": "APPROVE", "concerns": ""}'))
    result = review.review_change("t", {"/w/f.py": "d"}, fallback_model="c", sensitive=True)
    assert result == ReviewResult(True, "", "local", review.DEFAULT_LOCAL_MODEL)
    assert seen["model"] == review.DEFAULT_LOCAL_MODEL


class SensitiveReviewSessionTests(unittest.TestCase):
    # Borrow the fixture, not the inherited tests.
    setUp = rs.ReviewSessionTests.setUp
    tearDown = rs.ReviewSessionTests.tearDown
    _session = rs.ReviewSessionTests._session

    def test_a_sensitive_session_reroutes_and_never_calls_the_cli(self) -> None:
        f = self.tp / "code.py"
        f.write_text(_fifty_lines())
        session = self._session(_Scripted([
            _edit_call(str(f), "value_25 = 25", "value_25 = 250"), _terminal()]))
        session.sensitive_context = True
        reviewer = mock.MagicMock(return_value=ReviewResult(True, "", "local", "m"))
        with mock.patch.dict("os.environ", {"HARNESS_REVIEW_BACKEND": "claude-cli"}), \
                mock.patch.object(review, "review_change", reviewer), \
                mock.patch.object(claude_cli, "chat", _boom):
            session.run_turn("change value 25")
        assert reviewer.call_args.kwargs["sensitive"] is True
        rerouted = [e.payload for e in self.events if e.kind == "review_rerouted"]
        assert rerouted == [{"from": "claude-cli", "to": "local", "reason": "sensitive_context"}]
