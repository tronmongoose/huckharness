"""Pre-image snapshots for gray-zone destructive Bash commands.

Companion to ``coding_harness/security/snapshot.py``, which captures pre-images
for Write/Edit through the harness ``Session``. This module covers the Bash
side effect gap: commands that mutate files on disk but bypass the WritePlan
pipeline (e.g. ``sed -i``, ``rm <file>``, ``>`` overwrite, ``mv`` clobber).

Hard-destructive patterns (``rm -rf``, ``git reset --hard``, force push,
``drop table``, etc.) are blocked outright by ``.claude/hooks/sentinel-gate.py``
and never reach this code.

Stdlib only — the sentinel-gate hook runs under system ``python3`` with no
venv, and must remain importable from any Python on the machine.

Layout::

    <meta_dir>/bash-snapshots/
        {session_id}/
            {YYYYMMDD-HHMMSS-mmm}_{seq}/
                manifest.json
                {basename}.pre        # one per captured target

Recovery for v1 is manual: ``cp <basename>.pre <original-path>``. A wrapper
CLI lives in ``scripts/bjorn-bash-rewind.py``.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import time
from pathlib import Path

from coding_harness.core.paths import meta_dir

DEFAULT_ROOT = meta_dir() / "bash-snapshots"

# Hard cap on pre-image bytes per target so we never snapshot a multi-GB blob
# into the meta vault. Anything over this is skipped (still recorded in the
# manifest with ``skipped: too_large``).
MAX_PREIMAGE_BYTES = 50 * 1024 * 1024  # 50 MB


# ── Extraction ─────────────────────────────────────────────────────────────
#
# Conservative regexes. A miss (no snapshot) is preferable to a misparse
# (snapshotting the wrong file). Multi-target commands like ``cp a b c dst/``
# only extract the dest; that's a known limitation, not a bug.


_REDIRECT_OVERWRITE = re.compile(r"(?<![>&|])>(?!>)\s*([^\s;|&<>]+)")
_REDIRECT_APPEND = re.compile(r">>\s*([^\s;|&<>]+)")
_TEE = re.compile(r"\btee\b(?:\s+-[^\s]+)*\s+([^\s;|&<>]+)")
_SED_INPLACE = re.compile(r"\bsed\s+-i[^\s]*\s+(?:-[^\s]+\s+|'[^']*'\s+|\"[^\"]*\"\s+)*([^\s;|&<>]+)")


def _extract_rm_targets(cmd: str) -> list[str]:
    """Targets of ``rm`` without -r/-R/-rf. Hard-block path covers recursive."""
    targets: list[str] = []
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        return targets
    i = 0
    while i < len(tokens):
        if tokens[i] == "rm":
            j = i + 1
            while j < len(tokens) and tokens[j].startswith("-"):
                if any(c in tokens[j] for c in ("r", "R")):
                    return []  # recursive — leave to hard-block path
                j += 1
            while j < len(tokens) and tokens[j] not in (";", "&&", "||", "|"):
                if tokens[j].startswith("-"):
                    j += 1
                    continue
                targets.append(tokens[j])
                j += 1
            return targets
        i += 1
    return targets


def _extract_mv_cp_dests(cmd: str) -> list[str]:
    """Final positional arg of ``mv`` / ``cp`` — the destination that gets clobbered."""
    dests: list[str] = []
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        return dests
    i = 0
    while i < len(tokens):
        if tokens[i] in ("mv", "cp"):
            positionals = []
            j = i + 1
            while j < len(tokens) and tokens[j] not in (";", "&&", "||", "|"):
                if not tokens[j].startswith("-"):
                    positionals.append(tokens[j])
                j += 1
            if len(positionals) >= 2:
                dests.append(positionals[-1])
            i = j
        else:
            i += 1
    return dests


def extract_targets(cmd: str, cwd: Path | None = None) -> list[Path]:
    """Return absolute paths of files this command will mutate or destroy.

    Resolves relative paths against ``cwd`` (default: current directory).
    Only existing files are returned — non-existent targets have nothing
    to snapshot.
    """
    base = cwd if cwd is not None else Path.cwd()
    raw: list[str] = []

    for pat in (_REDIRECT_OVERWRITE, _REDIRECT_APPEND, _TEE, _SED_INPLACE):
        raw.extend(pat.findall(cmd))
    raw.extend(_extract_rm_targets(cmd))
    raw.extend(_extract_mv_cp_dests(cmd))

    seen: set[Path] = set()
    out: list[Path] = []
    for r in raw:
        # Strip surrounding quotes that survived a naive regex match.
        r = r.strip().strip("'\"")
        if not r or r.startswith("-") or "\x00" in r:
            continue
        p = Path(os.path.expanduser(r))
        if not p.is_absolute():
            p = (base / p).resolve()
        else:
            p = p.resolve()
        if p in seen:
            continue
        seen.add(p)
        if p.is_file():
            out.append(p)
    return out


# ── Capture ────────────────────────────────────────────────────────────────


def _timestamp_slug() -> str:
    now = time.time()
    ms = int((now - int(now)) * 1000)
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{ms:03d}"


def snapshot_bash(
    cmd: str,
    *,
    cwd: Path | None = None,
    session_id: str = "default",
    root: Path | None = None,
) -> tuple[Path | None, list[Path]]:
    """Capture pre-images for files the command will mutate.

    Returns ``(snapshot_dir, captured_paths)``. If no targets are extracted
    or none of them exist, returns ``(None, [])`` — no directory is created.
    Failures on individual targets are recorded in the manifest with a
    ``skipped`` reason but do not raise.
    """
    targets = extract_targets(cmd, cwd=cwd)
    if not targets:
        return None, []

    snap_root = (root or DEFAULT_ROOT) / session_id
    snap_dir = snap_root / _timestamp_slug()
    snap_dir.mkdir(parents=True, exist_ok=True)

    entries: list[dict] = []
    captured: list[Path] = []
    for target in targets:
        try:
            size = target.stat().st_size
        except OSError as e:
            entries.append({"path": str(target), "skipped": f"stat_error: {e}"})
            continue
        if size > MAX_PREIMAGE_BYTES:
            entries.append({"path": str(target), "skipped": "too_large", "size": size})
            continue
        try:
            data = target.read_bytes()
        except OSError as e:
            entries.append({"path": str(target), "skipped": f"read_error: {e}"})
            continue
        pre_path = snap_dir / (target.name + ".pre")
        # Disambiguate basename collisions by suffixing a counter.
        if pre_path.exists():
            n = 1
            while (snap_dir / f"{target.name}.{n}.pre").exists():
                n += 1
            pre_path = snap_dir / f"{target.name}.{n}.pre"
        try:
            pre_path.write_bytes(data)
        except OSError as e:
            entries.append({"path": str(target), "skipped": f"write_error: {e}"})
            continue
        entries.append({"path": str(target), "pre_image": str(pre_path), "size": size})
        captured.append(target)

    manifest = {
        "session_id": session_id,
        "cwd": str(cwd or Path.cwd()),
        "command": cmd,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "targets": entries,
    }
    (snap_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )

    if not captured:
        # Manifest exists for forensics but nothing was usefully captured.
        return snap_dir, []
    return snap_dir, captured
