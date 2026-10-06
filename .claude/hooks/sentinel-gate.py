#!/usr/bin/env python3
"""Minimal default Sentinel gate for standalone bjorn-harness.

Same contract as a deployment gate: JSON on stdin
{"tool_name", "tool_input", "session_id"}; exit 0 = allow, 2 = block; a human
reason on stderr. Unreadable input blocks. This default blocks only clear-cut
destructive shell commands and allows everything else — it has no deployment-specific policy (vault
sensitivity, escalation, alerts). A deployment overrides it by providing its own
``.claude/hooks/sentinel-gate.py`` (resolved first, via cwd) or by setting
SENTINEL_GATE_HOOK.

This file is what Claude Code's own PreToolUse hook runs in this repo. The
harness no longer spawns it: ``coding_harness/security/policy.py`` enforces the
same list in-process (plus the parser-based cwd-escaping rm and protected paths), and
``security/hook_adapter.py`` skips this bundled copy when walking up from cwd.
Keep the two lists in step when editing either.
"""
import json
import re
import sys

# (pattern, reason). The same entries as UNBYPASSABLE then LEVEL_GATED in
# coding_harness/security/policy.py; tests/test_blocklist_parity.py compares them.
_BLOCK = [
    (r">\s*/dev/(?:sd[a-z]|disk\d*)", "raw disk write"),
    (r"\bmkfs(?:\.\w+)?\b", "mkfs (format)"),
    (r"\bdd\b[^|;&]*\bof=/dev/", "dd to a block device"),
    (r":\(\)\s*\{.*\|.*&\s*\}\s*;", "fork bomb"),
    (r"\bsudo\b", "sudo"),
    (r"\b(?:curl|wget)\b[^|]*\|\s*(?:ba|z)?sh\b", "remote script piped to a shell"),
    (
        r"\bgit\s+push\b[^|;&]*\s(?:--force\b|-[a-zA-Z]*f[a-zA-Z]*(?=\s|$))",
        "git push --force",
    ),
    (
        r"\brm\s+(?:-\S+\s+)*(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)(?=\s|$)",
        "recursive rm",
    ),
    (r"\bgit\s+clean\b[^|;&]*\s(?:-[a-zA-Z]*f[a-zA-Z]*|--force)(?=\s|$)", "git clean -f"),
    (r"\bgit\s+reset\b[^|;&]*\s--hard\b", "git reset --hard"),
    (r"\bgit\s+(?:checkout|restore)\b[^|;&]*\s\./?(?=\s|$|[;&|])", "working-tree reset"),
]


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print("BLOCKED (bjorn-harness default gate): unreadable hook input", file=sys.stderr)
        return 2
    if not isinstance(data, dict):
        print("BLOCKED (bjorn-harness default gate): unreadable hook input", file=sys.stderr)
        return 2
    tool = data.get("tool_name", "")
    cmd = (data.get("tool_input") or {}).get("command", "") if tool == "Bash" else ""
    for pattern, reason in _BLOCK:
        if re.search(pattern, cmd):
            print(f"BLOCKED (bjorn-harness default gate): {reason}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
