"""Write tool — create or overwrite a file on disk.

Mirrors Claude Code's Write tool. Sentinel-gated via the registry
dispatch pipeline (tools/registry.py). Implements ``PlannedTool`` so the
registry can show a unified diff and capture a per-turn snapshot before
the actual disk write.
"""
from __future__ import annotations

import difflib
from pathlib import Path

from .base import PlannedTool, ToolResult, WritePlan, diff_label

MAX_PREVIEW_CHARS = 500
DIFF_CONTEXT = 3


class Write(PlannedTool):
    name = "Write"
    category = "edit"
    description = (
        "Write content to a file. Creates the file if it doesn't exist, "
        "overwrites if it does. Use absolute paths only."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute path to the file to write.",
            },
            "content": {
                "type": "string",
                "description": "The full content to write to the file.",
            },
        },
        "required": ["file_path", "content"],
    }

    def plan(self, args: dict) -> WritePlan | ToolResult:
        raw_path = args.get("file_path")
        content = args.get("content")

        if not isinstance(raw_path, str) or not raw_path:
            return ToolResult(content="error: file_path required (string)", is_error=True)
        if not isinstance(content, str):
            return ToolResult(content="error: content required (string)", is_error=True)

        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            return ToolResult(
                content=f"error: must be absolute path, got {raw_path!r}",
                is_error=True,
            )

        existed = path.exists()
        pre_image: bytes | None = None
        pre_text = ""
        if existed:
            if not path.is_file():
                return ToolResult(
                    content=f"error: not a regular file: {path}", is_error=True,
                )
            try:
                pre_image = path.read_bytes()
                pre_text = pre_image.decode("utf-8", errors="replace")
            except OSError as e:
                return ToolResult(
                    content=f"error: cannot read existing file: {e}", is_error=True,
                )

        post_image = content.encode("utf-8")

        unified = _unified_diff(pre_text, content, diff_label(path), existed=existed)

        lines = _line_count(content)
        action = "overwrote" if existed else "created"
        summary = f"Write {path} ({len(content)} bytes, {lines} lines, {action})"

        return WritePlan(
            tool=self.name,
            file_path=path,
            existed=existed,
            pre_image=pre_image,
            post_image=post_image,
            unified_diff=unified,
            summary=summary,
            metadata={"action": action, "bytes": len(content), "lines": lines},
        )

    def apply(self, plan: WritePlan) -> ToolResult:
        path = plan.file_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return ToolResult(
                content=f"error: cannot create parent directory: {e}",
                is_error=True,
            )
        try:
            path.write_bytes(plan.post_image)
        except OSError as e:
            return ToolResult(content=f"error: write failed: {e}", is_error=True)

        action = plan.metadata.get("action", "wrote")
        content = plan.post_image.decode("utf-8", errors="replace")
        preview = content[:MAX_PREVIEW_CHARS]
        if len(content) > MAX_PREVIEW_CHARS:
            preview += f"\n... [{len(content) - MAX_PREVIEW_CHARS} more chars]"

        note = ""
        if plan.existed and plan.pre_image is not None:
            before = _line_count(plan.pre_image.decode("utf-8", errors="replace"))
            note = (
                f"\nnote: this replaced an existing {before}-line file; "
                f"prefer Edit for changes to existing files"
            )
        return ToolResult(
            content=(
                f"{action} {path} ({len(plan.post_image)} bytes, "
                f"{plan.metadata.get('lines', 0)} lines){note}\n"
                f"--- preview ---\n{preview}"
            ),
            metadata={**plan.metadata, "file": str(path)},
        )


def _line_count(text: str) -> int:
    """Lines in text, counting an unterminated final line."""
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def _unified_diff(old: str, new: str, path: str, *, existed: bool) -> str:
    """Plain-text unified diff. Renderer adds ANSI colors."""
    if not existed:
        # New file: synthesize a "new file" diff so the operator still sees
        # what's being written.
        new_lines = new.splitlines(keepends=False)
        body = "\n".join(f"+{line}" for line in new_lines)
        return (
            f"--- /dev/null\n"
            f"+++ b{path}\n"
            f"@@ new file ({len(new_lines)} lines) @@\n"
            f"{body}"
        )
    diff = difflib.unified_diff(
        old.splitlines(keepends=False),
        new.splitlines(keepends=False),
        fromfile=f"a{path}",
        tofile=f"b{path}",
        n=DIFF_CONTEXT,
        lineterm="",
    )
    return "\n".join(diff)
