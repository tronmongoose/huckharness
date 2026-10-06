"""Base directory for harness state — audit chain, sessions, snapshots.

Single source for the harness state root so the package can be relocated
(HARNESS_META_DIR) without editing call sites. A deployment that keeps its
state elsewhere sets that variable.
"""
from __future__ import annotations

import os
from pathlib import Path


def meta_dir() -> Path:
    """Harness state root. Override with HARNESS_META_DIR."""
    return Path(os.environ.get(
        "HARNESS_META_DIR",
        os.path.expanduser("~/.local/state/bjorn"),
    ))
