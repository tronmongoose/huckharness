"""Paste-echo detector for Bash dispatch.

Catches the case where a REPL user pastes a CLI invocation as a prompt and
the local model emits that string verbatim as a ``Bash`` tool_call. Sentinel
correctly approves the command (it is not destructive in the rule-based
sense), but the operator never agreed to *run* it — they meant to *describe*
it. Without this gate the harness silently spawns a recursive
``python3 -m coding_harness ...`` that inherits ANTHROPIC_API_KEY and the
audit chain, and the same surface escalates clipboard-injected text.

Detection runs alongside Sentinel, not inside it. Sentinel guards intent
against known-bad patterns; this module guards against the model executing
the user's input as a command they didn't author. Three cheap layers, any
one trips the gate:

1. **Harness self-invocation** — regex match on
   ``python(3)? -m coding_harness``. We want even a deliberate operator
   re-invocation to surface a confirmation; nothing about a recursive
   harness spawn should be quiet.
2. **Verbatim / near-verbatim echo** — normalized SequenceMatcher ratio
   ≥ 0.80. Catches the model adding/removing whitespace or quote style.
3. **Long substring containment** — the normalized prompt (≥12 chars) is a
   substring of the command, or vice versa. Catches the wrapper case where
   the model embeds the prompt in a longer ``bash -c "<prompt>"`` shape.

Inputs shorter than 5 chars short-circuit to "no match" so legitimate tiny
prompts ("ls", "pwd") never collide with the substring rule.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

SIMILARITY_THRESHOLD = 0.80
MIN_PROMPT_LEN = 5
SUBSTRING_MIN_LEN = 12
# Minimum length (of the shorter side) to apply the SequenceMatcher check.
# Short shared phrases like "git status" naturally hit ratio≥0.80 when paired
# with paraphrasing prompts like "show git status" — that's a false positive.
# The actual vulnerability case is verbose pasted invocations (60+ chars), so
# a 20-char floor screens out short collisions without losing the real ones.
SIMILARITY_MIN_LEN = 20

# (?:^|\s) so we don't match inside an unrelated arg like "--foo=python3 -m coding_harness".
_HARNESS_INVOCATION_RE = re.compile(
    r"(?:^|\s)python3?\s+-m\s+coding_harness\b"
)


def _normalize(s: str) -> str:
    """Lowercase, collapse whitespace, strip surrounding quotes.

    Doesn't try to be a shell parser — the goal is to make ``"python3 -m
    coding_harness 'x'"`` match ``python3 -m coding_harness "x"`` without
    over-fitting to any single quoting convention.
    """
    s = s.strip().lower()
    # Collapse runs of whitespace to a single space.
    s = re.sub(r"\s+", " ", s)
    # Drop one layer of surrounding quotes if both ends match.
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    return s


def is_paste_echo(user_prompt: str, command: str) -> tuple[bool, str]:
    """Return ``(matches, reason)``. ``reason`` is empty when ``matches`` is False.

    Pure function. No I/O, no side effects — safe to call anywhere in the
    dispatch path.
    """
    if not user_prompt or not command:
        return False, ""
    if len(user_prompt.strip()) < MIN_PROMPT_LEN:
        return False, ""

    if _HARNESS_INVOCATION_RE.search(command):
        return True, "harness_self_invocation"

    np = _normalize(user_prompt)
    nc = _normalize(command)

    if not np or not nc:
        return False, ""

    if min(len(np), len(nc)) >= SIMILARITY_MIN_LEN:
        ratio = SequenceMatcher(a=np, b=nc).ratio()
        if ratio >= SIMILARITY_THRESHOLD:
            return True, f"verbatim_echo (similarity={ratio:.2f})"

    # Direction matters: vulnerability is "command echoes prompt verbatim",
    # not "prompt happens to mention something the command touches". Reverse
    # containment (e.g. "show git status" containing "git status") is normal
    # paraphrasing — only flag when the user's prompt sits inside the command.
    if len(np) >= SUBSTRING_MIN_LEN and np in nc:
        return True, "prompt_substring_in_command"

    return False, ""
