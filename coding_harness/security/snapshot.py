"""Per-turn filesystem snapshot for write-class tool calls.

Every Write/Edit tool call captures the *pre-image* of the target file (or
records that the file did not exist) into a session-scoped checkpoint
directory. ``Session.rewind(n)`` walks the manifests in reverse for the last
``n`` user-prompt turns and restores the disk to the state it had before
those turns, then truncates the conversation history to match.

Layout::

    <meta_dir>/checkpoints/
        {session_id}/
            turn_0001/
                0001.manifest.json
                0001.pre              # raw pre-image bytes (omitted if !existed)
                0002.manifest.json
                0002.pre
            turn_0002/
                ...

Snapshots are append-only within a turn. Restore order within a turn is
reverse-by-seq so the *first* write's pre-image is the one that wins (i.e. if
a turn writes the same path twice, rewinding restores the version that
existed at the start of the turn, not the intermediate state).

Out of scope: snapshotting Bash side effects. Rewind covers Write/Edit only.
"""
from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from coding_harness.core.paths import meta_dir
from coding_harness.tools.base import WritePlan

DEFAULT_ROOT = meta_dir() / "checkpoints"

# Pruned at session close — keep the last N user turns of snapshots, drop older.
RETENTION_TURNS = 10


@dataclass
class SnapshotRef:
    manifest_path: Path
    pre_image_path: Path | None
    turn: int
    seq: int


@dataclass
class RestoreReport:
    turns_rewound: int
    files_restored: int
    files_deleted: int
    errors: list[str]


class Snapshotter:
    """Per-session snapshot store. One instance per ``Session``.

    Cheap by construction: snapshots are bytes-on-disk, no hashing, no diffing
    (the diff is already on the WritePlan from the tool's plan() pass).
    """

    def __init__(self, session_id: str, *, root: Path | None = None) -> None:
        self.session_id = session_id
        self.root = (root or DEFAULT_ROOT) / session_id

    # ── Capture ──────────────────────────────────────────────────────

    def capture(self, plan: WritePlan, *, turn: int, seq: int) -> SnapshotRef:
        turn_dir = self.root / f"turn_{turn:04d}"
        turn_dir.mkdir(parents=True, exist_ok=True)

        pre_image_path: Path | None = None
        if plan.existed and plan.pre_image is not None:
            pre_image_path = turn_dir / f"{seq:04d}.pre"
            pre_image_path.write_bytes(plan.pre_image)

        manifest_path = turn_dir / f"{seq:04d}.manifest.json"
        manifest = {
            "tool": plan.tool,
            "path": str(plan.file_path),
            "existed": plan.existed,
            "pre_image_path": str(pre_image_path) if pre_image_path else None,
            "turn": turn,
            "seq": seq,
            "summary": plan.summary,
        }
        manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

        return SnapshotRef(
            manifest_path=manifest_path,
            pre_image_path=pre_image_path,
            turn=turn,
            seq=seq,
        )

    # ── Restore ──────────────────────────────────────────────────────

    def list_turn_dirs(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(
            (p for p in self.root.iterdir() if p.is_dir() and p.name.startswith("turn_")),
            key=lambda p: p.name,
        )

    def _iter_manifests(self, turn_dir: Path) -> Iterator[dict]:
        for manifest_path in sorted(turn_dir.glob("*.manifest.json")):
            try:
                yield json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

    def restore(self, *, last_n_turns: int) -> RestoreReport:
        """Restore pre-images for the last ``n`` turns (newest first).

        For each manifest entry (in reverse seq order so the first write of a
        turn wins): if the file existed, restore its pre-image bytes; if not,
        delete the file we created (if it still exists).
        """
        turn_dirs = self.list_turn_dirs()
        if not turn_dirs:
            return RestoreReport(0, 0, 0, [])
        target_dirs = turn_dirs[-last_n_turns:]

        files_restored = 0
        files_deleted = 0
        errors: list[str] = []

        # Within a single turn, restore in reverse-seq so the FIRST write's
        # pre-image is the final state. Across turns, walk newest → oldest so
        # multi-turn rewinds end up with the oldest pre-image winning.
        for turn_dir in reversed(target_dirs):
            manifests = list(self._iter_manifests(turn_dir))
            for manifest in reversed(manifests):
                path = Path(manifest["path"])
                try:
                    if manifest["existed"]:
                        pre_path = manifest.get("pre_image_path")
                        if pre_path and Path(pre_path).exists():
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_bytes(Path(pre_path).read_bytes())
                            files_restored += 1
                        else:
                            errors.append(f"missing pre-image for {path}")
                    else:
                        # File didn't exist before this turn — remove it if we
                        # created it. If it's gone already (e.g. already
                        # rewound) treat as no-op.
                        if path.exists():
                            path.unlink()
                            files_deleted += 1
                except OSError as e:
                    errors.append(f"{path}: {e}")

        # After successful restore, delete the snapshot dirs so a second
        # /rewind doesn't replay the same restore.
        for turn_dir in target_dirs:
            shutil.rmtree(turn_dir, ignore_errors=True)

        return RestoreReport(
            turns_rewound=len(target_dirs),
            files_restored=files_restored,
            files_deleted=files_deleted,
            errors=errors,
        )

    def manifests(self, turn: int | None = None) -> list[dict]:
        """Every manifest in capture order, for one ``turn`` or the whole session."""
        dirs = self.list_turn_dirs()
        if turn is not None:
            dirs = [d for d in dirs if d.name == f"turn_{turn:04d}"]
        return [m for d in dirs for m in self._iter_manifests(d)]

    def forget(self, turn: int, path: str) -> int:
        """Drop ``path``'s manifests and pre-images from ``turn``; how many went.

        Called after an operator reverts one file, so a later rewind of this
        turn cannot write back a pre-image the operator already superseded.
        """
        turn_dir = self.root / f"turn_{turn:04d}"
        if not turn_dir.is_dir():
            return 0
        target = os.path.realpath(path)
        dropped = 0
        for manifest_path in sorted(turn_dir.glob("*.manifest.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if os.path.realpath(str(manifest.get("path", ""))) != target:
                continue
            pre = manifest.get("pre_image_path")
            if pre:
                Path(pre).unlink(missing_ok=True)
            manifest_path.unlink(missing_ok=True)
            dropped += 1
        return dropped

    # ── Retention ────────────────────────────────────────────────────

    def prune(self, *, keep: int = RETENTION_TURNS) -> int:
        """Drop turn dirs older than the most recent ``keep``. Returns count
        deleted. Called from Session.close(). Idempotent."""
        turn_dirs = self.list_turn_dirs()
        if len(turn_dirs) <= keep:
            return 0
        old = turn_dirs[:-keep]
        for d in old:
            shutil.rmtree(d, ignore_errors=True)
        return len(old)
