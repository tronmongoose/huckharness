"""``bjorn`` with no prompt: serve the GUI for the current directory and open it.

Exports ``run``, ``registry_dir``, ``live_servers`` and ``spawn``. Every tool
resolves paths against the process cwd, so the GUI runs as its own server per
project rather than sharing the fleet's :9100 one. Each server records itself
in the registry so another project's GUI can find it instead of starting a
second one for the same tree.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path

from coding_harness.core import paths
from coding_harness.core.settings import Settings
from coding_harness.modes import serve_mode

DEFAULT_UI_PORT = 0
SPAWN_WAIT_S = 20.0


def registry_dir() -> Path:
    """Where live GUI servers record their port, one file per project."""
    return paths.meta_dir() / "gui"


def _entry_path(cwd: str) -> Path:
    digest = hashlib.sha1(cwd.encode("utf-8")).hexdigest()[:10]
    return registry_dir() / f"{Path(cwd).name}-{digest}.json"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def live_servers() -> dict[str, str]:
    """cwd -> URL for every registered GUI server whose process still runs."""
    found: dict[str, str] = {}
    root = registry_dir()
    if not root.is_dir():
        return found
    for path in root.glob("*.json"):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            pid, port, cwd = int(entry["pid"]), int(entry["port"]), str(entry["cwd"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if _alive(pid):
            found[cwd] = f"http://127.0.0.1:{port}"
        else:
            path.unlink(missing_ok=True)  # a crashed server never removed its entry
    return found


# ``python -m`` puts the working directory first on sys.path, so a project
# holding a ``coding_harness`` or ``pipelines`` package would be imported in
# place of the installed one. The child drops cwd from the path and puts it
# back only for a directory the user has trusted (a deployment checkout).
_BOOT = (
    "import os, sys; "
    "sys.path[:] = [p for p in sys.path if p not in ('', os.getcwd())]; "
    "from coding_harness.core import trust; "
    "trust.is_trusted(os.getcwd()) and sys.path.insert(0, os.getcwd()); "
    "from coding_harness.cli import main; "
    "sys.exit(main())"
)


def spawn(cwd: str, *, model: str | None, force_local: bool) -> str | None:
    """Start a detached GUI server rooted at ``cwd``; its URL, or None if it never came up."""
    argv = [sys.executable, "-c", _BOOT, "--no-open", "--port", "0"]
    if model:
        argv += ["--model", model]
    if force_local:
        argv.append("--force-local")
    registry_dir().mkdir(parents=True, exist_ok=True)
    with (registry_dir() / f"{Path(cwd).name}.log").open("ab") as log:
        subprocess.Popen(argv, cwd=cwd, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    deadline = time.monotonic() + SPAWN_WAIT_S
    for _ in range(int(SPAWN_WAIT_S / 0.2) + 1):
        url = live_servers().get(cwd)
        if url or time.monotonic() > deadline:
            return url
        time.sleep(0.2)
    return None


def _interrupt(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def run(
    *,
    model: str,
    explicit_model: bool,
    force_local: bool,
    enable_mcp: bool,
    settings: Settings,
    port: int = DEFAULT_UI_PORT,
    open_browser: bool = True,
) -> int:
    """Serve on loopback, open a tab that starts a fresh session, block until Ctrl-C."""
    cwd = os.getcwd()
    entry = _entry_path(cwd)

    def _ready(server: ThreadingHTTPServer) -> None:
        url = f"http://127.0.0.1:{server.server_port}/?new=1"
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(json.dumps(
            {"pid": os.getpid(), "port": server.server_port, "cwd": cwd}), encoding="utf-8")
        print(f"bjorn: GUI at {url}  (Ctrl-C to stop)", file=sys.stderr, flush=True)
        if open_browser:
            webbrowser.open(url)

    # A plain kill (SIGTERM) would skip the finally below and strand the
    # registry entry; treat it as Ctrl-C, which the server already handles.
    previous = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGTERM, _interrupt)
    try:
        return serve_mode.run(
            host="127.0.0.1",
            port=port,
            model=model,
            force_local=force_local,
            explicit_model=explicit_model,
            enable_mcp=enable_mcp,
            ready_callback=_ready,
            settings=settings,
            brain=True,
        )
    finally:
        entry.unlink(missing_ok=True)
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
