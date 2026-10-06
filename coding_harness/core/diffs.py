"""Per-turn change views and single-file revert from Snapshotter manifests.

Exports ``shadow_ok``, ``rel``, ``snapshot_files`` and ``revert_from_snapshot``.

The shadow git repo (``core/git.py``) is the primary source for the GUI's
changes view. These helpers rebuild the same ``{path, status, diff,
truncated}`` shape from the per-turn Write/Edit pre-images when the shadow is
off, broken, or holds no checkpoint for the turn asked about (a resumed
session). A file only Bash touched has no pre-image, so it is listed with
status ``unsnapshotted`` and an empty diff.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from coding_harness.core.git import DIFF_CAP
from coding_harness.security.snapshot import Snapshotter
from coding_harness.tools.edit import _unified_diff


def shadow_ok(cwd: str) -> bool:
    """False for a home dir or the filesystem root, where one checkpoint could copy a whole disk."""
    real = os.path.realpath(cwd)
    home = os.path.realpath(os.path.expanduser("~"))
    return real not in (home, os.path.realpath(os.sep))


def rel(path: str, cwd: str) -> str:
    """``path`` relative to ``cwd`` when it sits inside it, else its real absolute form."""
    full = os.path.realpath(path)
    r = os.path.relpath(full, os.path.realpath(cwd))
    return full if r.startswith(os.pardir) else r


def _first_pre_images(snap: Snapshotter, turn: int | None) -> dict[str, tuple[bool, bytes | None]]:
    """Real path -> (existed, pre-image bytes) from the first manifest naming it."""
    out: dict[str, tuple[bool, bytes | None]] = {}
    for m in snap.manifests(turn):
        path = os.path.realpath(str(m.get("path", "")))
        if not m.get("path") or path in out:
            continue
        pre: bytes | None = None
        pre_path = m.get("pre_image_path")
        if m.get("existed") and pre_path:
            try:
                pre = Path(pre_path).read_bytes()
            except OSError:
                pre = None
        out[path] = (bool(m.get("existed")), pre)
    return out


def _file_entry(path: str, existed: bool, pre: bytes | None, cwd: str) -> dict[str, Any] | None:
    """One file's change record, or None when it ends where it started."""
    p = Path(path)
    now = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
    if now is None and not existed:
        return None
    before = (pre or b"").decode("utf-8", errors="replace")
    status = "deleted" if now is None else ("modified" if existed else "added")
    name = rel(path, cwd)
    text = _unified_diff(before, now or "", "/" + name.lstrip("/"))
    if status == "modified" and not text:
        return None
    return {"path": name, "status": status, "diff": text[:DIFF_CAP], "truncated": len(text) > DIFF_CAP}


def snapshot_files(
    snap: Snapshotter, turn: int | None, touched: list[str], cwd: str,
) -> list[dict[str, Any]]:
    """Change records for ``turn`` (None: the whole session) rebuilt from manifests."""
    pres = _first_pre_images(snap, turn)
    files = [
        entry for path, (existed, pre) in sorted(pres.items())
        if (entry := _file_entry(path, existed, pre, cwd)) is not None
    ]
    for path in sorted(set(touched)):
        if os.path.realpath(path) not in pres:
            files.append({
                "path": rel(path, cwd), "status": "unsnapshotted", "diff": "", "truncated": False,
            })
    return files


def revert_from_snapshot(snap: Snapshotter, turn: int | None, path: str) -> bool:
    """Write back ``path``'s first pre-image in ``turn`` (or the session); False when none."""
    target = os.path.realpath(path)
    for m in snap.manifests(turn):
        if os.path.realpath(str(m.get("path", ""))) != target:
            continue
        try:
            if not m.get("existed"):
                Path(target).unlink(missing_ok=True)
                return True
            pre_path = m.get("pre_image_path")
            if not pre_path:
                return False
            Path(target).write_bytes(Path(pre_path).read_bytes())
            return True
        except OSError:
            return False
    return False
