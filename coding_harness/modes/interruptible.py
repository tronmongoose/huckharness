"""Run one session turn on a worker thread so Ctrl-C interrupts it cleanly.

Exports ``run_interruptible``. Print mode and the REPL both call it: the
first Ctrl-C becomes ``session.interrupt()`` (halted_reason=interrupted,
files kept) instead of a traceback through the model read loop.
"""
from __future__ import annotations

import threading
from typing import Any, Callable


def run_interruptible(session: Any, call: Callable[[], Any]) -> Any:
    """Run ``call`` on a worker; a KeyboardInterrupt on the main thread
    interrupts the session and waits for the turn to unwind."""
    box: dict[str, Any] = {}
    # The worker's own signal, not Thread.join: before Python 3.13 a Ctrl-C that
    # lands inside join can mark a running thread as stopped, and the turn's
    # result is then read before it exists.
    done = threading.Event()

    def _target() -> None:
        try:
            box["result"] = call()
        except BaseException as e:  # noqa: BLE001 re-raised on the main thread
            box["error"] = e
        finally:
            done.set()

    threading.Thread(target=_target, daemon=True).start()
    try:
        # UNBOUNDED-LOOP: ends when the turn does; the session's own deadline
        # and Ctrl-C are the kill switches.
        while not done.wait(0.2):
            pass
    except KeyboardInterrupt:
        session.interrupt()
        done.wait()
    if "error" in box:
        raise box["error"]
    return box["result"]
