"""Terminal input for the REPL: readline history and multi-line prompts.

Exports ``HISTORY_PATH``, ``HISTORY_LENGTH``, ``BLOCK_FENCE``,
``load_history``, ``save_history`` and ``read_prompt``.

A prompt is one line unless it ends in a backslash (the next line continues
it) or is exactly a triple backtick fence (every line up to the closing
fence is the prompt). The prompt glyph is written to stderr so stdout
carries only the model's answers.
"""
from __future__ import annotations

import builtins
import os
import sys
from pathlib import Path
from typing import Callable

try:
    import readline
except ImportError:  # a Python built without readline
    readline = None

HISTORY_PATH = Path("~/.config/bjorn/history")
HISTORY_LENGTH = 1000
BLOCK_FENCE = "```"
CONTINUATION_GLYPH = "... "
_MAX_PROMPT_LINES = 10_000


def _history_file() -> Path:
    return Path(os.path.expanduser(str(HISTORY_PATH)))


def _history_active() -> bool:
    """Only a terminal session reads or writes history: a piped stdin would
    overwrite the file with this process's empty in-memory history."""
    return readline is not None and sys.stdin.isatty()


def load_history() -> None:
    """Read the history file into readline; a missing file is fine."""
    if not _history_active():
        return
    readline.set_history_length(HISTORY_LENGTH)
    try:
        readline.read_history_file(str(_history_file()))
    except OSError:
        pass


def save_history() -> None:
    """Write readline history, creating the parent directory."""
    if not _history_active():
        return
    path = _history_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        readline.write_history_file(str(path))
    except OSError:
        pass


def read_prompt(
    glyph: str,
    *,
    read_line: Callable[[str], str] | None = None,
    stream=None,
) -> str | None:
    """One full prompt (possibly several lines), or None at EOF."""
    if read_line is None:
        read_line = builtins.input
    if stream is None:
        stream = sys.stderr
    stream.write(glyph)
    stream.flush()
    try:
        first = read_line("")
    except EOFError:
        return None
    if first.strip() == BLOCK_FENCE:
        return _read_block(read_line, stream)
    return _read_continued(first, read_line, stream)


def _next_line(read_line: Callable[[str], str], stream) -> str | None:
    stream.write(CONTINUATION_GLYPH)
    stream.flush()
    try:
        return read_line("")
    except EOFError:
        return None


def _read_block(read_line: Callable[[str], str], stream) -> str:
    """Lines after an opening fence, up to the closing fence or EOF."""
    lines: list[str] = []
    for _ in range(_MAX_PROMPT_LINES):
        line = _next_line(read_line, stream)
        if line is None or line.strip() == BLOCK_FENCE:
            break
        lines.append(line)
    return "\n".join(lines)


def _read_continued(first: str, read_line: Callable[[str], str], stream) -> str:
    """Join lines while each ends in a backslash; the backslash is dropped."""
    lines = [first]
    for _ in range(_MAX_PROMPT_LINES):
        if not lines[-1].endswith("\\"):
            break
        lines[-1] = lines[-1][:-1]
        line = _next_line(read_line, stream)
        if line is None:
            break
        lines.append(line)
    return "\n".join(lines)
