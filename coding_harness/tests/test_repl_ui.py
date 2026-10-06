"""REPL terminal surface: multi-line prompts, streamed deltas, arg summaries,
the settings ``model`` key and the banner's command list."""
from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from coding_harness.core.settings import SettingsError, load_settings
from coding_harness.modes import _pretty as pretty
from coding_harness.modes import repl_input
from coding_harness.tools.registry import DispatchEvent


def _reader(lines):
    it = iter(lines)

    def read_line(_prompt: str) -> str:
        value = next(it)
        if isinstance(value, BaseException):
            raise value
        return value

    return read_line


def _prompt(lines):
    err = io.StringIO()
    got = repl_input.read_prompt("> ", read_line=_reader(lines), stream=err)
    return got, err.getvalue()


def test_single_line_prompt_and_glyph_on_stderr():
    got, err = _prompt(["hello"])
    assert got == "hello" and err == "> "


def test_backslash_continues_the_prompt():
    got, err = _prompt(["first \\", "second \\", "third"])
    assert got == "first \nsecond \nthird"
    assert err == "> " + repl_input.CONTINUATION_GLYPH * 2


def test_fence_block_runs_to_the_closing_fence():
    got, _ = _prompt(["```", "def f():", "    return 1", "```", "ignored"])
    assert got == "def f():\n    return 1"


def test_eof_at_the_prompt_is_none_and_mid_block_keeps_what_was_typed():
    assert _prompt([EOFError()])[0] is None
    assert _prompt(["```", "partial", EOFError()])[0] == "partial"
    assert _prompt(["open \\", EOFError()])[0] == "open "


def _ev(kind, **payload):
    return DispatchEvent(kind=kind, payload=payload)


def test_pretty_sink_streams_deltas_and_prints_elapsed_status_lines():
    out = io.StringIO()
    sink = pretty.PrettySink(stream=out)
    sink(_ev("turn_start", turn=1, prompt="hi"))
    sink(_ev("assistant_delta", turn=1, text="think"))
    sink(_ev("assistant_delta", turn=1, text="ing"))
    sink(_ev("tool_call_start", tool="Bash", args={"command": "ls"}))
    sink(_ev("assistant_delta", turn=1, text="final answer"))
    sink(_ev("turn_done", turn=1, halted_reason="model_done"))
    text = out.getvalue()
    assert re.search(r"· turn 1 · \d+\.\ds", text)
    assert re.search(r"· Bash · \d+\.\ds", text)
    assert "thinking\n" in text and "final answer\n" in text
    assert sink.streamed_text == "final answer"


def test_pretty_sink_resets_streamed_text_each_turn():
    sink = pretty.PrettySink(stream=io.StringIO())
    sink(_ev("turn_start", turn=1))
    sink(_ev("assistant_delta", text="one"))
    sink(_ev("turn_done", turn=1))
    sink(_ev("turn_start", turn=2))
    sink(_ev("turn_done", turn=2))
    assert sink.streamed_text == ""


def test_path_args_keep_their_tail():
    long_path = "/" + "/".join(f"dir{i}" for i in range(30)) + "/target.py"
    shown = pretty._summarize_args("Read", {"file_path": long_path})
    assert shown.startswith("...") and shown.endswith("/target.py")
    assert len(shown) == 83
    assert pretty._summarize_args("Read", {"file_path": "/short.py"}) == "/short.py"
    long_cmd = "echo " + "x" * 100
    assert pretty._summarize_args("Bash", {"command": long_cmd}).startswith("echo xxx")


def test_banner_lists_every_command():
    out = io.StringIO()
    pretty.print_banner(model="m", session_id="s", stream=out)
    for cmd in ("/help", "/exit", "/autonomy", "/mode", "/plan", "/act",
                "/model", "/rewind", "/compact", "/context", "/diff"):
        assert cmd in out.getvalue()


def test_settings_model_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HARNESS_SETTINGS", raising=False)
    monkeypatch.setattr("coding_harness.core.settings.USER_SETTINGS", tmp_path / "none.json")
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    (project / ".bjorn").mkdir()
    (project / ".bjorn" / "settings.json").write_text(json.dumps({"model": "gpt-oss:20b"}))
    assert load_settings(str(project)).model == "gpt-oss:20b"
    (project / ".bjorn" / "settings.json").write_text(json.dumps({"model": 3}))
    with pytest.raises(SettingsError, match="model"):
        load_settings(str(project))


def test_pretty_sink_says_what_the_turn_waits_on_and_the_total():
    out = io.StringIO()
    sink = pretty.PrettySink(stream=out)
    sink(_ev("turn_start", turn=1, prompt="hi"))
    sink(_ev("model_call_start", step=1, model="gemma4:26b"))
    sink(_ev("checks_baseline_start", checks=["make test"]))
    sink(_ev("model_call_start", step=2, model="gemma4:26b"))
    sink(_ev("turn_done", turn=1, halted_reason="model_done"))
    text = out.getvalue()
    assert text.count("waiting on gemma4:26b") == 1
    assert re.search(r"· baseline: make test · \d+\.\ds", text)
    assert re.search(r"· done · model_done · \d+\.\ds", text)
