"""Read-only ops data for the serve UI: heartbeat routines + beads work list.

Exports: list_routines(), list_work(), list_models(), ollama_status(). Both fail soft — a missing config or
dead Dolt server degrades to an explicit error/source field, never a 500.
No mutations; the UI is a window, not a control plane, for these systems.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from coding_harness.modes import model_probe

try:
    # Resolved via cwd/PYTHONPATH in a deployment; absent standalone.
    from pipelines.heartbeat_render_schedule import _format_cadence
except ImportError:  # standalone: heartbeat renderer unavailable
    def _format_cadence(spec):  # type: ignore[misc]
        sched = spec.get("schedule")
        if sched == "interval":
            for unit, label in (("minutes", "min"), ("seconds", "sec"), ("hours", "hr")):
                if unit in spec:
                    return f"every {spec[unit]} {label}"
            return "interval"
        if sched == "cron":
            hour, minute = spec.get("hour", "?"), spec.get("minute", 0)
            if isinstance(hour, int) and isinstance(minute, int):
                time_str = f"{hour:02d}:{minute:02d}"
            else:
                time_str = f"{hour}:{minute}"
            if spec.get("day_of_week"):
                return f"{spec['day_of_week']} {time_str}"
            if spec.get("day"):
                return f"day {spec['day']} @ {time_str}"
            return f"daily {time_str}"
        return str(sched or "?")

# Optional deployment data: a scheduler's state directory and a beads backup
# in cwd. Both degrade to empty lists when absent.
HEARTBEAT_DIR = Path(os.path.expanduser(os.environ.get("HARNESS_HEARTBEAT_DIR", "~/.config/bjorn/heartbeat")))
BEADS_BACKUP = Path.cwd() / ".beads" / "backup" / "issues.jsonl"
BD_TIMEOUT_S = 5
WORK_LIMIT = 200


def _load_yaml_config() -> dict[str, Any]:
    import yaml
    with (HEARTBEAT_DIR / "config.yaml").open() as f:
        return yaml.safe_load(f) or {}


def _failure_state() -> dict[str, Any]:
    try:
        with (HEARTBEAT_DIR / "failure_state.json").open() as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _scheduler_alive() -> bool:
    try:
        pid = int((HEARTBEAT_DIR / "scheduler.pid").read_text().strip())
        os.kill(pid, 0)
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return False


def list_routines() -> dict[str, Any]:
    """Heartbeat jobs: config.yaml merged with failure state + liveness.

    Last-success and next-run are not tracked anywhere today — omitted
    rather than faked (no invented freshness).
    """
    try:
        config = _load_yaml_config()
    except Exception as e:  # noqa: BLE001 — degrade to explicit error
        return {
            "jobs": [], "total": 0, "failed": 0,
            "scheduler_alive": _scheduler_alive(),
            "error": f"{type(e).__name__}: {e}",
        }

    failures = _failure_state()
    jobs = []
    for job_id, spec in (config.get("jobs") or {}).items():
        fail = failures.get(job_id, {})
        consecutive = int(fail.get("count", 0))
        jobs.append({
            "id": job_id,
            "description": spec.get("description", job_id),
            "cadence": _format_cadence(spec),
            "command": (spec.get("command") or "").strip(),
            "status": "failed" if consecutive > 0 else "ok",
            "consecutive_failures": consecutive,
            "last_failure": fail.get("last_failure"),
            "last_error": fail.get("last_error"),
        })
    jobs.sort(key=lambda j: (0 if j["status"] == "failed" else 1, j["id"]))
    return {
        "jobs": jobs,
        "total": len(jobs),
        "failed": sum(1 for j in jobs if j["status"] == "failed"),
        "scheduler_alive": _scheduler_alive(),
    }


def _work_item(issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": issue.get("id"),
        "title": issue.get("title"),
        "status": issue.get("status"),
        "priority": issue.get("priority"),
        # The backup JSONL key is issue_type; `type` is a known wrong key
        # (a silent miss, not an error).
        "issue_type": issue.get("issue_type"),
        "updated_at": issue.get("updated_at"),
    }


def _work_from_bd(status: str) -> list[dict[str, Any]] | None:
    cmd = ["bd", "list", "--json", "--limit", str(WORK_LIMIT), "--no-pager"]
    if status == "closed":
        cmd += ["--status", "closed"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=BD_TIMEOUT_S,
            cwd=str(Path.cwd()),
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
        return [_work_item(i) for i in data] if isinstance(data, list) else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def _work_from_backup(status: str) -> tuple[list[dict[str, Any]], str | None]:
    items: list[dict[str, Any]] = []
    age: str | None = None
    try:
        mtime = datetime.fromtimestamp(
            BEADS_BACKUP.stat().st_mtime, tz=timezone.utc,
        )
        hours = (datetime.now(timezone.utc) - mtime).total_seconds() / 3600
        age = f"{hours:.1f}h"
        with BEADS_BACKUP.open() as f:
            for line in f:
                if not line.strip():
                    continue
                issue = json.loads(line)
                if issue.get("status") == status:
                    items.append(_work_item(issue))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return [], age
    items.sort(key=lambda i: (i.get("priority") or 9, i.get("id") or ""))
    return items[:WORK_LIMIT], age


def list_models() -> dict[str, Any]:
    """Models a turn may be pinned to (model agility, sl-pb9m).

    Local Ollama tags filtered by the banned-origin list, plus the Max-plan
    claude-cli backend (text-only — tool turns stay local). A dead Ollama
    degrades to just the cli entry rather than an error.
    """
    from coding_harness.models.ollama import OLLAMA_URL, assert_model_allowed

    models: list[dict[str, Any]] = []
    resident = {m["model"] for m in ollama_status()["resident"]}
    try:
        with urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as resp:
            tags = json.loads(resp.read().decode("utf-8")).get("models", [])
        for t in tags:
            name = t.get("name")
            if not name or "embed" in name:
                continue
            try:
                assert_model_allowed(name)
            except Exception:  # noqa: BLE001 — banned origin: omit, not error
                continue
            digest = str(t.get("digest") or "")
            models.append({"id": name, "backend": "ollama", "text_only": False,
                           "loaded": name in resident, "digest": digest,
                           "agent": model_probe.cached(name, digest),
                           "probing": model_probe.probing(name)})
    except OSError:
        pass
    models.sort(key=lambda m: m["id"])
    models.append({
        "id": "claude-cli",
        "backend": "claude-cli",
        "text_only": True,
        "label": "sonnet (Max plan, text-only)",
    })
    return {"models": models}


def ollama_status() -> dict[str, Any]:
    """Models Ollama holds in memory now; a turn on any other model waits for a load."""
    from coding_harness.models.ollama import OLLAMA_URL

    try:
        with urlopen(f"{OLLAMA_URL}/api/ps", timeout=2) as resp:
            loaded = json.loads(resp.read().decode("utf-8")).get("models", [])
    except (OSError, ValueError):
        return {"reachable": False, "resident": []}
    resident = [{
        "model": str(m.get("name") or m.get("model") or "?"),
        "size_gb": round(float(m.get("size") or 0) / 2**30, 1),
        "until": m.get("expires_at"),
    } for m in loaded if isinstance(m, dict)]
    return {"reachable": True, "resident": resident}


def list_work(status: str = "open") -> dict[str, Any]:
    """Beads issues, live from `bd` with a stale-backup fallback."""
    if status not in ("open", "closed"):
        status = "open"
    live = _work_from_bd(status)
    if live is not None:
        return {"items": live, "status": status, "source": "live"}
    items, age = _work_from_backup(status)
    return {
        "items": items, "status": status,
        "source": "backup", "backup_age": age,
    }
