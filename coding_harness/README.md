# coding_harness

The import package behind the `bjorn` command. Product page, install, the
autonomy ladder, settings keys and the full env-var reference live in the
root [README](../README.md). This file covers package internals.

## Quick start

```bash
# Default model: mistral-small3.2:latest
python3 -m coding_harness "list the files in the current directory and tell me what kind of project this is"

# Override model (any allowed-origin local tag)
python3 -m coding_harness --model gpt-oss:20b "refactor the imports in main.py"
```

Banned-origin models (see the root README, Model policy) are refused at call time.

Output: streamed events (`tool_call_start`, `tool_call_result`, `sentinel_verdict`,
`audit_entry`) on stderr, final assistant text on stdout.

## Tools

The core tools match Claude Code's parameter names, so prompts and habits carry
over. `tools/registry.py` builds the full surface, which also includes
TodoWrite, Explore and Brain.

| Tool | What it does |
|------|-------------|
| **Read** | Line-numbered file read with offset/limit and truncation |
| **Bash** | Shell command execution with stdout/stderr capture and timeout |
| **Write** | Create or overwrite files (auto-creates parent directories) |
| **Edit** | Exact string replacement in existing files (single or replace-all) |
| **Grep** | Regex content search via ripgrep (falls back to Python re if rg missing) |
| **Glob** | File pattern matching, results sorted by modification time |

## Security

- **Sentinel:** `security/policy.py` reviews every tool call in-process and
  fails closed: blocklist, level gate, category rule, autonomy ladder, then an
  optional deployment hook (`security/hook_adapter.py`) from a trusted
  directory or `SENTINEL_GATE_HOOK`. The bundled
  `.claude/hooks/sentinel-gate.py` carries the same denied-command list for
  hosts that run it as a subprocess hook.
- **Audit:** `security/audit.py` writes hash-chained JSONL to
  `<meta_dir>/audit.jsonl`. Each entry has `prev_hash`
  and `hash`. `verify_chain()` re-walks the file. Tampering is detected.
- **Sessions:** flat append-only JSONL at
  `<meta_dir>/sessions/{session_id}.jsonl`.

## What's NOT wired yet

- RPC mode (LF-delimited JSONL on stdin/stdout, pi-mono compatible)

Auto context compaction IS wired: when history exceeds a char
budget (`core/compaction.py`, default 100k chars), the middle of the
conversation is folded into a local-model summary — system prompt and the
last 2 user turns survive verbatim. The summarizer is always local Ollama
regardless of routing, every compaction lands on the audit hash chain
(`kind == "compaction"`), and all failure paths fail open to an
uncompacted turn. `MAX_TURNS` remains the per-prompt loop safety net.

Frontier backends ARE wired: `models/anthropic.py` (API, cost-capped via
`core/cost.py`) and `models/claude_cli.py` (`claude -p`, text-only). Both need
a deployment router (`pipelines`) to approve a turn. Without one every turn
stays on local Ollama.

## Tests

```bash
make test                                                   # the CI gate
.venv/bin/python -m pytest coding_harness/tests/test_x.py -q # one file
```

Test modules live in `coding_harness/tests/` — one per subsystem (audit chain
+ signing, Sentinel middleware, banned models, envelope + JIT permissions,
serve mode, MCP transports, router, cost governance, tool bodies). `ls
coding_harness/tests/` for the current set; counts drift.
