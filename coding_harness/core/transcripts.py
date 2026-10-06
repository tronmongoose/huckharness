"""Finding session transcripts on disk.

Exports ``transcript_cwd``, ``recent``, ``first_prompt``, ``valid_id``,
``summary``, ``search``, ``custom_title``, ``set_title``, ``removal_plan``,
``delete``, ``TranscriptError`` and ``DeleteFailed``. Shared by the REPL's --continue and the
GUI's history list so there is one directory walk.

A ``<id>.title`` sidecar next to ``<id>.jsonl`` holds an operator-chosen title
that overrides the first prompt. Deleting a session removes its transcript,
the sidecar, and its ``checkpoints/<id>`` and ``shadow/<id>`` trees under
``meta_dir()``, file by file.
"""
from __future__ import annotations

import functools
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

from coding_harness.core import paths
from coding_harness.core import session as session_mod
from coding_harness.core.envelope import envelope_from_state_dict

SCAN_LIMIT = 500
TITLE_MAX = 120
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class TranscriptError(ValueError):
    """A history request that cannot be honored; ``status`` is the HTTP code."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class DeleteFailed(TranscriptError):
    """A delete that stopped part way: what went, and the path that would not."""

    def __init__(self, removed: list[Path], failed: Path, reason: str) -> None:
        super().__init__(500, f"could not remove {failed.name}: {reason}")
        self.removed = removed
        self.failed = failed


def _session_start(path: Path) -> dict[str, Any] | None:
    """A transcript's session_start record, or None when it has none."""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            record = json.loads(f.readline())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("kind") != "session_start":
        return None
    return record


def transcript_cwd(path: Path) -> str | None:
    """The cwd recorded on a transcript's session_start line, if any."""
    record = _session_start(path)
    return record.get("cwd") if record else None


def recent(cwd: str, limit: int, scan_limit: int = SCAN_LIMIT) -> list[Path]:
    """Up to ``limit`` transcripts started in ``cwd``, newest first."""
    sessions_dir = session_mod.SESSIONS_DIR
    if not sessions_dir.is_dir():
        return []
    newest_first = sorted(
        sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True,
    )
    found: list[Path] = []
    for path in newest_first[:scan_limit]:
        record = _session_start(path)
        # A subagent's session (an Explore scout) is part of its parent's
        # turn, not something to reopen or continue on its own.
        if record and record.get("cwd") == cwd and not record.get("subagent"):
            found.append(path)
            if len(found) >= limit:
                break
    return found


def first_prompt(path: Path, max_lines: int = 50) -> str | None:
    """The first user message in a transcript, or None when it never got one."""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(max_lines), f):
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("kind") == "user_message":
                    return str(record.get("content") or "")
    except OSError:
        return None
    return None


def valid_id(session_id: str) -> bool:
    """True when ``session_id`` is safe to build a path from."""
    return isinstance(session_id, str) and bool(_ID_RE.match(session_id))


def _transcript_path(session_id: str) -> Path:
    """The transcript for a validated id; TranscriptError 400 or 404 otherwise."""
    if not valid_id(session_id):
        raise TranscriptError(400, "invalid session id")
    path = session_mod.SESSIONS_DIR / f"{session_id}.jsonl"
    if not path.is_file():
        raise TranscriptError(404, f"no transcript: {session_id}")
    return path


@functools.lru_cache(maxsize=256)
def _scan(path_str: str, mtime_ns: int, size: int) -> dict[str, Any]:
    """One pass over a transcript; keyed on mtime and size so edits re-scan."""
    out: dict[str, Any] = {"started": None, "turns": 0, "prompt": None,
                           "envelope": None, "calls": False, "results": False}
    with open(path_str, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                _absorb(out, rec)
    return out


def _absorb(out: dict[str, Any], rec: dict[str, Any]) -> None:
    """Fold one transcript record into the scan summary."""
    kind = rec.get("kind")
    if kind == "session_start":
        out["started"] = out["started"] or rec.get("ts")
        out["envelope"] = rec.get("envelope")
    elif kind == "user_message":
        out["turns"] += 1
        if out["prompt"] is None:
            out["prompt"] = str(rec.get("content") or "")
    elif kind == "assistant_message" and rec.get("tool_calls"):
        out["calls"] = True
    elif kind == "tool_result":
        out["results"] = True


def _not_resumable(scan: dict[str, Any]) -> str | None:
    """Why a resume would refuse this transcript, with serve's reason codes."""
    if scan["calls"] and not scan["results"]:
        return "transcript_incomplete"
    env = scan["envelope"]
    if isinstance(env, dict):
        try:
            live = envelope_from_state_dict(env).is_live()
        except (KeyError, TypeError, ValueError):
            live = False
        if not live:
            return "envelope_expired"
    return None


def summary(path: Path) -> dict[str, Any]:
    """{first_prompt, started, turns, bytes, resumable, reason_if_not} for a transcript."""
    st = path.stat()
    scan = _scan(str(path), st.st_mtime_ns, st.st_size)
    reason = _not_resumable(scan)
    return {
        "first_prompt": scan["prompt"],
        "started": scan["started"],
        "turns": scan["turns"],
        "bytes": st.st_size,
        "resumable": reason is None,
        "reason_if_not": reason,
    }


def custom_title(session_id: str) -> str | None:
    """The operator's title from the ``.title`` sidecar, if one is set."""
    if not valid_id(session_id):
        return None
    try:
        text = (session_mod.SESSIONS_DIR / f"{session_id}.title").read_text(encoding="utf-8")
    except OSError:
        return None
    return text.strip() or None


def search(cwd: str, q: str, limit: int, scan_limit: int = SCAN_LIMIT) -> list[Path]:
    """Transcripts in ``cwd`` whose first prompt or custom title holds ``q``, newest first."""
    needle = q.strip().lower()
    found: list[Path] = []
    for path in recent(cwd, limit=scan_limit, scan_limit=scan_limit):
        hay = f"{first_prompt(path) or ''}\n{custom_title(path.stem) or ''}".lower()
        if needle in hay:
            found.append(path)
            if len(found) >= limit:
                break
    return found


def set_title(session_id: str, title: Any) -> str | None:
    """Write or (on an empty title) clear the sidecar; returns the stored title."""
    path = _transcript_path(session_id)
    if not isinstance(title, str):
        raise TranscriptError(400, "body.title must be a string")
    visible = "".join(c for c in title if unicodedata.category(c) not in ("Cc", "Cf"))
    clean = " ".join(visible.split())
    if len(clean) > TITLE_MAX:
        raise TranscriptError(400, f"title is longer than {TITLE_MAX} characters")
    sidecar = path.with_suffix(".title")
    if not clean:
        if sidecar.exists():
            os.remove(sidecar)
        return None
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".title-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(clean + "\n")
    os.replace(tmp, sidecar)
    return clean


def _tree(root: Path) -> list[Path]:
    """Everything under ``root`` then ``root``, children before parents; links not followed."""
    if root.is_symlink() or root.is_file():
        return [root]
    if not root.is_dir():
        return []
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        base = Path(dirpath)
        out.extend(base / name for name in filenames)
        out.extend(base / name for name in dirnames if (base / name).is_symlink())
        out.append(base)
    return out


def removal_plan(session_id: str) -> list[Path]:
    """Every path ``delete`` would remove, in removal order.

    The transcript goes last: while any other file stays, the session is still
    listed and a retried delete can finish the job.
    """
    path = _transcript_path(session_id)
    meta = paths.meta_dir()
    plan: list[Path] = []
    for area in ("checkpoints", "shadow", "memory_proposals"):
        plan.extend(_tree(meta / area / session_id))
    for name in (f"{session_id}.md", f"{session_id}.todo.json"):
        plan.extend(_tree(meta / "plans" / name))
    sidecar = path.with_suffix(".title")
    if sidecar.exists():
        plan.append(sidecar)
    plan.append(path)
    return plan


def delete(session_id: str, live: set[str], cwd: str) -> list[Path]:
    """Remove a past session's files by name. Returns what went.

    Refuses a session live in this server and, like resume, one started in
    another project, since that project's server may hold it live.
    """
    if not valid_id(session_id):
        raise TranscriptError(400, "invalid session id")
    if session_id in live:
        raise TranscriptError(409, "session is live; close it before deleting")
    if transcript_cwd(_transcript_path(session_id)) not in (None, cwd):
        raise TranscriptError(409, "session belongs to another project")
    removed: list[Path] = []
    for p in removal_plan(session_id):
        try:
            if p.is_dir() and not p.is_symlink():
                os.rmdir(p)
            else:
                os.remove(p)
        except OSError as e:
            raise DeleteFailed(removed, p, e.strerror or type(e).__name__) from e
        removed.append(p)
    return removed
