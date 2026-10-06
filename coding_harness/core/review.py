"""Agentic review pass: a second model grades the change before the session says done.

Exports: ReviewResult, backend, enabled, review_model, review_change.

Local by default. The reviewer is an Ollama model (HARNESS_REVIEW_MODEL,
default mistral-small3.2) that sees the task, one unified diff per edited file
(a new file is shown whole), and the last verify report, and answers with a
JSON verdict. The local backend needs no sensitivity guard: the content never
leaves the machine. When the review tag is not pulled, the session's own coder
stands in (fallback_used=True). The default is mistral-small3.2 because it is
the only local row that clears the reviewer bench (agreement 0.811 with the
Opus label, 0 false-FAIL, every row answered) where granite4.1:8b scores 0.453
and granite4.2:8b cannot render its template on Ollama 0.20.0
(eval/results/review-bench-2026-08-30.json).

HARNESS_REVIEW_BACKEND=claude-cli keeps the Opus reviewer on the Max-plan
claude CLI as an explicit opt-in for A/B. That path keeps the lethal-trifecta
guard: content the router classifies sensitive never reaches the frontier and
approves through (the deterministic gates already ran).

Fail-open on every path. A reviewer error or a reply with no parseable verdict
approves with parse_error=True so it stays countable. Read-only by
construction: the reviewer consumes text and runs no tools. Kill switches:
HARNESS_REVIEW=0 (the fleet's) and HARNESS_REVIEW_BACKEND=off.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import urllib.request
from dataclasses import dataclass
from typing import Any

from coding_harness.models import ollama

DEFAULT_LOCAL_MODEL = "mistral-small3.2"
DEFAULT_CLI_MODEL = "opus"
_MAX_FILE_CHARS = 8000
_TAGS_TIMEOUT_S = 3.0
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
_EMBEDDED_JSON_RE = re.compile(r"\{.*?\}", re.S)

_SYSTEM = (
    "You are a strict senior code reviewer. You answer with ONLY a JSON "
    "object and no other text."
)
_INSTRUCTIONS = (
    "Decide whether the change CORRECTLY and COMPLETELY accomplishes the "
    "task. Check specifically: does it actually do what the task asked; "
    "correctness bugs; off-by-one; unhandled edge cases (None, empty, "
    "duplicates); mutable-default-argument bugs; behavior changed beyond "
    "the task.\n\n"
    "Answer with ONLY a JSON object of this exact shape and nothing else:\n"
    '{"verdict": "APPROVE" | "REVISE", "concerns": "<one sentence naming '
    'the fix needed; empty when APPROVE>"}'
)

# GLOBAL-STATE: the /api/tags probe is cached per process so a long-lived
# serve session pays the HTTP round trip once per review model.
_tag_cache: dict[str, bool] = {}


@dataclass
class ReviewResult:
    """One reviewer verdict; approved=False carries concerns for a repair round."""

    approved: bool
    concerns: str
    backend: str
    model: str
    parse_error: bool = False
    fallback_used: bool = False


def backend() -> str:
    """Reviewer backend from HARNESS_REVIEW_BACKEND: local (default), claude-cli, or off."""
    return os.environ.get("HARNESS_REVIEW_BACKEND", "local").strip().lower() or "local"


def enabled() -> bool:
    """Review runs unless HARNESS_REVIEW=0 or the backend is off."""
    return os.environ.get("HARNESS_REVIEW", "1") != "0" and backend() != "off"


def review_model() -> str:
    """Reviewer model from HARNESS_REVIEW_MODEL with a per-backend default; banned origins refused."""
    if backend() == "claude-cli":
        model = os.environ.get("HARNESS_REVIEW_MODEL", DEFAULT_CLI_MODEL)
        ollama.assert_model_allowed(model)
        return model
    from coding_harness.core.model_roles import role_model

    return role_model("review", check_present=False)


def _new_file_text(diff: str) -> str | None:
    """Whole-file text when the diff is a pure addition (a new file), else None."""
    lines = diff.splitlines()
    if len(lines) < 3 or not lines[2].startswith("@@ -0,0 "):
        return None
    return "\n".join(ln[1:] for ln in lines[3:])


def _file_block(path: str, diff: str) -> str:
    """One prompt block per file: the diff, or the whole text for a new file, capped."""
    text = _new_file_text(diff)
    body = diff if text is None else f"--- {path} (new file) ---\n{text}"
    if len(body) > _MAX_FILE_CHARS:
        body = body[:_MAX_FILE_CHARS] + "\n[truncated]"
    return body


def _prompt(task: str, diffs: dict[str, str], verify_report: str | None) -> str:
    """Task, one block per file, the last verify report, then the JSON-verdict instructions."""
    parts = [f"TASK: {task}", "CHANGES (unified diffs; a new file is shown whole):"]
    parts.extend(_file_block(path, diff) for path, diff in diffs.items())
    if verify_report:
        parts.append(f"LAST VERIFY REPORT (py_compile + ruff):\n{verify_report}")
    parts.append(_INSTRUCTIONS)
    return "\n\n".join(parts)


def _json_verdict(text: str) -> dict[str, Any] | None:
    """The last JSON object carrying a verdict: the whole text, a fenced block, or an embedded one."""
    candidates = [text.strip()]
    candidates.extend(m.group(1) for m in _FENCED_JSON_RE.finditer(text))
    candidates.extend(m.group(0) for m in _EMBEDDED_JSON_RE.finditer(text))
    found = None
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(obj, dict) and "verdict" in obj:
            found = obj
    return found


def _verdict_line(text: str) -> str | None:
    """The last 'VERDICT:' line of a legacy prose reply, or None."""
    verdict = None
    for line in text.splitlines():
        s = line.strip()
        if s.upper().startswith("VERDICT:"):
            verdict = s
    return verdict


def _parse(text: str) -> tuple[bool, str, bool]:
    """(approved, concerns, parse_error); nothing parseable approves with parse_error=True."""
    # A model whose template drops its thinking tags leaks the scratchpad
    # ahead of the answer; only what follows the last close tag is the verdict.
    text = text.rsplit("</think>", 1)[-1]
    # Only the exact token approves: "NOT APPROVED" and "DISAPPROVE" both
    # contain APPROVE, and a substring test would wave them through.
    obj = _json_verdict(text)
    if obj is not None:
        raw = str(obj.get("verdict", ""))
        if _token(raw) == "APPROVE":
            return True, "", False
        if raw.strip():
            return False, str(obj.get("concerns") or raw.strip()), False
    line = _verdict_line(text)
    if line is None:
        return True, "", True
    if _token(line.split(":", 1)[1]) == "APPROVE":
        return True, "", False
    return False, line, False


def _token(raw: str) -> str:
    """A verdict word normalized for comparison: stripped of space and punctuation, upper."""
    return raw.strip().strip(".!,;'\"`*").upper()


def _tag_present(model: str) -> bool:
    """Whether Ollama has ``model`` pulled, via GET /api/tags; a probe error is not cached."""
    if model in _tag_cache:
        return _tag_cache[model]
    try:
        with urllib.request.urlopen(f"{ollama.OLLAMA_URL}/api/tags", timeout=_TAGS_TIMEOUT_S) as resp:
            names = {m.get("name") for m in json.loads(resp.read()).get("models", [])}
    except (OSError, ValueError):
        return False
    present = model in names or f"{model}:latest" in names
    _tag_cache[model] = present
    return present


def _review_local(
    task: str, diffs: dict[str, str], verify_report: str | None, fallback_model: str,
    model: str | None = None,
) -> ReviewResult:
    """Grade via Ollama; the session's coder stands in when the review tag is not pulled."""
    model = model or review_model()
    fallback = not _tag_present(model)
    if fallback:
        # review_model() vetted the configured tag, not this substitute.
        ollama.assert_model_allowed(fallback_model)
        model = fallback_model
    try:
        msg = ollama.chat(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _prompt(task, diffs, verify_report)},
            ],
            tools=None, max_tokens=512, temperature=0.1,
            response_format={"type": "json_object"},
        )
        text = msg.get("content", "") or ""
    except Exception:  # noqa: BLE001 a broken reviewer must not block; the gates already ran
        text = ""
    approved, concerns, parse_error = _parse(text)
    return ReviewResult(approved, concerns, "local", model, parse_error, fallback)


def _review_cli(task: str, diffs: dict[str, str], verify_report: str | None) -> ReviewResult:
    """Grade via the Max-plan claude CLI; sensitive content approves through unsent."""
    from coding_harness.core.router import decide_route
    from coding_harness.models import claude_cli
    model = review_model()
    try:
        decision = decide_route(task + "\n" + "\n".join(diffs.values()))
        if getattr(decision, "sensitivity_flag", False):
            return ReviewResult(True, "", "claude-cli", model)
        decision = dataclasses.replace(
            decision, route="frontier", route_reason="agentic_review"
        )
        msg = claude_cli.chat(
            model=model, decision=decision, tools=[],
            messages=[{"role": "user", "content": _prompt(task, diffs, verify_report)}],
        )
        text = msg.get("content", "") or ""
    except Exception:  # noqa: BLE001 a broken reviewer must not block; the gates already ran
        text = ""
    approved, concerns, parse_error = _parse(text)
    return ReviewResult(approved, concerns, "claude-cli", model, parse_error)


def review_change(
    task: str,
    diffs: dict[str, str],
    verify_report: str | None = None,
    *,
    fallback_model: str,
    sensitive: bool = False,
) -> ReviewResult:
    """Grade the change on the configured backend; empty diffs approve without a call.

    ``sensitive`` is the session's sticky mark: its history (recalled or
    attached confidential notes) shaped these diffs, so the claude-cli
    backend is swapped for the local reviewer rather than trusting the
    router's read of the diff text alone.
    """
    which = backend()
    if not diffs:
        return ReviewResult(True, "", which, "")
    if which == "claude-cli" and sensitive:
        return _review_local(task, diffs, verify_report, fallback_model, DEFAULT_LOCAL_MODEL)
    if which == "claude-cli":
        return _review_cli(task, diffs, verify_report)
    return _review_local(task, diffs, verify_report, fallback_model)
