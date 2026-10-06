"""Read tool — line-numbered file read with truncation.

Mirrors Claude Code's Read tool semantics so the Sentinel hook's fast-path
("Read" == always allowed) applies unchanged. M2 will port pi-mono's
``truncate.ts`` for smarter elision; M1 hard-caps at MAX_LINES.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from coding_harness.tools.base import Tool, ToolResult

MAX_LINES = 2000
MAX_LINE_CHARS = 2000


class Read(Tool):
    name = "Read"
    category = "read"
    description = (
        "Read a file from disk and return its contents with line numbers. "
        "Use absolute paths. Output is truncated at 2000 lines and 2000 "
        "characters per line."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute path to the file to read.",
            },
            "offset": {
                "type": "integer",
                "description": "Optional 1-indexed line to start reading from.",
                "default": 1,
            },
            "limit": {
                "type": "integer",
                "description": "Optional max number of lines to return.",
                "default": MAX_LINES,
            },
        },
        "required": ["file_path"],
    }

    def run(self, args: dict[str, Any]) -> ToolResult:
        raw_path = args.get("file_path")
        if not isinstance(raw_path, str) or not raw_path:
            return ToolResult(content="error: file_path required (string)", is_error=True)

        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            return ToolResult(content=f"error: file_path must be absolute, got {raw_path!r}", is_error=True)
        if not path.exists():
            return ToolResult(content=f"error: no such file: {path}", is_error=True)
        if path.is_dir():
            return ToolResult(content=f"error: is a directory: {path}", is_error=True)

        offset = max(1, int(args.get("offset", 1) or 1))
        limit = min(MAX_LINES, max(1, int(args.get("limit", MAX_LINES) or MAX_LINES)))

        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as e:
            return ToolResult(content=f"error reading {path}: {e}", is_error=True)

        total = len(lines)
        start_idx = offset - 1
        end_idx = min(total, start_idx + limit)
        selected = lines[start_idx:end_idx]

        out_lines: list[str] = []
        for i, line in enumerate(selected, start=offset):
            stripped = line.rstrip("\n")
            if len(stripped) > MAX_LINE_CHARS:
                stripped = stripped[:MAX_LINE_CHARS] + f"  … [line truncated, {len(stripped) - MAX_LINE_CHARS} more chars]"
            out_lines.append(f"{i:6d}\t{stripped}")

        header = f"# {path} (lines {offset}-{end_idx} of {total})\n"
        body = "\n".join(out_lines)
        if end_idx < total:
            body += f"\n# … {total - end_idx} more lines not shown (use offset to continue)"
        return ToolResult(content=header + body)
