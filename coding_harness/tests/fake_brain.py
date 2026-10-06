"""A stand-in slos-recall MCP server for brain tests: four notes, one per tier.

``block(tmp)`` writes the server script and returns a settings ``brain`` block.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

NOTES = {
    "startup/plan.md": ("internal", "Larkspur launch plan: ship the landing page first."),
    "finance/budget.md": ("confidential", "Larkspur budget: 1200 a month on tools."),
    "health/visit.md": ("restricted", "Larkspur clinic visit notes."),
    "life/unlabeled.md": ("", "Larkspur note with no sensitivity label."),
}

_SOURCE = textwrap.dedent('''
    import json, sys
    NOTES = %r
    def send(m):
        sys.stdout.write(json.dumps(m) + "\\n"); sys.stdout.flush()
    def reply(mid, payload):
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}})
    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line); method = req.get("method"); mid = req.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2024-11-05",
                  "capabilities": {}, "serverInfo": {"name": "fake-recall", "version": "0"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": []}})
        elif method == "tools/call":
            p = req["params"]; args = p.get("arguments") or {}
            if args.get("agent_id") != "bjorn-harness":
                reply(mid, {"error": "unknown agent_id: %%r" %% args.get("agent_id")})
            elif p["name"] == "search":
                q = args.get("query", "").lower()
                hits = [{"page_path": path, "sensitivity": s, "score": 1.0, "chunk_text": t}
                        for path, (s, t) in NOTES.items() if q in t.lower()]
                reply(mid, {"hits": hits[: int(args.get("top_k", 8))]})
            elif p["name"] == "get_page":
                path = args.get("path")
                if path in NOTES:
                    reply(mid, {"path": path, "sensitivity": NOTES[path][0], "content": NOTES[path][1]})
                else:
                    reply(mid, {"error": "not indexed", "path": path})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown"}})
''')


def block(tmp: Path) -> dict:
    """Write the fake server under ``tmp`` and return the matching settings block."""
    script = tmp / "fake_recall.py"
    script.write_text(_SOURCE % (NOTES,), encoding="utf-8")
    return {"command": sys.executable, "args": [str(script)], "agent_id": "bjorn-harness"}
