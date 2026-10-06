"""Headless result envelope for print mode. Exports: envelope, exit_code, FORMATS."""
from __future__ import annotations

from typing import Any

FORMATS = ("text", "json")
# 0 done, 3 out of time, 4 gave up, 5 broke, 6 operator stopped it.
_CODES = {"model_done": 0, "deadline": 3, "max_turns": 4, "stuck": 4,
          "error": 5, "interrupted": 6}


def envelope(result: Any, *, model: str, autonomy: str, duration_ms: int) -> dict[str, Any]:
    """Machine-readable summary of a finished turn, one object on stdout."""
    return {
        "session_id": result.session_id,
        "halted_reason": result.halted_reason,
        "final_text": result.final_text,
        "turns": result.turns,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "files_changed": list(result.files_changed),
        "checks_passed": result.checks_passed,
        "duration_ms": duration_ms,
        "error": result.error,
        "session_log": str(result.session_log_path),
        "autonomy": autonomy,
        "model": model,
    }


def exit_code(halted_reason: str) -> int:
    """Exit status for a JSON-format run; unknown reasons count as errors."""
    return _CODES.get(halted_reason, 5)
