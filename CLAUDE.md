# bjorn-harness

Local-first coding agent on Ollama. Distribution `bjorn-harness`, package
`coding_harness`, command `bjorn`. The README is the reference for
architecture, the autonomy ladder, settings, hooks, skills, the model roster
and every `HARNESS_*` variable. Read it before changing behavior. This file
holds the rules that are not derivable from the README or the code.

## Commands

    make install-dev     # pip install -e '.[dev]' (pytest, ruff, pynacl, pyyaml)
    make lint            # ruff over coding_harness/ and eval/*.py
    make test            # ruff + every test file run separately; the CI gate
    make ui              # build the React UI into coding_harness/ui/dist
    make eval            # harness eval, needs Ollama
    make eval-gate       # regression gate vs eval/results/baseline.json
    make audit-verify    # verify the audit chain

Run one test file with `.venv/bin/python -m pytest coding_harness/tests/test_x.py -q`.
`make test` is the gate CI knows. It runs per file, a habit from a SIGPIPE that
killed whole-suite runs; that was `mcp/transport.py` setting SIG_DFL and is fixed,
so `pytest coding_harness/tests/` in one process also works now.

## Hard constraints

- **Stdlib only at runtime.** `dependencies = []` in pyproject.toml stays empty.
  pyyaml and pynacl are optional imports that degrade gracefully. Python 3.9 is
  the floor, so no `match`, no `X | Y` unions in runtime annotations, no 3.10+ stdlib.
- **Every state path comes from `core/paths.py:meta_dir()`.** Sessions, audit
  logs and snapshots never go to a hardcoded home path. `tests/test_state_isolation.py`
  fails otherwise. User config (settings, trust file, login token, memory, REPL
  history) lives under `~/.config/bjorn`. The conftests point `HARNESS_META_DIR`
  at a temp dir and set `HARNESS_SETTINGS=off`, `HARNESS_TRUST_PROJECTS=all`,
  `HARNESS_DONE_GATE=0`, `HARNESS_TARGETED_TESTS=0`.
  A test that needs different switches sets them with `monkeypatch`, never by
  editing the conftest defaults.
- **Two blocklists move together.** `security/policy.py` (in-process Sentinel)
  and `.claude/hooks/sentinel-gate.py` (subprocess fallback) carry the same
  denied-command list. Edit both or neither.
- **Banned model origins are enforced in code.** `BANNED_MODEL_PREFIXES` in
  `models/ollama.py`. Never loosen it. Never add a banned-origin tag to eval
  configs, benchmark rosters or docs.
- **A repository is untrusted input.** Anything a project directory supplies
  that runs code or widens permissions (settings keys in `PRIVILEGED_KEYS`,
  `.mcp.json`, a sentinel hook) is gated on `core/trust.py`. A new such input
  gets the same gate.
- **Fail closed at the gate.** `PreToolUse` hooks and Sentinel deny on timeout
  or crash. Do not add a fallback that allows on error.

## Docs that tests enforce

- Any new `HARNESS_*` variable goes in the README env-var table in the same
  change. `tests/test_env_docs.py` fails otherwise.
- Adding or changing an `eval/tasks/*` task changes the task set. Re-pin with
  `make eval-gate GATE_ARGS=--pin` and commit `eval/results/baseline.json`.
- `eval/tasks/*/repo/` fixtures are meant to be buggy. Never lint or "fix" them.

## Layout, one line each

- `cli.py` entry. `modes/` print, REPL, serve, GUI, worktree. `serve_mode.py`
  only dispatches; each GUI route family lives in its own `serve_<name>.py`
  (plan, changes, board, composer, brain_extra, git, history, settings, auth).
  A new route family gets a new module, not more lines in `serve_mode.py`.
- `core/` the loop: `session.py` (routing, turn setup), `turn_loop.py` (per-step
  work), `turn_guards.py` (done gate, review, paste-echo), `context_budget.py`
  (tool caps, Read elision), `envelope.py`, `done_gate.py`, `verify.py`,
  `review.py`, `settings.py`, `hooks.py`, `paths.py`.
- `security/` Sentinel `policy.py`, command classifier `commands.py`, audit
  chain `audit.py`, snapshots and rewind.
- `tools/` the fixed tool surface, dispatched through `registry.py`.
- `context/` system-prompt assembly: conventions, memory, repo map, skills.
- `models/` Ollama client and per-model profiles. `mcp/` outbound MCP client.
- `ui/` Vite + React + TS. `dist/` is built, not committed.
- `tests/` about 100 files, one per subsystem. `eval/` tasks, runner, gate, evolve.
- `context/recall.py` injects second-brain hits only into the view sent to local
  Ollama, never into history or the transcript. Keep it that way: anything that
  persists recalled text would let a later frontier turn see it.

## Conventions

- Commits: `area: lowercase sentence saying what changed`, body says why.
  Areas in use: gui, serve, eval, tests, skills, audit, review, prompt, repl, bash.
- Ruff rules F, I, UP, B, E, W with E501 ignored. New code is ruff-clean.
- Functions short, files near 300 lines, docstring per module and function.
  The user-level CLAUDE.md code-discipline rules apply here without exception.
- `coding_harness/README.md` describes internals and is partly stale (Sentinel is
  in-process now, tests run via make). Trust the code and the root README over it.

## Related repos

- A deployment may consume this package with `pip install -e` and supply the
  optional `pipelines` router. This repo must keep working without one.
- Sibling worktrees live at `../bjorn-harness-<topic>`. One checkout, one agent.
