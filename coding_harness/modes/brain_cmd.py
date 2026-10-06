"""``bjorn brain``: status, search and index for the configured second brain.

Exports ``run``, ``build_parser`` and the exit codes ``OK``, ``NOT_OK`` and
``MISCONFIGURED``.

``status`` reports the backend, the index file's name, its age and the
library's freshness verdict (in-process only; MCP reports what it can).
``search`` prints rank, tier and path, and snippets only under ``--show``,
so a terminal scrollback or a pasted log carries no note text by default.
``index`` runs the library's indexer in this process and refuses on MCP.
This is the operator's own terminal, so no model tier cap applies, as in
the GUI's Brain tab.
"""
from __future__ import annotations

import argparse
import sys
from typing import Any, Callable

from coding_harness.context.brain import BrainClient, BrainError, MCPBackend
from coding_harness.context.brain_inprocess import UPGRADE_WARNING

OK, NOT_OK, MISCONFIGURED = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    """The ``bjorn brain`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="bjorn brain", description="Inspect, search and rebuild the second-brain index.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="backend, index age and freshness; exit 1 when stale or not ok")
    search = sub.add_parser("search", help="rank, tier and path per hit")
    search.add_argument("query")
    search.add_argument("--top-k", type=int, default=10)
    search.add_argument("--vault", default=None)
    search.add_argument("--show", action="store_true", help="also print each hit's snippet")
    index = sub.add_parser("index", help="re-index in-process (in-process backend only)")
    index.add_argument("--file", default=None, help="index exactly this one file")
    return parser


def _err(message: str) -> None:
    """One line on stderr."""
    print(f"bjorn brain: {message}", file=sys.stderr)


def _age_text(age: float | None) -> str:
    """The index age for a person."""
    return "unknown" if age is None else f"{age:.1f} h"


def _status(client: BrainClient, args: argparse.Namespace) -> int:
    """Print what the backend can tell; 1 when stale or not answering, 2 when misconfigured."""
    print(f"backend: {client.backend_name}")
    if isinstance(client.backend, MCPBackend):
        try:
            client.search("index", top_k=1)
        except BrainError as e:
            print(f"ok: no ({e})")
            return NOT_OK
        print("index age: unknown (MCP does not report it)\nok: yes")
        return OK
    backend: Any = client.backend
    print(f"db: {backend.db_path().name}")
    client.probe()  # an unknown identity or a missing DB is a BrainError: exit 2
    fresh = backend.freshness()
    print(f"index age: {_age_text(fresh.age_hours)}")
    if client.upgrade_pending():
        print(f"upgrade pending: yes ({UPGRADE_WARNING})")
    print(f"ok: {'yes' if fresh.ok else 'no'}")
    for reason in fresh.reasons:
        print(f"  - {reason}")
    return OK if fresh.ok else NOT_OK


def _search(client: BrainClient, args: argparse.Namespace) -> int:
    """Rank, tier and path per hit; snippets only with --show."""
    if args.top_k < 1:
        _err("--top-k must be at least 1")
        return MISCONFIGURED
    hits = client.search(args.query, vault=args.vault, top_k=args.top_k)
    for warning in client.last_warnings:
        _err(f"warning: {warning}")
    if not hits:
        print("no matching notes")
    for rank, hit in enumerate(hits, start=1):
        print(f"{rank:>3}  {hit.sensitivity or 'unlabeled':<12}  {hit.path}")
        if args.show:
            print("     " + " ".join(hit.snippet.split()))
    return OK


def _index(client: BrainClient, args: argparse.Namespace) -> int:
    """Run the library's indexer against this backend's DB and print its counts."""
    if isinstance(client.backend, MCPBackend):
        _err("index runs in-process only; set \"backend\": \"inprocess\" in the brain block")
        return MISCONFIGURED
    backend: Any = client.backend
    try:
        out = backend.index(args.file)
    except BrainError as e:
        _err(f"index failed: {e}")
        return NOT_OK
    for key, value in out.items():
        if key != "path":
            print(f"{key}: {value}")
    return OK


def run(argv: list[str], settings_fn: Callable[[], Any]) -> int:
    """Parse ``argv``, build the client from settings and run one subcommand."""
    args = build_parser().parse_args(argv)
    settings = settings_fn()
    if settings is None:
        return MISCONFIGURED
    if not settings.brain:
        _err("no brain configured; add a brain block to the user settings file")
        return MISCONFIGURED
    try:
        client = BrainClient(settings.brain)
    except BrainError as e:
        _err(str(e))
        return MISCONFIGURED
    try:
        handler = {"status": _status, "search": _search, "index": _index}[args.cmd]
        return handler(client, args)
    except BrainError as e:
        _err(str(e))
        return MISCONFIGURED
    finally:
        client.close()
