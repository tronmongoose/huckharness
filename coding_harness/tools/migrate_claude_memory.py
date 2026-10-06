"""One-shot migration of Claude Code project memory into bjorn's memory tree.

Copies ``~/.claude/projects/<encoded-path>/memory/*`` (MEMORY.md included)
into ``memory_root()/<repo>/`` (HARNESS_MEMORY_DIR, else
``~/.config/bjorn/memory``) without overwriting anything already
there. The repo slug is the segment after the last ``-projects-`` in the
encoded directory name (Claude encodes the project path with dashes).

Exports: repo_slug_for(name), migrate(src_root, dest_root, dry_run), main(argv).
Console script: ``bjorn-migrate-memory`` (``--dry-run`` lists, copies nothing).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from coding_harness.context.memory import memory_root

CLAUDE_PROJECTS = Path("~/.claude/projects")


def repo_slug_for(name: str) -> str:
    """Repo slug from a Claude project dir name like -Users-x-projects-repo."""
    if "-projects-" in name:
        return name.rsplit("-projects-", 1)[1]
    return name.lstrip("-")


def migrate(src_root: Path, dest_root: Path, *, dry_run: bool) -> list[tuple[str, int]]:
    """Copy each project's memory files; (repo, files copied) per repo."""
    results: list[tuple[str, int]] = []
    if not src_root.is_dir():
        return results
    for project in sorted(src_root.iterdir()):
        memory = project / "memory"
        if not memory.is_dir():
            continue
        slug = repo_slug_for(project.name)
        copied = 0
        for src in sorted(memory.rglob("*")):
            if not src.is_file():
                continue
            dest = dest_root / slug / src.relative_to(memory)
            if dest.exists():
                continue
            if dry_run:
                print(f"would copy {src} -> {dest}")
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
            copied += 1
        results.append((slug, copied))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bjorn-migrate-memory",
        description="Copy Claude Code project memory into bjorn's memory root.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="list the files that would be copied, copy nothing",
    )
    args = parser.parse_args(argv)
    src_root = Path(os.path.expanduser(str(CLAUDE_PROJECTS)))
    dest_root = memory_root()
    results = migrate(src_root, dest_root, dry_run=args.dry_run)
    verb = "would copy" if args.dry_run else "copied"
    for slug, count in results:
        print(f"{slug}: {verb} {count} file(s)")
    if not results:
        print(f"no memory directories under {src_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
