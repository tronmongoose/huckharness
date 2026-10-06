"""Every harness state path the suite can write sits under the conftest's temp dirs.

The suite once wrote thousands of transcripts and checkpoints into the real
vault. A module-level path computed from anything other than meta_dir() would
escape conftest's HARNESS_META_DIR and do it again; this fails first.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from coding_harness.context import memory
from coding_harness.core import paths, session
from coding_harness.modes import ui_mode
from coding_harness.security import audit, audit_breaks, bash_snapshot, snapshot
from coding_harness.tests.conftest import REAL_HOME


def test_meta_dir_is_a_temp_dir_not_the_vault() -> None:
    assert "bjorn-test-meta-" in str(paths.meta_dir())


def test_state_paths_live_under_the_temp_meta_dir() -> None:
    root = paths.meta_dir().resolve()
    for path in (session.SESSIONS_DIR, snapshot.DEFAULT_ROOT, audit.AUDIT_PATH,
                 audit.ANCHORS_PATH, audit_breaks.BREAKS_PATH, bash_snapshot.DEFAULT_ROOT,
                 ui_mode.registry_dir()):
        assert Path(path).resolve().is_relative_to(root), path


def test_memory_root_is_a_temp_dir() -> None:
    root = memory.memory_root().resolve()
    assert "bjorn-test-memory-" in str(root)
    assert root.is_relative_to(Path(tempfile.gettempdir()).resolve())


def test_no_state_path_is_the_operators_real_config_tree() -> None:
    real = Path(os.path.join(REAL_HOME, ".config", "bjorn")).resolve()
    for path in (memory.memory_root(), memory.memory_dir(os.getcwd()), paths.meta_dir()):
        resolved = Path(path).resolve()
        assert resolved != real and not resolved.is_relative_to(real), path
