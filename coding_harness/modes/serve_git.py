"""Git panel routes for serve: status, commit, worktree list, create and open.

Every git argv is fixed. Client input reaches one only as a literal pathspec
after ``--`` (commit) or a validated topic (worktree create). The panel never
pushes, never deletes a branch, never forces and never stages a path it was
not given. A commit below MEDIUM parks on the session's permission broker.

Routes:
  GET  /v1/git             -> status (cached 2 s)
  POST /v1/git/commit      {"session_id", "message", "paths"} -> {sha, paths}
  GET  /v1/worktrees       -> {worktrees: [{path, branch, ..., live, current}]}
  POST /v1/worktrees       {"topic"} -> {path, branch}; 409 if the path exists
  POST /v1/worktrees/open  {"path"} -> {origin, status}; listed paths only

Exports: GET_PATHS, POST_PATHS, OPENAPI_PATHS, handle().
"""
from __future__ import annotations

import os
import re
import shlex
import threading
import time
import uuid
from typing import Any

from coding_harness.core import gitinfo
from coding_harness.core.mode import Autonomy
from coding_harness.core.paths import meta_dir
from coding_harness.modes import worktree as worktree_mode

GET_PATHS = frozenset({"/v1/git", "/v1/worktrees"})
POST_PATHS = frozenset({"/v1/git/commit", "/v1/worktrees", "/v1/worktrees/open"})
STATUS_TTL_S = 2.0
COMMIT_TIMEOUT_S = 120.0
MAX_MESSAGE = 20_000
MAX_PATHS = 500
COMMIT_REASON = "git commit from the panel"
_TOPIC = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_RANK = {Autonomy.OFF: 0, Autonomy.LOW: 1, Autonomy.MEDIUM: 2, Autonomy.HIGH: 3}
# Pathspec magic (":/", ":(glob)") would widen a listed path to others.
_LITERAL = ["--literal-pathspecs"]


def _op(summary: str, code: str, what: str) -> dict[str, Any]:
    """One OpenAPI operation."""
    return {"summary": summary, "responses": {code: {"description": what}}}


OPENAPI_PATHS: dict[str, Any] = {
    "/v1/git": {"get": _op("Branch, ahead/behind, staged/unstaged/untracked", "200", "status")},
    "/v1/git/commit": {"post": _op(
        "Commit listed paths: body {session_id, message, paths}; below medium asks the broker",
        "200", "committed")},
    "/v1/worktrees": {
        "get": _op("Worktrees of this repo with live GUI servers", "200", "list"),
        "post": _op("Create ../<repo>-<topic> on branch <topic>: body {topic}", "201", "created"),
    },
    "/v1/worktrees/open": {"post": _op(
        "Find or start the GUI server for a listed worktree: body {path}", "200", "origin")},
}


class _StatusCache:
    """Per-cwd git status memo so a polling panel and header cost one git call."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rows: dict[str, tuple[float, dict[str, Any]]] = {}

    def get(self, cwd: str) -> dict[str, Any]:
        """Status for ``cwd``, at most ``STATUS_TTL_S`` old."""
        now = time.monotonic()
        with self.lock:
            hit = self.rows.get(cwd)
            if hit is not None and now - hit[0] < STATUS_TTL_S:
                return hit[1]
        fresh = gitinfo.status(cwd)
        with self.lock:
            self.rows[cwd] = (now, fresh)
        return fresh

    def drop(self, cwd: str) -> None:
        """Forget ``cwd`` after a write so the next read is fresh."""
        with self.lock:
            self.rows.pop(cwd, None)


# GLOBAL-STATE: one memo per server process, shared by every handler thread.
_CACHE = _StatusCache()


def _clean_paths(cwd: str, raw: Any, known: set[str]) -> tuple[list[str] | None, str | None]:
    """Paths under ``cwd``, outside .git, each a file git status reports as changed.

    A directory is refused even though git would accept it: ``add -- dir``
    stages every file beneath it, which is ``git add -A`` by another name.
    """
    if not isinstance(raw, list) or not raw or len(raw) > MAX_PATHS:
        return None, f"body.paths must be a list of 1 to {MAX_PATHS} strings"
    root = os.path.normpath(cwd)
    out: list[str] = []
    for p in raw:
        if not isinstance(p, str) or not p or "\0" in p:
            return None, "body.paths must hold non-empty strings"
        full = os.path.normpath(os.path.join(root, p))
        rel = os.path.relpath(full, root)
        if rel == "." or rel.startswith(".." + os.sep) or rel == ".." or os.path.isabs(rel):
            return None, f"path outside the project: {p}"
        if ".git" in [part.lower() for part in rel.split(os.sep)]:
            return None, f"path inside .git: {p}"
        if os.path.isdir(full) and not os.path.islink(full):
            return None, f"directories are not committed whole: {p}"
        if rel not in known:
            return None, f"not a changed file: {p}"
        if rel not in out:
            out.append(rel)
    return out, None


def _render(paths: list[str], msg_file: str) -> str:
    """The command the operator approves, exactly as it runs."""
    quoted = " ".join(shlex.quote(p) for p in paths)
    return (f"git --literal-pathspecs add -- {quoted} && "
            f"git --literal-pathspecs commit -F {shlex.quote(msg_file)} -- {quoted}")


def _known_paths(cwd: str) -> set[str]:
    """Every path git status reports, a rename's old side included."""
    st = gitinfo.status(cwd)
    rows = st.get("staged", []) + st.get("unstaged", []) + st.get("untracked", [])
    return {r["path"] for r in rows} | {r["orig"] for r in rows if r.get("orig")}


def _gate(entry: Any, command: str) -> str | None:
    """None when the commit may run; otherwise why not. Parks below MEDIUM."""
    level = entry.session.registry.autonomy
    if _RANK.get(level, 0) >= _RANK[Autonomy.MEDIUM]:
        return None
    # Tool "GitCommit", not "Bash": an allow_always on a Bash request would
    # grant the model an execute scope. This name matches no tool, so a
    # persistent grant from this card stays inert.
    decision = entry.broker.request(tool="GitCommit", args={"command": command},
                                    reason=COMMIT_REASON, kind="permission")
    return None if decision.allowed else f"commit not approved ({decision.decision})"


def _index_snapshot(cwd: str, paths: list[str]) -> str | None:
    """The index entries of ``paths`` in ``--index-info -z`` form, every stage kept."""
    code, out, _ = gitinfo.run_git([*_LITERAL, "ls-files", "-s", "-z", "--", *paths], cwd)
    return out if code == 0 else None


def _restore_index(cwd: str, paths: list[str], snapshot: str) -> None:
    """Put the index entries of ``paths`` back as ``snapshot`` recorded them.

    A mode-0 record drops whatever ``add`` left for a path; the snapshot then
    re-adds the prior entries, so a path new to the index leaves it again and
    a partly staged one gets its old blob back rather than HEAD's.
    """
    drop = "".join(f"0 {'0' * 40}\t{p}\0" for p in paths)
    gitinfo.run_git([*_LITERAL, "update-index", "-z", "--index-info"], cwd,
                    stdin_text=drop + snapshot)


def _run_commit(cwd: str, paths: list[str], msg_file: str) -> tuple[str | None, str | None]:
    """(sha, None) after add then commit of exactly ``paths``; (None, error) else.

    On any failure the index entries of ``paths`` return to their pre-call state.
    """
    snapshot = _index_snapshot(cwd, paths)
    if snapshot is None:
        return None, "git ls-files failed; nothing staged"
    # A staged rename's old side is in neither the tree nor the index, and
    # add refuses it; commit -- <old> still records the delete.
    indexed = {rec.partition("\t")[2] for rec in snapshot.split("\0") if rec}
    to_add = [p for p in paths if p in indexed or os.path.lexists(os.path.join(cwd, p))]
    code, err = 0, ""
    if to_add:
        code, _, err = gitinfo.run_git([*_LITERAL, "add", "--", *to_add], cwd)
    if code == 0:
        code, _, err = gitinfo.run_git([*_LITERAL, "commit", "-F", msg_file, "--", *paths],
                                       cwd, timeout=COMMIT_TIMEOUT_S)
    if code != 0:
        _restore_index(cwd, paths, snapshot)
        return None, f"git commit failed: {err.strip()[:500]}"
    code, out, _ = gitinfo.run_git(["rev-parse", "HEAD"], cwd)
    return (out.strip() if code == 0 else ""), None


def _commit_input(h: Any, state: Any, body: dict[str, Any], query: dict[str, list[str]]) -> Any:
    """The session entry and message for a commit, or None after a 4xx."""
    sid = body.get("session_id") or (query.get("session_id") or [None])[0]
    entry = state.get(sid) if isinstance(sid, str) else None
    if entry is None:
        h._send_error_json(404, f"session not found: {sid}")
        return None
    if entry.broker is None:
        h._send_error_json(400, "commit needs an interactive session")
        return None
    message = body.get("message")
    if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE:
        h._send_error_json(400, f"body.message must be a non-empty string under {MAX_MESSAGE} chars")
        return None
    return entry


def _post_commit(h: Any, state: Any, body: dict[str, Any], query: dict[str, list[str]]) -> None:
    """Stage and commit exactly the listed paths, gated by autonomy and the broker."""
    entry = _commit_input(h, state, body, query)
    if entry is None:
        return
    cwd = os.getcwd()
    paths, problem = _clean_paths(cwd, body.get("paths"), _known_paths(cwd))
    if paths is None:
        h._send_error_json(400, problem or "invalid paths")
        return
    with entry.listeners_lock:
        if entry.turn_active:
            h._send_error_json(409, "a turn is running; commit after it ends")
            return
        # Holds turns off (409) for the whole commit, a parked approval included.
        entry.turn_active = True
    try:
        with entry.lock:
            _commit_locked(h, entry, cwd, paths, body["message"])
    finally:
        with entry.listeners_lock:
            entry.turn_active = False


def _commit_locked(h: Any, entry: Any, cwd: str, paths: list[str], message: str) -> None:
    """Write the message file, ask if needed, commit, emit; the file is always removed."""
    folder = meta_dir() / "git"
    folder.mkdir(parents=True, exist_ok=True)
    msg_file = folder / f"commit-{uuid.uuid4().hex}.txt"
    fd = os.open(msg_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(message)
    try:
        refused = _gate(entry, _render(paths, str(msg_file)))
        if refused is not None:
            h._send_error_json(403, refused)
            return
        sha, error = _run_commit(cwd, paths, str(msg_file))
    finally:
        msg_file.unlink(missing_ok=True)
        _CACHE.drop(cwd)
    if error is not None:
        h._send_error_json(500, error)
        return
    first = message.strip().splitlines()[0][:200]
    entry.session._emit("git_commit", {"sha": sha, "paths": paths, "message_first_line": first})
    h._send_json(200, {"sha": sha, "paths": paths, "message_first_line": first})


def _list_worktrees(cwd: str) -> dict[str, Any]:
    """Worktrees joined with the GUI registry: which have a live server, which is this one."""
    from coding_harness.modes import ui_mode

    found = gitinfo.worktrees(cwd)
    if "error" in found:
        return found
    live = {os.path.realpath(k): v for k, v in ui_mode.live_servers().items()}
    here = os.path.realpath(cwd)
    for row in found["worktrees"]:
        real = os.path.realpath(row["path"])
        row["live"] = live.get(real)
        row["current"] = real == here
    return found


def _post_worktree(h: Any, body: dict[str, Any]) -> None:
    """Create ``../<repo>-<topic>`` on branch ``<topic>`` through modes/worktree."""
    topic = body.get("topic")
    if (not isinstance(topic, str) or not _TOPIC.match(topic) or topic[0] in ".-"
            or ".." in topic or topic.endswith(".lock")):
        h._send_error_json(400, "body.topic must match [A-Za-z0-9._-]{1,64}, not start with . or -")
        return
    try:
        path = worktree_mode.create(topic, os.getcwd())
    except worktree_mode.WorktreeExists as e:
        h._send_error_json(409, str(e))
        return
    except worktree_mode.WorktreeError as e:
        h._send_error_json(400, str(e))
        return
    h._send_json(201, {"path": str(path), "branch": topic})


def _open_worktree(h: Any, state: Any, body: dict[str, Any]) -> None:
    """Find or start the GUI server of a worktree from the current list only."""
    from coding_harness.modes import ui_mode

    target = body.get("path")
    if not isinstance(target, str) or not target:
        h._send_error_json(400, "body.path must be a non-empty string")
        return
    listed = _list_worktrees(os.getcwd()).get("worktrees", [])
    row = next((r for r in listed if r["path"] == target and not r["bare"]), None)
    if row is None:
        h._send_error_json(400, f"not a worktree of this repo: {target}")
        return
    if row["live"]:
        h._send_json(200, {"origin": row["live"], "status": "running"})
        return
    url = ui_mode.spawn(os.path.realpath(target), model=state.model if state.explicit_model else None,
                        force_local=state.force_local)
    if url is None:
        h._send_error_json(502, f"cannot open {target}: did_not_start")
        return
    h._send_json(200, {"origin": url, "status": "started"})


def handle(h: Any, state: Any, method: str, path: str, query: dict[str, list[str]],
           body: dict[str, Any] | None = None) -> None:
    """Dispatch one route from ``GET_PATHS``/``POST_PATHS``."""
    body = body or {}
    cwd = os.getcwd()
    if method == "GET" and path == "/v1/git":
        h._send_json(200, _CACHE.get(cwd))
    elif method == "GET" and path == "/v1/worktrees":
        h._send_json(200, _list_worktrees(cwd))
    elif method == "POST" and path == "/v1/git/commit":
        _post_commit(h, state, body, query)
    elif method == "POST" and path == "/v1/worktrees":
        _post_worktree(h, body)
    elif method == "POST" and path == "/v1/worktrees/open":
        _open_worktree(h, state, body)
    else:
        h._send_error_json(405, f"{method} not allowed on {path}")
