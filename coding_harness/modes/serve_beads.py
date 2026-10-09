"""Serve routes for the Fleet work list: read beads issues and act on one.

Exports ``resolve_beads_dir``, ``list_work``, ``handle_get``, ``handle_post``
and ``OPENAPI_PATHS``.

The beads directory is the server's cwd when it holds ``.beads``, else the
user's ``beads_dir`` setting, else none, and then the list says why it is
empty. Every ``bd`` call is an argv list with no shell, runs in that
directory and times out. Ids must match ``ID_RE`` and free text is length
bounded before it reaches argv; user text rides in ``--flag=value`` form so
it can never parse as a flag. Writes are refused with 409 while
``HARNESS_SETTINGS=off``, because then no settings file vouched for the
directory or the operator.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from coding_harness.core.settings import USER_SETTINGS

Reply = tuple[int, dict[str, Any]]
ID_RE = re.compile(r"^[a-z][a-z0-9]*-[a-z0-9.]+$")
ISSUE_TYPES = ("bug", "feature", "task", "epic", "chore", "decision")
BD_TIMEOUT_S = 10
WORK_LIMIT = 200
MAX_ID_CHARS = 64
MAX_TITLE_CHARS = 300
MAX_TEXT_CHARS = 4000
STDERR_TAIL_CHARS = 600
NO_BEADS_REASON = f"no beads here; set beads_dir in {USER_SETTINGS}"
_ACTIONS = ("claim", "close", "note")
_DETAIL_KEYS = ("id", "title", "status", "priority", "issue_type", "description",
                "notes", "acceptance_criteria", "design", "assignee", "owner", "labels",
                "parent", "close_reason", "created_at", "updated_at", "closed_at")
_LINK_KEYS = ("id", "title", "status", "priority", "issue_type", "dependency_type")


def _error(status: int, message: str) -> Reply:
    """The serve error envelope."""
    return status, {"error": {"code": status, "message": message}}


def resolve_beads_dir(cwd: str, beads_dir: str | None) -> Path | None:
    """cwd when it holds .beads, else the expanded setting when it does, else None."""
    if (Path(cwd) / ".beads").is_dir():
        return Path(cwd)
    if beads_dir:
        candidate = Path(os.path.expanduser(beads_dir))
        if (candidate / ".beads").is_dir():
            return candidate
    return None


def _run(argv: list[str], cwd: Path) -> tuple[int, str, str]:
    """(returncode, stdout, stderr) of one bd call; 127/124 when it cannot run or hangs."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=BD_TIMEOUT_S, cwd=str(cwd))
    except OSError as e:
        return 127, "", f"bd did not start: {e}"
    except subprocess.TimeoutExpired:
        return 124, "", f"bd timed out after {BD_TIMEOUT_S}s"
    return proc.returncode, proc.stdout, proc.stderr


def _bd_failed(stderr: str, stdout: str = "") -> Reply:
    """A 404 when bd says the issue is missing, else a 502 with bd's stderr tail."""
    tail = (stderr.strip() or stdout.strip() or "bd failed")[-STDERR_TAIL_CHARS:]
    if "not found" in tail.lower():
        return _error(404, tail)
    return _error(502, tail)


def _parse(stdout: str) -> Any:
    """bd's JSON output, or None when it printed something else."""
    try:
        return json.loads(stdout)
    except ValueError:
        return None


def _work_item(issue: dict[str, Any]) -> dict[str, Any]:
    """One list row; the issue type key is ``issue_type``, never ``type``."""
    return {k: issue.get(k) for k in ("id", "title", "status", "priority",
                                      "issue_type", "updated_at", "assignee")}


def _work_from_bd(status: str, beads: Path) -> list[dict[str, Any]] | None:
    """Live rows from ``bd list``, or None when bd cannot answer."""
    argv = ["bd", "list", "--json", "--limit", str(WORK_LIMIT), "--no-pager"]
    if status == "closed":
        argv += ["--status", "closed"]
    code, out, _ = _run(argv, beads)
    data = _parse(out) if code == 0 else None
    if not isinstance(data, list):
        return None
    return [_work_item(i) for i in data if isinstance(i, dict)]


def _work_from_backup(status: str, beads: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Rows from the JSONL backup bd keeps, and how old it is."""
    backup = beads / ".beads" / "backup" / "issues.jsonl"
    items: list[dict[str, Any]] = []
    try:
        mtime = datetime.fromtimestamp(backup.stat().st_mtime, tz=timezone.utc)
        age = f"{(datetime.now(timezone.utc) - mtime).total_seconds() / 3600:.1f}h"
        with backup.open(encoding="utf-8") as f:
            for line in f:
                issue = json.loads(line) if line.strip() else None
                if isinstance(issue, dict) and issue.get("status") == status:
                    items.append(_work_item(issue))
    except (OSError, ValueError):
        return [], None
    items.sort(key=lambda i: (i.get("priority") if i.get("priority") is not None else 9,
                              i.get("id") or ""))
    return items[:WORK_LIMIT], age


def _writable() -> bool:
    """False while settings are off: writes need a settings file behind them."""
    return os.environ.get("HARNESS_SETTINGS") != "off"


def list_work(status: str, cwd: str, beads_dir: str | None) -> Reply:
    """GET /v1/work: beads issues live from bd, else the backup, else a reason."""
    status = status if status in ("open", "closed") else "open"
    beads = resolve_beads_dir(cwd, beads_dir)
    if beads is None:
        return 200, {"items": [], "status": status, "source": "none",
                     "reason": NO_BEADS_REASON, "writable": False}
    base = {"status": status, "dir": str(beads), "writable": _writable()}
    live = _work_from_bd(status, beads)
    if live is not None:
        return 200, {**base, "items": live, "source": "live"}
    items, age = _work_from_backup(status, beads)
    return 200, {**base, "items": items, "source": "backup", "backup_age": age}


def _link(entry: Any) -> dict[str, Any] | None:
    """A dependency or dependent trimmed to what the drawer shows."""
    return {k: entry.get(k) for k in _LINK_KEYS} if isinstance(entry, dict) else None


def show(bead_id: str, beads: Path) -> Reply:
    """GET /v1/beads/{id}: ``bd show <id> --json`` trimmed to the drawer's fields."""
    code, out, err = _run(["bd", "show", bead_id, "--json"], beads)
    if code != 0:
        return _bd_failed(err, out)
    data = _parse(out)
    issue = data[0] if isinstance(data, list) and data else data
    if not isinstance(issue, dict):
        return _error(502, "bd show printed no issue")
    detail = {k: issue.get(k) for k in _DETAIL_KEYS}
    for key in ("dependencies", "dependents"):
        detail[key] = [x for x in map(_link, issue.get(key) or []) if x]
    return 200, detail


def _text(body: dict[str, Any], key: str, limit: int) -> tuple[str | None, str | None]:
    """(stripped text, None) or (None, why it is refused)."""
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        return None, f"body.{key} must be a non-empty string"
    if len(value) > limit:
        return None, f"body.{key} is longer than {limit} characters"
    return value.strip(), None


def create_argv(body: dict[str, Any]) -> tuple[list[str] | None, str | None]:
    """The ``bd create`` argv for a ``{title, priority, type}`` body, or why not."""
    title, problem = _text(body, "title", MAX_TITLE_CHARS)
    if title is None:
        return None, problem
    priority = body.get("priority", 2)
    if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 4:
        return None, "body.priority must be an integer 0-4"
    issue_type = body.get("type", "task")
    if issue_type not in ISSUE_TYPES:
        return None, f"body.type must be one of {', '.join(ISSUE_TYPES)}"
    return ["bd", "create", f"--title={title}", f"--priority={priority}",
            f"--type={issue_type}", "--json"], None


def action_argv(bead_id: str, action: str,
                body: dict[str, Any]) -> tuple[list[str] | None, str | None]:
    """The bd argv for claim, close or note on one issue, or why not."""
    if action == "claim":
        return ["bd", "update", bead_id, "--claim", "--json"], None
    key = "reason" if action == "close" else "text"
    text, problem = _text(body, key, MAX_TEXT_CHARS)
    if text is None:
        return None, problem
    if action == "close":
        return ["bd", "close", bead_id, f"--reason={text}", "--json"], None
    return ["bd", "update", bead_id, f"--append-notes={text}", "--json"], None


def _write(argv: list[str], beads: Path, bead_id: str | None) -> Reply:
    """Run one bd write and answer what bd printed."""
    code, out, err = _run(argv, beads)
    if code != 0:
        return _bd_failed(err, out)
    result = _parse(out)
    if bead_id is None:
        issue = result[0] if isinstance(result, list) and result else result
        bead_id = issue.get("id") if isinstance(issue, dict) else None
    return 200, {"ok": True, "id": bead_id, "result": result}


def valid_id(raw: str) -> str | None:
    """The decoded id when it matches ``ID_RE``, else None."""
    bead_id = unquote(raw)
    if len(bead_id) > MAX_ID_CHARS or not ID_RE.match(bead_id):
        return None
    return bead_id


def handle_get(path: str, query: dict[str, list[str]], cwd: str,
               beads_dir: str | None) -> Reply | None:
    """Answer GET /v1/work and /v1/beads/{id}; None for any other path."""
    if path == "/v1/work":
        return list_work((query.get("status") or ["open"])[0], cwd, beads_dir)
    if not path.startswith("/v1/beads/"):
        return None
    bead_id = valid_id(path[len("/v1/beads/"):])
    if bead_id is None:
        return _error(400, "invalid bead id")
    beads = resolve_beads_dir(cwd, beads_dir)
    if beads is None:
        return _error(404, NO_BEADS_REASON)
    return show(bead_id, beads)


def handle_post(path: str, body: dict[str, Any], cwd: str,
                beads_dir: str | None) -> Reply | None:
    """Answer POST /v1/beads and /v1/beads/{id}/{claim,close,note}; None otherwise."""
    if path != "/v1/beads" and not path.startswith("/v1/beads/"):
        return None
    bead_id: str | None = None
    if path == "/v1/beads":
        argv, problem = create_argv(body)
    else:
        raw, _, action = path[len("/v1/beads/"):].rpartition("/")
        if action not in _ACTIONS:
            return _error(404, f"not found: {path}")
        bead_id = valid_id(raw)
        if bead_id is None:
            return _error(400, "invalid bead id")
        argv, problem = action_argv(bead_id, action, body)
    if argv is None:
        return _error(400, problem or "invalid body")
    if not _writable():
        return _error(409, "settings disabled; bead writes are off")
    beads = resolve_beads_dir(cwd, beads_dir)
    if beads is None:
        return _error(409, NO_BEADS_REASON)
    return _write(argv, beads, bead_id)


_ID_PARAM = [{"name": "id", "in": "path", "required": True,
              "schema": {"type": "string", "pattern": ID_RE.pattern}}]
_WRITE_RESPONSES = {"200": {"description": "{ok, id, result}: bd's JSON output"},
                    "400": {"description": "invalid id or body"},
                    "404": {"description": "bd has no such issue"},
                    "409": {"description": "HARNESS_SETTINGS=off, or no beads directory"},
                    "502": {"description": "bd failed; message is its stderr tail"}}


def _write_op(summary: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """One OpenAPI POST operation on a single bead."""
    op: dict[str, Any] = {"summary": summary, "parameters": _ID_PARAM,
                          "responses": _WRITE_RESPONSES}
    if props:
        op["requestBody"] = {"content": {"application/json": {"schema": {
            "type": "object", "required": required, "properties": props}}}}
    return {"post": op}


OPENAPI_PATHS: dict[str, Any] = {
    "/v1/work": {"get": {
        "summary": "Beads work items (?status=open|closed)",
        "description": ("Runs bd in cwd when it holds .beads, else in the beads_dir "
                        "setting. source is live, backup or none; none carries a reason."),
        "responses": {"200": {"description": (
            "{items, status, source, writable, dir?, reason?, backup_age?}")}},
    }},
    "/v1/beads": {"post": {
        "summary": "Create a bead with bd create",
        "requestBody": {"content": {"application/json": {"schema": {
            "type": "object", "required": ["title"],
            "properties": {"title": {"type": "string", "maxLength": MAX_TITLE_CHARS},
                           "priority": {"type": "integer", "minimum": 0, "maximum": 4},
                           "type": {"type": "string", "enum": list(ISSUE_TYPES)}},
        }}}},
        "responses": _WRITE_RESPONSES,
    }},
    "/v1/beads/{id}": {"get": {
        "summary": "One bead with notes, dependencies and dependents (bd show)",
        "parameters": _ID_PARAM,
        "responses": {"200": {"description": "bead detail"},
                      "400": {"description": "invalid id"},
                      "404": {"description": "no such bead, or no beads directory"},
                      "502": {"description": "bd failed"}},
    }},
    "/v1/beads/{id}/claim": _write_op("Claim a bead (bd update --claim)", {}, []),
    "/v1/beads/{id}/close": _write_op(
        "Close a bead with a reason", {"reason": {"type": "string"}}, ["reason"]),
    "/v1/beads/{id}/note": _write_op(
        "Append to a bead's notes", {"text": {"type": "string"}}, ["text"]),
}
