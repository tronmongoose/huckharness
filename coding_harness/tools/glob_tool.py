"""Glob tool — fast file pattern matching.

Mirrors Claude Code's Glob tool. Finds files by glob pattern and returns
matching paths sorted by modification time (newest first).
Sentinel-gated via the registry dispatch pipeline.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .base import Tool, ToolResult

MAX_RESULTS = 500


class Glob(Tool):
    name = "Glob"
    category = "read"
    description = (
        "Find files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts'). "
        "Returns file paths sorted by modification time (newest first). "
        "Specify an absolute path for the search directory, or omit for cwd."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": (
                    "Glob pattern to match files (e.g. '**/*.py', '*.md', "
                    "'src/**/*.ts'). Use ** for recursive matching."
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "Directory to search in (absolute path). "
                    "Defaults to current working directory."
                ),
            },
        },
        "required": ["pattern"],
    }

    def run(self, args: dict[str, Any]) -> ToolResult:
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return ToolResult(content="error: pattern required (string)", is_error=True)

        search_dir = args.get("path") or os.getcwd()
        search_dir = os.path.expanduser(search_dir)
        base = Path(search_dir)

        if not base.exists():
            return ToolResult(
                content=f"error: directory not found: {search_dir}", is_error=True
            )
        if not base.is_dir():
            return ToolResult(
                content=f"error: not a directory: {search_dir}", is_error=True
            )

        try:
            matches = [p for p in base.glob(pattern) if p.is_file()]
        except (OSError, ValueError) as e:
            return ToolResult(content=f"error: glob failed: {e}", is_error=True)

        # Sort by mtime descending (newest first)
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        total = len(matches)
        shown = matches[:MAX_RESULTS]

        if not shown:
            return ToolResult(content=f"no files matching: {pattern}")

        body = "\n".join(str(p) for p in shown)
        if total > MAX_RESULTS:
            body += f"\n# … {total - MAX_RESULTS} more files not shown"

        header = f"# {total} file{'s' if total != 1 else ''} matching {pattern}\n"
        return ToolResult(
            content=header + body,
            metadata={"total": total, "shown": len(shown)},
        )
