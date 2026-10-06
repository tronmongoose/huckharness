"""Prompt overlay: an operator-owned text block appended to the base prompt.

HARNESS_PROMPT_OVERLAY names a file whose text is injected right after the
base prompt as "## Operating notes". It exists so a prompt-evolution loop
(eval/evolve.py) can score candidate instruction blocks without editing the
base prompt in code, and so a candidate that wins lands through review as
an ordinary file change. Unset or missing file renders nothing.

Exports: OVERLAY_ENV, OVERLAY_CAP_BYTES, render_overlay.
"""
from __future__ import annotations

import os
from pathlib import Path

OVERLAY_ENV = "HARNESS_PROMPT_OVERLAY"
OVERLAY_CAP_BYTES = 4000


def render_overlay() -> str:
    """The overlay block, or "" when the env var is unset or the file unreadable."""
    path = os.environ.get(OVERLAY_ENV, "").strip()
    if not path:
        return ""
    try:
        text = Path(os.path.expanduser(path)).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not text:
        return ""
    clipped = text.encode()[:OVERLAY_CAP_BYTES]
    body = clipped.decode("utf-8", errors="ignore").strip()
    return f"\n\n## Operating notes\n\n{body}\n"
