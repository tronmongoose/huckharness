"""Agentic review unit tests: verdict parsing (JSON, fenced, embedded, legacy
VERDICT line, garbage), backend selection by env, the diff-based prompt, the
local backend's tag fallback and json_object request, and the claude-cli
sensitivity guard. The session integration lives in test_review_session.py."""
from __future__ import annotations

import io
import json
from unittest import mock

import pytest

from coding_harness.core import review
from coding_harness.core.review import ReviewResult
from coding_harness.core.router import RouteDecision
from coding_harness.models import ollama
from coding_harness.tools.edit import _unified_diff

_ENV_KEYS = ("HARNESS_REVIEW", "HARNESS_REVIEW_BACKEND", "HARNESS_REVIEW_MODEL")


def _env(monkeypatch, **values):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _fifty_lines(changed: int | None = None) -> str:
    lines = [f"value_{i:02d} = {i}" for i in range(1, 51)]
    if changed is not None:
        lines[changed - 1] = f"value_{changed:02d} = {changed * 10}"
    return "\n".join(lines) + "\n"


# ── Parsing ──────────────────────────────────────────────────


def test_parse_json_plain():
    assert review._parse('{"verdict": "APPROVE", "concerns": ""}') == (True, "", False)
    assert review._parse('{"verdict": "REVISE", "concerns": "fix the operator"}') == (
        False, "fix the operator", False,
    )


def test_parse_json_fenced():
    text = 'Sure.\n```json\n{"verdict": "REVISE", "concerns": "off by one"}\n```\n'
    assert review._parse(text) == (False, "off by one", False)


def test_parse_json_embedded():
    text = 'Verdict: {"verdict": "APPROVE", "concerns": ""} -- done.'
    assert review._parse(text) == (True, "", False)


def test_parse_legacy_verdict_line():
    assert review._parse("looks fine\nVERDICT: APPROVE") == (True, "", False)
    ok, concerns, parse_error = review._parse("has a bug\nVERDICT: REVISE - fix the operator")
    assert (ok, parse_error) == (False, False) and "fix the operator" in concerns


def test_parse_skips_leaked_thinking_and_takes_last_block():
    text = (
        'Example: {"verdiff": "X"} is not allowed.\n</think>\n'
        'Note {"verdict": "APPROVE", "concerns": ""} was my draft.\n'
        '{"verdict": "REVISE", "concerns": "sub is untouched"}'
    )
    assert review._parse(text) == (False, "sub is untouched", False)
    assert review._parse('draft {"verdict": "REVISE", "concerns": "x"}\n</think>\nno verdict') == (True, "", True)


def test_parse_only_the_exact_approve_token_approves():
    for verdict in ("NOT APPROVED", "DISAPPROVE", "DISAPPROVED", "APPROVE, REVISE first",
                    "REVISE (not approve)", "not-approve"):
        ok, concerns, parse_error = review._parse(json.dumps({"verdict": verdict, "concerns": ""}))
        assert (ok, parse_error) == (False, False), verdict
        assert concerns == verdict
    for line in ("VERDICT: NOT APPROVED", "VERDICT: DISAPPROVE", "VERDICT: APPROVE after REVISE"):
        ok, concerns, parse_error = review._parse(f"reasoning\n{line}")
        assert (ok, concerns, parse_error) == (False, line, False)
    assert review._parse('{"verdict": " approve. "}') == (True, "", False)
    assert review._parse("VERDICT: approve.") == (True, "", False)


def test_parse_garbage_fails_open_with_parse_error():
    assert review._parse("the reviewer rambled with no verdict") == (True, "", True)
    assert review._parse("") == (True, "", True)


# ── Backend selection ────────────────────────────────────────


def test_default_is_local_and_enabled(monkeypatch):
    _env(monkeypatch)
    assert review.enabled() and review.backend() == "local"
    assert review.review_model() == "mistral-small3.2"


def test_backend_off_disables(monkeypatch):
    _env(monkeypatch, HARNESS_REVIEW_BACKEND="off")
    assert not review.enabled()


def test_harness_review_zero_is_the_kill_switch(monkeypatch):
    _env(monkeypatch, HARNESS_REVIEW="0")
    assert not review.enabled() and review.backend() == "local"


def test_backend_claude_cli(monkeypatch):
    _env(monkeypatch, HARNESS_REVIEW_BACKEND="claude-cli")
    assert review.enabled() and review.backend() == "claude-cli"
    assert review.review_model() == "opus"


def test_review_model_env_and_banned(monkeypatch):
    _env(monkeypatch, HARNESS_REVIEW_MODEL="gemma4:26b")
    assert review.review_model() == "gemma4:26b"
    monkeypatch.setenv("HARNESS_REVIEW_MODEL", "qwen3:8b")
    with pytest.raises(ollama.BannedModelError):
        review.review_model()


# ── Prompt ───────────────────────────────────────────────────


def test_prompt_carries_diffs_not_whole_files():
    diff = _unified_diff(_fifty_lines(), _fifty_lines(changed=25), "/w/f.py")
    prompt = review._prompt("bump 25", {"/w/f.py": diff}, None)
    assert "@@" in prompt and "+value_25 = 250" in prompt and "-value_25 = 25" in prompt
    assert "value_01 = 1" not in prompt and "value_50 = 50" not in prompt
    assert "TASK: bump 25" in prompt and '"verdict"' in prompt


def test_prompt_shows_new_file_whole():
    diff = _unified_diff("", "def f():\n    return 1\n", "/w/new.py")
    prompt = review._prompt("add f", {"/w/new.py": diff}, None)
    assert "--- /w/new.py (new file) ---\ndef f():\n    return 1" in prompt
    assert "+def f():" not in prompt


def test_prompt_caps_each_file_with_marker():
    prompt = review._prompt("t", {"/w/big.py": "x" * 9000}, None)
    assert "[truncated]" in prompt and "x" * 8001 not in prompt


def test_prompt_includes_verify_report():
    prompt = review._prompt("t", {"/w/f.py": "d"}, "ruff: F401 unused import")
    assert "ruff: F401 unused import" in prompt


# ── Local backend ────────────────────────────────────────────


def _fake_chat(seen: dict, content: str):
    def _chat(**kw):
        seen.update(kw)
        return {"role": "assistant", "content": content}
    return _chat


def test_local_backend_requests_json_object(monkeypatch):
    _env(monkeypatch)
    seen: dict = {}
    monkeypatch.setattr(review, "_tag_present", lambda _m: True)
    monkeypatch.setattr(ollama, "chat", _fake_chat(seen, '{"verdict": "REVISE", "concerns": "off by one"}'))
    result = review.review_change("task", {"/w/f.py": "diff"}, "report", fallback_model="coder:1")
    assert result == ReviewResult(False, "off by one", "local", "mistral-small3.2", False, False)
    assert seen["model"] == "mistral-small3.2" and seen["tools"] is None
    assert seen["response_format"] == {"type": "json_object"}
    assert seen["max_tokens"] == 512 and seen["temperature"] == 0.1
    assert seen["messages"][0]["role"] == "system"
    assert "TASK: task" in seen["messages"][1]["content"] and "report" in seen["messages"][1]["content"]


def test_local_backend_falls_back_to_coder_when_tag_missing(monkeypatch):
    _env(monkeypatch)
    seen: dict = {}
    monkeypatch.setattr(review, "_tag_present", lambda _m: False)
    monkeypatch.setattr(ollama, "chat", _fake_chat(seen, '{"verdict": "APPROVE"}'))
    result = review.review_change("task", {"/w/f.py": "diff"}, fallback_model="coder:1")
    assert seen["model"] == "coder:1"
    assert result == ReviewResult(True, "", "local", "coder:1", False, True)


def test_review_fallback_reasserts_allowlist(monkeypatch):
    _env(monkeypatch)
    seen: dict = {}
    monkeypatch.setattr(review, "_tag_present", lambda _m: False)
    monkeypatch.setattr(ollama, "chat", _fake_chat(seen, '{"verdict": "APPROVE"}'))
    with pytest.raises(ollama.BannedModelError):
        review.review_change("task", {"/w/f.py": "diff"}, fallback_model="qwen2.5:72b")
    assert seen == {}


def test_local_backend_fails_open_with_parse_error(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(review, "_tag_present", lambda _m: True)
    monkeypatch.setattr(ollama, "chat", _fake_chat({}, "no verdict here"))
    assert review.review_change("t", {"/w/f.py": "d"}, fallback_model="c").parse_error is True

    def _boom(**_kw):
        raise RuntimeError("ollama down")
    monkeypatch.setattr(ollama, "chat", _boom)
    result = review.review_change("t", {"/w/f.py": "d"}, fallback_model="c")
    assert result.approved and result.parse_error


def test_local_backend_skips_sensitivity_guard(monkeypatch):
    import coding_harness.core.router as router_mod
    _env(monkeypatch)
    monkeypatch.setattr(review, "_tag_present", lambda _m: True)
    monkeypatch.setattr(ollama, "chat", _fake_chat({}, '{"verdict": "REVISE", "concerns": "c"}'))

    def _never(*_a, **_k):
        raise AssertionError("router must not run for the local reviewer")
    monkeypatch.setattr(router_mod, "decide_route", _never)
    result = review.review_change("what is my 401k balance", {"/w/f.py": "d"}, fallback_model="c")
    assert result.approved is False and result.backend == "local"


def test_empty_diffs_approve_without_a_call(monkeypatch):
    _env(monkeypatch)

    def _boom(**_kw):
        raise AssertionError("no call expected")
    monkeypatch.setattr(ollama, "chat", _boom)
    assert review.review_change("t", {}, fallback_model="c") == ReviewResult(True, "", "local", "")


def test_fleet_env_selects_local_and_never_touches_claude_cli(monkeypatch):
    import coding_harness.core.router as router_mod
    from coding_harness.models import claude_cli
    _env(monkeypatch, HARNESS_REVIEW="1")
    assert review.enabled() and review.backend() == "local"
    assert "claude_cli" not in vars(review)
    seen: dict = {}
    monkeypatch.setattr(review, "_tag_present", lambda _m: True)
    monkeypatch.setattr(ollama, "chat", _fake_chat(seen, '{"verdict": "APPROVE", "concerns": ""}'))

    def _boom(*_a, **_k):
        raise AssertionError("the fleet env must stay local")
    monkeypatch.setattr(claude_cli, "chat", _boom)
    monkeypatch.setattr(router_mod, "decide_route", _boom)
    result = review.review_change("bead task", {"/w/f.py": "diff"}, fallback_model="c")
    assert result == ReviewResult(True, "", "local", "mistral-small3.2")
    assert seen["model"] == "mistral-small3.2" and seen["response_format"] == {"type": "json_object"}


def _tags_response(names: list[str]):
    body = json.dumps({"models": [{"name": n} for n in names]}).encode("utf-8")

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None
    return _Resp(body)


def test_tag_present_probes_api_tags_and_caches(monkeypatch):
    monkeypatch.setattr(review, "_tag_cache", {})
    with mock.patch("urllib.request.urlopen", return_value=_tags_response(["granite4.1:8b"])) as m:
        assert review._tag_present("granite4.1:8b") is True
        assert review._tag_present("absent:1b") is False
        assert m.call_args.kwargs["timeout"] == 3.0
    with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
        assert review._tag_present("granite4.1:8b") is True  # cached
        assert review._tag_present("other:1b") is False  # probe error, not cached
    assert "other:1b" not in review._tag_cache


# ── claude-cli backend ───────────────────────────────────────


def test_cli_backend_sensitive_never_hits_frontier(monkeypatch):
    import coding_harness.core.router as router_mod
    from coding_harness.models import claude_cli
    _env(monkeypatch, HARNESS_REVIEW_BACKEND="claude-cli")
    monkeypatch.setattr(router_mod, "decide_route",
                        lambda q, context=None: RouteDecision(sensitivity_flag=True))

    def _boom(**_kw):
        raise AssertionError("frontier must not be called")
    monkeypatch.setattr(claude_cli, "chat", _boom)
    result = review.review_change("what is my 401k balance", {"/w/f.py": "x = 1\n"}, fallback_model="c")
    assert result == ReviewResult(True, "", "claude-cli", "opus")


def test_cli_backend_parses_verdict_line(monkeypatch):
    import coding_harness.core.router as router_mod
    from coding_harness.models import claude_cli
    _env(monkeypatch, HARNESS_REVIEW_BACKEND="claude-cli")
    monkeypatch.setattr(router_mod, "decide_route",
                        lambda q, context=None: RouteDecision(sensitivity_flag=False))
    seen: dict = {}
    monkeypatch.setattr(claude_cli, "chat", _fake_chat(seen, "reasoning\nVERDICT: REVISE - fix it"))
    result = review.review_change("task", {"/w/f.py": "diff"}, fallback_model="c")
    assert result.approved is False and result.backend == "claude-cli" and not result.parse_error
    assert seen["model"] == "opus" and seen["decision"].route == "frontier"


def test_cli_backend_fails_open_on_error(monkeypatch):
    import coding_harness.core.router as router_mod
    from coding_harness.models import claude_cli
    _env(monkeypatch, HARNESS_REVIEW_BACKEND="claude-cli")
    monkeypatch.setattr(router_mod, "decide_route",
                        lambda q, context=None: RouteDecision(sensitivity_flag=False))

    def _boom(**_kw):
        raise RuntimeError("cli down")
    monkeypatch.setattr(claude_cli, "chat", _boom)
    result = review.review_change("task", {"/w/f.py": "diff"}, fallback_model="c")
    assert result.approved and result.parse_error and result.backend == "claude-cli"
