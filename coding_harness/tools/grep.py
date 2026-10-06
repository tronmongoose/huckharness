"""Grep tool — regex content search via ripgrep (rg).

Mirrors Claude Code's Grep tool. Uses ripgrep for fast regex search across
files. Falls back to Python's re module + pathlib walk if rg is not installed.
Sentinel-gated via the registry dispatch pipeline.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .base import Tool, ToolResult

MAX_RESULTS = 250
MAX_LINE_CHARS = 500


class Grep(Tool):
    name = "Grep"
    category = "read"
    description = (
        "Search file contents for a regex pattern. Returns matching file paths "
        "by default, or matching lines with context. Uses ripgrep (rg) when "
        "available. Use absolute paths for the search directory."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regex pattern to search for.",
            },
            "path": {
                "type": "string",
                "description": (
                    "Directory or file to search in (absolute path). "
                    "Defaults to current working directory."
                ),
            },
            "glob": {
                "type": "string",
                "description": "Glob pattern to filter files (e.g. '*.py', '*.ts').",
            },
            "output_mode": {
                "type": "string",
                "enum": ["files_with_matches", "content"],
                "description": (
                    "'files_with_matches' returns file paths only (default). "
                    "'content' returns matching lines with line numbers."
                ),
            },
            "context": {
                "type": "integer",
                "description": "Lines of context around each match (content mode only).",
            },
            "case_insensitive": {
                "type": "boolean",
                "description": "Case insensitive search (default: false).",
            },
        },
        "required": ["pattern"],
    }

    def run(self, args: dict[str, Any]) -> ToolResult:
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return ToolResult(content="error: pattern required (string)", is_error=True)

        search_path = args.get("path") or os.getcwd()
        search_path = os.path.expanduser(search_path)
        if not os.path.exists(search_path):
            return ToolResult(
                content=f"error: path not found: {search_path}", is_error=True
            )

        output_mode = args.get("output_mode", "files_with_matches")
        glob_filter = args.get("glob")
        context_lines = int(args.get("context", 0) or 0)
        case_insensitive = bool(args.get("case_insensitive", False))

        # Try ripgrep first, fall back to Python
        try:
            return self._rg_search(
                pattern, search_path, output_mode, glob_filter,
                context_lines, case_insensitive,
            )
        except FileNotFoundError:
            return self._py_search(
                pattern, search_path, output_mode, glob_filter,
                case_insensitive,
            )

    def _rg_search(
        self,
        pattern: str,
        search_path: str,
        output_mode: str,
        glob_filter: str | None,
        context_lines: int,
        case_insensitive: bool,
    ) -> ToolResult:
        cmd = ["rg", "--no-heading", "--color=never"]

        if output_mode == "files_with_matches":
            cmd.append("--files-with-matches")
        else:
            cmd.append("--line-number")
            if context_lines > 0:
                cmd.extend(["-C", str(context_lines)])

        if case_insensitive:
            cmd.append("-i")

        if glob_filter:
            cmd.extend(["--glob", glob_filter])

        cmd.extend(["--", pattern, search_path])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )

        # rg exits 1 for no matches (not an error)
        if result.returncode > 1:
            return ToolResult(
                content=f"error: rg failed: {result.stderr.strip()}",
                is_error=True,
            )

        lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
        total = len(lines)
        truncated = lines[:MAX_RESULTS]

        body = "\n".join(
            line[:MAX_LINE_CHARS] for line in truncated
        )
        if total > MAX_RESULTS:
            body += f"\n# … {total - MAX_RESULTS} more results not shown"

        if not body:
            return ToolResult(content=f"no matches for pattern: {pattern}")

        header = f"# {total} result{'s' if total != 1 else ''} for /{pattern}/\n"
        return ToolResult(
            content=header + body,
            metadata={"total": total, "shown": len(truncated)},
        )

    def _py_search(
        self,
        pattern: str,
        search_path: str,
        output_mode: str,
        glob_filter: str | None,
        case_insensitive: bool,
    ) -> ToolResult:
        """Pure-Python fallback when rg is not installed."""
        flags = re.IGNORECASE if case_insensitive else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return ToolResult(content=f"error: invalid regex: {e}", is_error=True)

        search = Path(search_path)
        if search.is_file():
            files = [search]
        else:
            glob_pat = glob_filter or "**/*"
            # Ensure recursive
            if "**" not in glob_pat:
                glob_pat = "**/" + glob_pat
            files = sorted(search.glob(glob_pat))

        matches: list[str] = []
        for fp in files:
            if not fp.is_file():
                continue
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if output_mode == "files_with_matches":
                if regex.search(text):
                    matches.append(str(fp))
            else:
                for i, line in enumerate(text.split("\n"), 1):
                    if regex.search(line):
                        matches.append(f"{fp}:{i}:{line[:MAX_LINE_CHARS]}")

            if len(matches) >= MAX_RESULTS:
                break

        if not matches:
            return ToolResult(content=f"no matches for pattern: {pattern}")

        body = "\n".join(matches[:MAX_RESULTS])
        total = len(matches)
        header = f"# {total} result{'s' if total != 1 else ''} for /{pattern}/ (python fallback)\n"
        return ToolResult(
            content=header + body,
            metadata={"total": total, "shown": min(total, MAX_RESULTS)},
        )
