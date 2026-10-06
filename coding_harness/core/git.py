"""Shadow git checkpoints of the session work tree.

Exports ``DIFF_CAP``, ``IGNORE_ALWAYS``, ``RestoreReport`` and ``ShadowRepo``.

A ``ShadowRepo`` keeps a private git dir under ``meta_dir()/shadow/<session_id>``
whose work tree is the session cwd, so a checkpoint is one commit of
everything on disk (minus the ignore rules) that never touches the project's
own ``.git``. ``checkpoint`` returns the commit sha, ``restore`` puts the work
tree back at a sha and removes, one by one, every file added since, ``diff``
and ``diff_files`` show what changed since a sha, ``restore_file`` reverts one
path. Every git call has a timeout and fails open: a broken shadow logs
``shadow_error`` and returns None or empty rather than blocking a turn.
Kill switch: ``HARNESS_SHADOW=0``.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from coding_harness.core.paths import meta_dir

IGNORE_ALWAYS = (".git/", ".venv/", "node_modules/", "__pycache__/", ".pytest_cache/", ".ruff_cache/")
DIFF_CAP = 200_000
_TIMEOUT_S = 60
_SHA_RE = re.compile(r"^[0-9a-f]{4,64}$")
_STATUS = {"A": "added", "D": "deleted", "M": "modified", "T": "modified"}
_IDENTITY = {
    "GIT_AUTHOR_NAME": "bjorn-shadow",
    "GIT_AUTHOR_EMAIL": "shadow@bjorn.local",
    "GIT_COMMITTER_NAME": "bjorn-shadow",
    "GIT_COMMITTER_EMAIL": "shadow@bjorn.local",
}
_logger = logging.getLogger(__name__)


@dataclass
class RestoreReport:
    """Files a restore rewrote, files it removed by name, and what failed."""

    restored: list[str]
    removed: list[str]
    errors: list[str]


def _default_log(kind: str, payload: dict[str, Any]) -> None:
    """Fallback sink when no session logger is attached."""
    _logger.warning("%s %s", kind, payload)


class ShadowRepo:
    """Checkpoints of ``cwd`` in a git dir the project never sees."""

    def __init__(
        self,
        session_id: str,
        cwd: str,
        *,
        log: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        """Bind to ``cwd``; nothing touches disk until first use."""
        self.session_id = session_id
        self.cwd = os.path.realpath(cwd)
        self.git_dir = meta_dir() / "shadow" / session_id
        self._log = log or _default_log
        self._ready = False

    def available(self) -> bool:
        """Kill switch off, git on PATH, cwd a writable dir, shadow dir creatable."""
        if os.environ.get("HARNESS_SHADOW", "1") == "0":
            return False
        if shutil.which("git") is None:
            return False
        if not os.path.isdir(self.cwd) or not os.access(self.cwd, os.W_OK):
            return False
        try:
            self.git_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        return True

    def checkpoint(self, label: str) -> str | None:
        """Commit the work tree as ``label``; the sha, or None when the shadow failed."""
        if not self._init():
            return None
        if self._git("add", "-A") is None:
            return None
        if self._git("commit", "-q", "--allow-empty", "-m", label) is None:
            return None
        sha = self._git("rev-parse", "HEAD")
        return sha.strip() if sha else None

    def restore(self, sha: str) -> RestoreReport:
        """Put the work tree back at ``sha`` and remove files added since, by name."""
        if not self._valid_sha(sha):
            return RestoreReport([], [], ["shadow unavailable or bad sha"])
        head = self.checkpoint("restore-point")
        if head is None:
            return RestoreReport([], [], ["restore-point checkpoint failed"])
        added = self._git("diff", "--no-renames", "--name-only", "-z", "--diff-filter=A", sha, head)
        changed = self._git("diff", "--no-renames", "--name-only", "-z", sha, head)
        if added is None or changed is None or self._git("checkout", sha, "--", ".") is None:
            return RestoreReport([], [], [f"restore to {sha[:12]} failed"])
        added_names = [n for n in added.split("\0") if n]
        removed, errors = self._unlink_all(added_names)
        restored = [n for n in changed.split("\0") if n and n not in set(added_names)]
        return RestoreReport(restored, removed, errors)

    def diff(self, sha: str) -> str:
        """Unified diff from ``sha`` to the work tree, capped at DIFF_CAP chars."""
        if not self._valid_sha(sha) or not self._init() or self._git("add", "-A") is None:
            return ""
        out = self._git("diff", sha) or ""
        return out[:DIFF_CAP]

    def diff_files(self, sha: str, end: str | None = None) -> list[dict[str, Any]]:
        """Per-file ``{path, status, diff, truncated}`` from ``sha`` to ``end`` or the work tree."""
        if not self._valid_sha(sha) or not self._init():
            return []
        if end is not None and not self._valid_sha(end):
            return []
        if end is None and self._git("add", "-A") is None:
            return []
        revs = [sha] if end is None else [sha, end]
        listing = self._git("diff", "--no-renames", "--name-status", "-z", *revs)
        if listing is None:
            return []
        fields = [f for f in listing.split("\0") if f]
        files: list[dict[str, Any]] = []
        for code, path in zip(fields[0::2], fields[1::2]):
            text = self._git("diff", *revs, "--", path) or ""
            files.append({
                "path": path,
                "status": _STATUS.get(code[:1], code),
                "diff": text[:DIFF_CAP],
                "truncated": len(text) > DIFF_CAP,
            })
        return files

    def restore_file(self, sha: str, path: str) -> bool:
        """Put one ``path`` back as it was at ``sha``; False when ``sha`` does not explain it.

        A path absent at ``sha`` is unlinked only when the shadow shows it
        added since ``sha``. An ignored file (.env), a path outside the
        checkpoints, or a typo is never deleted.
        """
        target = self._inside(path)
        if target is None or not self._valid_sha(sha) or not self._init():
            return False
        rel = os.path.relpath(target, self.cwd)
        listed = self._git("ls-tree", "--name-only", "-z", sha, "--", rel)
        if listed is None:
            return False
        if listed.strip("\0"):
            return self._git("checkout", sha, "--", rel) is not None
        if self._git("add", "-A") is None:
            return False
        added = self._git("diff", "--no-renames", "--name-only", "-z", "--diff-filter=A", sha, "--", rel)
        if added is None or rel not in added.split("\0"):
            return False
        _, errors = self._unlink_all([rel])
        return not errors

    def _valid_sha(self, sha: str) -> bool:
        """A hex object name, so no argument can pass as a git option."""
        return bool(_SHA_RE.match(sha or ""))

    def _inside(self, path: str) -> str | None:
        """Absolute form of ``path`` when it sits in cwd and outside any .git, else None."""
        full = os.path.realpath(os.path.join(self.cwd, path))
        rel = os.path.relpath(full, self.cwd)
        # Case-folded: on a case-insensitive filesystem ".GIT/HEAD" is .git/HEAD.
        if rel == "." or rel.startswith(os.pardir) or any(p.lower() == ".git" for p in Path(rel).parts):
            return None
        return full

    def _unlink_all(self, names: list[str]) -> tuple[list[str], list[str]]:
        """Remove each named file under cwd; the removed names and the failures."""
        removed: list[str] = []
        errors: list[str] = []
        for name in names:
            try:
                os.unlink(os.path.join(self.cwd, name))
                removed.append(name)
            except FileNotFoundError:
                removed.append(name)
            except OSError as e:
                errors.append(f"{name}: {e}")
        return removed, errors

    def _init(self) -> bool:
        """Create the shadow git dir and its ignore file on first use."""
        if not self.available():
            return False
        if self._ready:
            return True
        try:
            if not (self.git_dir / "HEAD").exists() and self._git("init", "-q") is None:
                return False
            ignore = self.git_dir / "shadow-ignore"
            ignore.write_text(self._ignore_text(), encoding="utf-8")
        except OSError as e:
            self._log("shadow_error", {"step": "init", "error": f"{type(e).__name__}: {e}"})
            return False
        if self._git("config", "core.excludesFile", str(ignore)) is None:
            return False
        self._ready = True
        return True

    def _ignore_text(self) -> str:
        """The target repo's info/exclude lines plus roots a checkpoint never carries."""
        # Work-tree .gitignore files apply natively; info/exclude lives in the
        # target's own git dir, which the shadow does not see, so copy it.
        lines: list[str] = []
        try:
            lines = (Path(self.cwd) / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
        except OSError:
            pass
        return "\n".join(lines + list(IGNORE_ALWAYS)) + "\n"

    def _git(self, *args: str) -> str | None:
        """stdout of one git call against the shadow, or None after a logged failure."""
        env = {**os.environ, **_IDENTITY, "GIT_DIR": str(self.git_dir), "GIT_WORK_TREE": self.cwd}
        # The user's global config may sign commits or point core.hooksPath at
        # hooks meant for their real repos; neither belongs on a checkpoint.
        argv = [
            "git", "--literal-pathspecs", "-c", "commit.gpgsign=false", "-c", f"core.hooksPath={self.git_dir / 'hooks'}",
            "-c", "core.quotePath=false", *args,
        ]
        try:
            proc = subprocess.run(
                argv, cwd=self.cwd, env=env, capture_output=True, text=True, timeout=_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            self._log("shadow_error", {"args": list(args), "error": f"{type(e).__name__}: {e}"})
            return None
        if proc.returncode != 0:
            self._log("shadow_error", {"args": list(args), "stderr": proc.stderr[-500:]})
            return None
        return proc.stdout
