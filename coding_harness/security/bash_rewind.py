"""CLI to inspect and restore pre-images captured by bash_snapshot.

Reads from the same ``<meta_dir>/bash-snapshots/``
layout that ``bash_snapshot.snapshot_bash`` writes to.

Usage::

    python -m coding_harness.security.bash_rewind list [--session SID] [--limit N]
    python -m coding_harness.security.bash_rewind show <snap_dir>
    python -m coding_harness.security.bash_rewind restore <snap_dir> [--all] [--yes]

``restore`` walks each target in the manifest and offers to copy its
``.pre`` file back over the current on-disk file. Per-target confirmation
unless ``--yes`` is given; ``--all`` skips the interactive prompt entirely.

Stdlib only — runs under whichever Python the hook itself uses.

Closes bead sl-e24v.
"""
from __future__ import annotations

import argparse
import difflib
import json
import shutil
import sys
from pathlib import Path

from coding_harness.security.bash_snapshot import DEFAULT_ROOT


def _iter_snapshots(root: Path, *, session: str | None = None) -> list[Path]:
    """Return snapshot dirs sorted newest-first."""
    if not root.exists():
        return []
    if session:
        scope = root / session
        if not scope.exists():
            return []
        scopes = [scope]
    else:
        scopes = sorted(p for p in root.iterdir() if p.is_dir())

    snaps: list[Path] = []
    for scope in scopes:
        for snap in scope.iterdir():
            if snap.is_dir() and (snap / "manifest.json").exists():
                snaps.append(snap)
    return sorted(snaps, key=lambda p: p.name, reverse=True)


def _load_manifest(snap_dir: Path) -> dict | None:
    mf = snap_dir / "manifest.json"
    if not mf.exists():
        return None
    try:
        return json.loads(mf.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def cmd_list(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser() if args.root else DEFAULT_ROOT
    snaps = _iter_snapshots(root, session=args.session)
    if not snaps:
        print("No bash snapshots found.")
        return 0
    for snap in snaps[: args.limit]:
        manifest = _load_manifest(snap) or {}
        cmd = (manifest.get("command") or "")[:80]
        captured = sum(1 for t in manifest.get("targets", []) if "pre_image" in t)
        skipped = sum(1 for t in manifest.get("targets", []) if "skipped" in t)
        session = snap.parent.name
        print(f"{snap}\n  session={session}  captured={captured}  skipped={skipped}")
        print(f"  cmd: {cmd}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    snap = Path(args.snap_dir).expanduser().resolve()
    manifest = _load_manifest(snap)
    if manifest is None:
        print(f"error: no manifest at {snap}", file=sys.stderr)
        return 1
    print(json.dumps(manifest, indent=2, sort_keys=True))
    for target in manifest.get("targets", []):
        pre = target.get("pre_image")
        path = target.get("path")
        if not pre or not path or not Path(pre).exists():
            continue
        current = Path(path)
        cur_text = current.read_text(errors="replace") if current.is_file() else ""
        pre_text = Path(pre).read_text(errors="replace")
        diff = difflib.unified_diff(
            pre_text.splitlines(keepends=True),
            cur_text.splitlines(keepends=True),
            fromfile=f"{path} (pre-image)",
            tofile=f"{path} (current)",
            n=3,
        )
        body = "".join(diff)
        if body:
            print(f"\n--- diff: {path} ---")
            sys.stdout.write(body)
    return 0


def _confirm(prompt: str) -> bool:
    try:
        ans = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def cmd_restore(args: argparse.Namespace) -> int:
    snap = Path(args.snap_dir).expanduser().resolve()
    manifest = _load_manifest(snap)
    if manifest is None:
        print(f"error: no manifest at {snap}", file=sys.stderr)
        return 1

    restored = 0
    skipped = 0
    failed: list[str] = []

    for target in manifest.get("targets", []):
        pre = target.get("pre_image")
        path = target.get("path")
        if not pre or not path:
            continue
        pre_p = Path(pre)
        out_p = Path(path)
        if not pre_p.exists():
            failed.append(f"{path}: pre-image missing at {pre}")
            continue
        if not args.all and not args.yes:
            if not _confirm(f"Restore {path} from {pre_p.name}?"):
                skipped += 1
                continue
        try:
            out_p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pre_p, out_p)
            restored += 1
            print(f"restored: {path}")
        except OSError as e:
            failed.append(f"{path}: {e}")

    print(f"\nRestored {restored}, skipped {skipped}, failed {len(failed)}.")
    for f in failed:
        print(f"  fail: {f}")
    return 0 if not failed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bjorn-bash-rewind",
        description="Inspect and restore pre-images captured by the Bash snapshot hook.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List recent snapshots, newest first")
    p_list.add_argument("--session", help="Filter to one session_id")
    p_list.add_argument("--limit", type=int, default=20, help="Max rows (default 20)")
    p_list.add_argument("--root", help="Snapshot root (default: bash_snapshot.DEFAULT_ROOT)")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Show manifest + diff vs current files for a snapshot")
    p_show.add_argument("snap_dir", help="Path to a snapshot directory")
    p_show.set_defaults(func=cmd_show)

    p_restore = sub.add_parser("restore", help="Copy pre-images back over current files")
    p_restore.add_argument("snap_dir", help="Path to a snapshot directory")
    p_restore.add_argument("--all", action="store_true", help="Restore every target without prompting")
    p_restore.add_argument("--yes", action="store_true", help="Same as --all (compat alias)")
    p_restore.set_defaults(func=cmd_restore)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
