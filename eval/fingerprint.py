"""Run fingerprint for the eval (P0-4): which code, model and server produced a result.

The gate compares model digest, Ollama version and sampling-profile hash
between a pin and a live run; the harness sha is recorded only, since it is
the thing under test. Every probe has a short timeout and degrades to
"unknown" instead of raising.

Exports: REPO_ROOT, default_model, model_profile_hash, harness_rev, ollama_version,
model_digest, fingerprint.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from pathlib import Path
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
TIMEOUT_S = 5
UNKNOWN = "unknown"


def default_model() -> str:
    """The harness's own default tag, imported lazily to keep the runner's import light."""
    from coding_harness.modes.print_mode import DEFAULT_MODEL
    return DEFAULT_MODEL


def model_profile_hash(model: str) -> str:
    """Hash of the sampling profile the harness would use for ``model``, imported lazily."""
    from coding_harness.models.profile import profile_hash, resolve_profile
    return profile_hash(resolve_profile(model))


def _git(*args: str) -> str | None:
    """stdout of one git command in the repo root, None on any failure."""
    try:
        proc = subprocess.run(["git", "-C", str(REPO_ROOT), *args],
                              capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def harness_rev() -> tuple[str, bool | str]:
    """(sha, dirty) of the harness checkout; unknown when git is unavailable."""
    sha, status = _git("rev-parse", "HEAD"), _git("status", "--porcelain")
    return sha or UNKNOWN, (bool(status) if status is not None else UNKNOWN)


def _http(url: str, payload: dict | None = None) -> dict:
    """One JSON round-trip under the probe timeout. POST when a payload is given."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = Request(url, data=data, headers={"Content-Type": "application/json"},
                  method="POST" if data is not None else "GET")
    with urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ollama_version(url: str) -> str:
    """Server version string from /api/version."""
    try:
        return str(_http(f"{url}/api/version").get("version") or UNKNOWN)
    except (OSError, ValueError):
        return UNKNOWN


def model_digest(url: str, model: str) -> str:
    """Ollama's digest for the tag when it reports one, else a sha256 of the modelfile text."""
    try:
        info = _http(f"{url}/api/show", {"model": model})
    except (OSError, ValueError):
        return UNKNOWN
    if info.get("digest"):
        return str(info["digest"])
    if not info.get("modelfile"):
        return UNKNOWN
    return "modelfile-sha256:" + hashlib.sha256(str(info["modelfile"]).encode("utf-8")).hexdigest()


def fingerprint(model: str) -> dict:
    """Harness sha and dirty flag, python, Ollama version, model tag, digest and profile hash."""
    sha, dirty = harness_rev()
    url = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
    return {
        "harness_sha": sha, "harness_dirty": dirty, "python": platform.python_version(),
        "ollama_version": ollama_version(url), "model": model,
        "model_digest": model_digest(url, model),
        "profile_hash": model_profile_hash(model),
    }
