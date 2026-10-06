# bjorn-harness

A local-first coding agent that runs on Ollama. The agent loop drives a local
model through a small fixed tool surface (Read, Write, Edit, Bash, Grep, Glob,
plus MCP tools). Every tool call passes an in-process security gate before it
runs. Every dispatch lands in a hash-chained, signed audit log. Edits go
through a verification gate that repairs malformed output and runs the
project's checks before the session says done. A second local model reviews
the diff against the task. No third-party runtime dependencies: stdlib only.
The distribution name is `bjorn-harness`, the import package is
`coding_harness`, and the command is `bjorn`.

## Install

```bash
pipx install -e .        # isolated, puts `bjorn` on PATH
# or
pip install -e .         # into the active venv
bjorn --help
```

Install from a clone. The GUI bundle is built locally and is not committed, so
a plain `pip install .` ships the GUI only if `make ui` ran first.

Requirements:

- Python 3.9+.
- An Ollama server (`OLLAMA_URL`, default `http://localhost:11434`) with the
  default model pulled: `ollama pull mistral-small3.2` (about 15 GB on disk,
  about 19 GB resident). Pass `--model` to use another allowed model.
- Node.js 18+ and npm, only for the GUI. `make ui` builds the bundle once.
  The print and REPL modes need no Node.

Developers use `make install-dev` (adds pytest, ruff, pynacl, pyyaml) and
`make test`.
The second brain is optional. It runs in-process when the separate
`slos_recall` package is importable: `pip install -e '.[brain]'` plus that
package (`make brain-install` installs a sibling `../slos-recall` checkout).
Without it the harness runs unchanged and the brain needs an MCP `command`
(see Settings file).
`python -m coding_harness` still works as the module entry point.

## Quick start

GUI, the default. Run it from the project you want to work in:

```bash
make ui                          # once, and after UI changes: builds the bundle
bjorn                            # serves the GUI for cwd and opens a browser tab
bjorn --model gpt-oss:20b        # start sessions pinned to a model
bjorn --no-open --port 9300      # print the URL instead, on a fixed port
```

The second brain from a terminal (exit 0 ok, 1 stale or not ok, 2
misconfigured):

```bash
bjorn brain status               # backend, index file, age, freshness verdict
bjorn brain search "launch plan" # rank, tier, path; add --show for snippets
bjorn brain index                # full re-index in-process; --file PATH for one note
```

Each `bjorn` is its own loopback server rooted at the current directory,
because every tool resolves paths against the process cwd. The tab opens a
new session.

- **Status line.** While a turn runs it says what the turn waits on: the
  test baseline, a model load (and which models Ollama holds instead), a
  tool, your approval, or generation with tok/s.
- **Model.** Switch it from the header between turns, or type
  `/model <tag|auto>`. `auto` hands the choice back to the router. Loaded
  models are marked. Each turn's footer names the model that answered and
  who picked it.
- **Skills.** The rail lists the skills in the model's prompt, from
  `~/.config/bjorn/skills`, `~/.claude/skills`, `~/.agents/skills` and the
  project. A click starts `/skill <name> <task>`.
- **Context.** The header meter shows the last prompt against the window.
  `/compact` folds older history.
- **History.** "Earlier" lists past sessions from this project with date and
  turn count. The search box matches the first prompt and any title you set.
  The pencil renames a session (a `<id>.title` sidecar next to the
  transcript). Delete asks inline, then removes the transcript, the title and
  the session's `checkpoints/<id>` and `shadow/<id>` trees under the state
  dir, file by file, transcript last. A session from another project is
  refused, as resume refuses it. A delete that stops part way answers 500
  with what went and the path that did not, and a retry finishes it. A session that cannot resume (lapsed envelope,
  incomplete transcript) says so in its row. Opening one replays its thread
  under a dismissible "Resumed from <date>, N turns" banner. Routes:
  `GET /v1/transcripts?q=`, `POST /v1/transcripts/{id}/title {"title"}` (120
  chars, empty clears), `POST /v1/transcripts/{id}/delete` (409 while live
  or from another project).
- **Settings view.** The Settings tab edits the user settings file:
  `autonomy`, `model`, the four command and path lists and `denyWrite`.
  `brain` and `hooks` name commands the server spawns and `sandbox` is
  passed through unchecked, so they show read-only and stay hand-edited.
  The project file is read-only too. Under `HARNESS_SETTINGS=off` saving is
  disabled (409). A save
  goes through the same validation as the loader, refuses banned-origin
  models, writes atomically at mode 0600 and applies to sessions started
  after it. Routes: `GET /v1/settings`, `PUT /v1/settings {"user": {...}}`;
  writable keys left out of the PUT are cleared, the rest are kept from disk.
- **Projects.** The header dropdown lists the git repos in `~/projects`.
  Picking one starts or reuses that project's server and moves the tab.
- Tool calls expand to show the full command, diff and output. Calls
  outside the project pause on an inline approval card. Esc or `stop`
  interrupts after the current step. A reload replays the thread.

**Changes and rewind.** Serve sessions take a shadow git checkpoint of the
work tree at every turn start and end (never in `~` or `/`). Each finished
turn shows "N files changed". A click opens the changes tab with that turn's
per-file diffs. Reject puts one file back as it was before the turn and tells
the model. Rewind undoes the latest turn on disk and in history, Bash side
effects included. "Review writes before apply" parks every Write and Edit on
an inline diff card: Apply, Skip, or Apply all to end review. Routes, the
first three 409 while a turn runs: `GET /v1/sessions/{id}/diff?turn=N` (omit `turn` for
the whole session), `POST .../rewind {"turns": n}`,
`POST .../revert-file {"path", "turn"}`, `POST .../review {"enabled": bool}`.

The test baseline runs only before a turn's first file-changing call, so a
question never waits on the suite. A Makefile `test:` target is preferred
over bare pytest.

REPL, multi-turn, confirms out-of-level commands interactively:

```bash
bjorn -i
bjorn -i --model gpt-oss:20b     # any allowed-origin local tag
bjorn -i -v                      # raw JSONL events on stderr
```

Print mode, one prompt, events on stderr, final text on stdout:

```bash
bjorn "list the files here and tell me what kind of project this is"
bjorn --dry-run "rename foo to bar"          # refuse every write, feed the diff back
bjorn --max-time 600 --check "make test" "fix the failing test"
```

Headless exec with a JSON result envelope on stdout:

```bash
bjorn --output-format json "add a docstring to cli.main"
```

| Exit code | Meaning |
|-----------|---------|
| 0 | done |
| 3 | deadline (`--max-time` ran out, changed files are reported, not discarded) |
| 4 | max_turns or stuck |
| 5 | error |
| 6 | interrupted |

Serve mode, HTTP API with an SSE stream and OpenAPI:

```bash
bjorn serve --port 9100          # --host, --model, --no-mcp, --ui-origin, --force-local
bjorn serve --port 0             # pick any free port
```

### Reaching the GUI over Tailscale

A plain loopback request needs no login, so `bjorn gui` and local scripts are
unaffected. Anything the network can reach needs a session cookie on every
`/v1` route: a non-loopback `--host`, or a loopback request that carries a
reverse-proxy header (`X-Forwarded-For`, `Forwarded`, `X-Real-IP`,
`Tailscale-User-Login`). The app shell still loads and shows a login screen.

```bash
bjorn serve --port 9100            # default loopback bind
tailscale serve --bg 9100          # https on your tailnet name, proxies to 127.0.0.1:9100
```

Open `https://<machine>.<tailnet>.ts.net`, then paste the contents of
`~/.config/bjorn/auth_token` into the login screen. The file is written with
mode 0600 on the first proxied request, or at startup for a non-loopback
`--host` (which prints its path, never the token). Delete it and restart to
rotate. Binding a tailnet address directly (`--host 100.x.y.z`) also works and
also requires the token.

`POST /v1/auth {"token"}` takes `Content-Type: application/json` and at most
4096 bytes. It sets `bjorn_session`: HttpOnly, SameSite=Strict, Path=/, 30
days, and Secure when the request came over https or with
`X-Forwarded-Proto: https` from the proxy. Sessions live in server memory, so
a restart logs everyone out. `POST /v1/auth/logout` ends one. Five wrong tokens
lock the sender out for 60 s. Behind `tailscale serve` the lockout keys on the
`Tailscale-User-Login` the proxy adds; elsewhere on the client address.
`GET /v1/auth/status` answers `{"required", "authenticated"}` for the calling
request without a cookie. When a login is required it adds `token_path_hint`,
the token file's parent directory and name only, for the login screen. `HARNESS_AUTH=0` turns the check off everywhere and
prints a warning at startup on a non-loopback bind.

Flags shared by print and REPL: `--model`, `--force-local`, `--no-mcp`,
`--autonomy`, `--mode` (deprecated alias: plan = off, act = low). A banned
model name is refused before any session starts (exit 2).

## Autonomy ladder

`--autonomy {off,low,medium,high}` sets how much a session may do without
asking. A settings file can set the default. LOW is the default.

| Level | Tool surface | Shell commands allowed without asking |
|-------|--------------|----------------------------------------|
| off | Plan mode: Read, Grep, Glob, TodoWrite and read-only MCP tools only | `ls`, `cat`, `head`, `tail`, `wc`, `grep`, `rg`, `pwd`, `echo`, `which`, `git status`, `git diff`, `git log`, `git show` |
| low | Act mode: full surface, edits under cwd | above plus `pytest`, `ruff`, `make test`, `make lint`, `npm test`, `npm run test`, `go test`, `cargo test`, `git add`, `python -m` for the test and lint modules only (pytest, ruff, py_compile, unittest, mypy, flake8, coverage, `black --check`, `pip check`) |
| medium | same | above plus `git commit`, `npm install`, `pip install`, `cargo build`, `go build`, `make`, and file ops inside cwd |
| high | same | above plus `git push`, `curl`, `wget`, `ssh`, and everything the blocklist leaves alone |

The classifier in `coding_harness/security/commands.py` splits a command into
its pipeline, list, subshell and line segments and returns the strictest
verdict. Blocklisted commands deny at every level. A write redirection outside
cwd denies below HIGH. Anything the ladder does not name at the current level
asks the operator in the REPL and is denied headless. Plan mode is enforced by
visibility: the model never sees a tool that could mutate disk, so it cannot
request one.

## Security

- **Sentinel gate** (`security/policy.py`): an in-process, fail-closed review
  of every tool call. Order: unbypassable blocklist, level-gated list, tool
  category rule, autonomy ladder, then the optional deployment hook
  (`SENTINEL_GATE_HOOK`).
- **Session envelope** (`core/envelope.py`): deny-by-default scope for what a
  session may touch. Print and serve attach the cwd preset. Out-of-scope calls
  never reach Sentinel.
- **Snapshots and rewind**: per-turn filesystem snapshots so a bad turn can be
  undone.
- **Audit chain**: every dispatch is appended to a hash-chained log, signed
  when pynacl is present.
- **Banned origins**: Chinese-origin model families are refused at the CLI and
  again at call time. See [Model policy](#model-policy).

## Verification and review

After each Edit or Write, deterministic gates (py_compile, ruff with autofix,
per-suffix syntax checks) feed failures back so malformed edits self-repair.
Then the tests targeting the edited module run. Before the session says done,
the project's checks (`--check`, the serve turn's `checks`, or a discovered
test command) run against a pre-edit baseline. A new failure buys a repair
round. When the model declares done, a local reviewer grades the diff against
the task and concerns feed back for one more round.

## Context

Hierarchical AGENTS.md and CLAUDE.md ingestion, a deterministic repo map
injected at turn start (`HARNESS_REPO_MAP_TOKENS` budget), and a tool probe
that tells the model which python and CLI tools resolve in cwd. The
conventions budget is 24000 bytes total with per-layer caps: repo files
12000, the global `~/.config/bjorn/AGENTS.md` 6000, the memory index 6000.
The global file renders first, so anything closer overrides it.

## Plan and act

`/plan <goal>` in the REPL runs one read-only turn. The model investigates
and writes a markdown plan. The plan lands in `meta_dir()/plans/<session_id>.md`
and the path is printed. `/plan` with no argument shows the current plan path.
`/act` re-reads the saved plan and executes it at autonomy LOW, or the
settings default if that is higher. `/act <path>` executes a specific plan
file.

The GUI's **spec** tab (right pane) does the same over serve. Type a goal and
draft a plan, edit it in place, then run it at LOW, MEDIUM or HIGH. The run
turn keeps a checklist with the `TodoWrite` tool. The list shows above the
composer while it has items. The status line shows the session's level.

| Route | Body | Result |
|-------|------|--------|
| `POST /v1/sessions/{id}/plan` | `{goal}` | 202. The turn runs at OFF and the level is restored after. Emits `plan_saved {path, bytes}` or `plan_failed {reason}` |
| `GET /v1/sessions/{id}/plan` | | `{exists, text, mtime}` |
| `PUT /v1/sessions/{id}/plan` | `{text}` | Writes the session's fixed plan path only. 409 mid-turn. Emits `plan_updated {bytes}` |
| `POST /v1/sessions/{id}/act` | `{autonomy}` | 202. 400 for `off` or no plan. Runs the plan with a TodoWrite instruction |
| `POST /v1/sessions/{id}/autonomy` | `{level}` | `{from, to, mode}`. Emits `autonomy_change {from, to, reason}`. A running turn sees it at its next model step |
| `GET /v1/sessions/{id}/todo` | | `{items}`, also streamed as `todo_update {items}` |

REPL and serve share one `set_autonomy` in `core/mode.py`, audited as an
`autonomy_change` row. In serve, a session on the cwd preset envelope also
gets its default grants rebuilt for the new level. That swap is audited as
an `envelope_change` row with action `preset`, and operator grants survive
it. A session created with a custom envelope keeps it. `TodoWrite` is
plan-safe. It touches no project file and mirrors the list to
`meta_dir()/plans/<session_id>.todo.json`.

## Board and steering

The GUI's **board** tab shows every session on every running `bjorn` GUI
server. Each card has the project, a status, the pending approval count, the
last reply excerpt, the model, the autonomy level and the time since the last
event. Clicking a card opens the session. A session on another project opens
that project's own server in a new tab.

| Route | Body | Result |
|-------|------|--------|
| `GET /v1/board` | | `{server: {cwd, port, pid}, sessions: [{session_id, title, status, pending, last_excerpt, last_event_ts, turn, model, autonomy}]}`. Status is `needs_approval`, `running`, `error`, `done` or `idle` |
| `GET /v1/board?all=1` | | `{servers: [...], errors: [{origin, error}]}`. Reads the GUI registry in `meta_dir()/gui`, prunes dead pids, fetches each loopback origin in parallel with no proxy, no redirects, a 2 MB body cap and one shared ~1.2 s deadline. A slow origin is reported as `timeout` |
| `POST /v1/sessions/{id}/steer` | `{message}` | 202 while a model turn runs (async or `wait:true`), 409 otherwise, including during `/compact` and revert (use `/turn`). Emits `steer_queued {turn, text}`, then `steer_injected {turn, step, text}`, or `steer_dropped {turn, texts}` when the turn ends first |

A steer message joins the conversation as a user message before the running
turn's next model call. It is logged as a `steer_message` record, so replay
rebuilds it, and it does not count as a user turn for rewind. A steer that
arrives during the final model read buys one more step. One still queued when
the turn ends is dropped, never carried into the next turn. While a turn
runs, the composer shows a **steer** button beside stop.

Browser notifications are opt-in from the masthead. With permission granted,
the page notifies when a session finishes or errors, or starts waiting on an
approval. It only does so while the tab is hidden or the session is not the
one open. There is no sound.

## Memory

Each repo gets a memory directory at `~/.config/bjorn/memory/<repo>/`.
`HARNESS_MEMORY_DIR` moves the root, and the eval runner and both test
conftests point it at a temp dir so they never write the real corpus. The
repo slug is the git root's basename. `MEMORY.md` there is the index. Its
text is injected into the system prompt under a `## Memory` heading, capped
at 6000 bytes. The base system prompt names the memory directory, the
`<type>_<slug>.md` file convention and the frontmatter keys, so the model
knows where and how to write. The model writes memories with the ordinary
Write tool. The envelope preset grants write under the memory directory
from LOW up.
`bjorn-migrate-memory` copies Claude Code project memory into this tree.
It never overwrites, and `--dry-run` lists what it would copy.

## Worktrees

`bjorn --worktree <topic>` creates `../<repo>-<topic>` with
`git worktree add` and starts the REPL there. The branch is created unless
it already exists. An existing path is refused. One checkout, one active
agent.

### Git panel

The GUI's right pane has a git tab, and the header shows the branch (or a
detached marker) beside the project picker. `GET /v1/git` returns the branch,
upstream, ahead/behind and the staged, unstaged and untracked lists, cached
for 2 s. `POST /v1/git/commit {session_id, message, paths}` runs
`git add -- <paths>` then `git commit -F <file> -- <paths>` with literal
pathspecs, so nothing outside the ticked paths is staged. Paths must sit
under the project and outside `.git`. The session must be interactive and
idle (409 mid-turn). `git commit` is a MEDIUM command on the autonomy ladder,
so below MEDIUM the route parks on the permission broker until the operator
approves the exact command. A commit emits `git_commit {sha, paths,
message_first_line}` on the session stream. `GET /v1/worktrees` lists the
worktrees and which have a live GUI server. `POST /v1/worktrees {topic}`
creates `../<repo>-<topic>` as `--worktree` does (409 if the path exists).
`POST /v1/worktrees/open {path}` accepts only a listed worktree and returns
the origin of its server, starting one if needed. The panel never pushes,
deletes branches or forces. Push stays a shell action at HIGH.

## Settings file

Two JSON files, user then project, merged per key:

- `~/.config/bjorn/settings.json`
- `<git root or cwd>/.bjorn/settings.json`

The project file wins per scalar key. List keys concatenate, so a project can
add prefixes and roots but never remove what the user file set.
A project file is untrusted until you run `bjorn trust` in that repository.
Before that its `autonomy`, `hooks`, `commandAllowlist`, `extraReadRoots` and
`sandbox` keys are skipped with a startup note, its `.mcp.json` is not loaded
and its `.claude/hooks/sentinel-gate.py` is not run. Trust covers
subdirectories and is stored in `~/.config/bjorn/trusted_projects.json`.
See [SECURITY.md](SECURITY.md) for the threat model.
`HARNESS_SETTINGS=off` skips both. Unknown keys are an error. A list holds
at most 500 entries of at most 1024 characters each.

| Key | Type | Effect |
|-----|------|--------|
| `autonomy` | string | default level: `off`, `low`, `medium`, `high` |
| `commandAllowlist` | list | command prefixes allowed from LOW up |
| `commandDenylist` | list | command prefixes denied at every level |
| `commandBlocklist` | list | extends the built-in blocklist, can never remove an entry |
| `denyWrite` | list | path prefixes (not globs) the session may never write. Enforced by the session envelope in print and serve; the REPL has no envelope and does not apply it |
| `extraReadRoots` | list | directories readable outside cwd |
| `hooks` | dict | lifecycle hook commands per event, lists concatenate per event (see Hooks) |
| `brain` | dict | the second-brain index, user file only (see below) |
| `sandbox` | dict | accepted and stored; nothing reads it yet. There is no sandbox: LOW means arbitrary code inside the working directory's toolchain |

### The brain block

The `brain` key picks one of two backends with `backend`:

- `inprocess` calls the slos_recall library in this process. Keys:
  `agent_id` (required; its clearance is read from the identity table on
  every call), `db` (index path, else the library default),
  `agents_yaml` (identity table, else the library default) and
  `embed_timeout_s` (default 5; a slower or failed query embedding falls
  back to keyword search and says so). One query embedding runs at a time,
  and after a timeout searches stay keyword-only for 60 seconds.
- `mcp` spawns an slos-recall MCP server: `command`, `args`, `cwd`, `env`
  as in `.mcp.json`, plus `agent_id`.

With no `backend`, a block without `command` goes in-process when
slos_recall is importable, and anything else goes over MCP. An explicit
`inprocess` with the package missing is an error, never a quiet fallback.
Either way the harness's own tier caps apply on top of the index's
clearance. A brain block that cannot start (package missing, no
`agent_id`, a bad value) never stops a session: it starts without the
Brain tool, with one `warning:` line on stderr in print and the REPL, and a
`brain_unavailable {reason}` event in serve.

```json
"brain": {"backend": "inprocess", "agent_id": "bjorn-harness"}
```

## Hooks

Hooks attach in every mode: print, the REPL, `bjorn serve` and the GUI.
Two limits today: `PreCompact` is declared and never fired, and
`UserPromptSubmit` cannot block and is not given the prompt text.

The `hooks` settings key runs shell commands on session events:
`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PreCompact`
and `Stop`. Each event maps to a list of `{matcher, command, timeout}`
entries (Claude Code's nested `{matcher, hooks: [{command, timeout}]}` form
is accepted too). `matcher` is a glob on the tool name, or `Tool(prefix *)`
which matches the command string for Bash. An empty matcher matches every
call. Non-tool events ignore matchers.

Contract per hook, compatible with Claude Code hooks:

- The command runs as `/bin/sh -c <command>` with a scrubbed environment
  (`security/env_scrub.py`) and a JSON payload on stdin:
  `{event, session_id, cwd, tool_name, tool_input, tool_output?}`.
  `tool_name`/`tool_input` appear on tool events, `tool_output` only on
  `PostToolUse`.
- Exit 0 allows. Exit 2 blocks, with stderr as the reason. A stdout JSON
  object `{"decision": "block", "reason": "..."}` also blocks.
- `PreToolUse` blocks deny the tool call with the reason as the tool error,
  and fail closed on a timeout or crash. Every other event fails open and
  logs.
- A `Stop` block's stderr is fed back to the model as one user message, once
  per turn.
- Timeout defaults to 10 seconds per hook. `HARNESS_HOOKS=0` disables all of
  it.

Example, a guard on every `rm` the agent tries to run:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash(rm *)",
        "command": "echo 'rm is not allowed; delete files individually after listing them' >&2; exit 2"
      }
    ]
  }
}
```

## Skills

A skill is one directory holding a `SKILL.md`; a directory without one is not
a skill and is skipped. Roots are searched in this order and a later one wins
a name clash, so project beats user and the native `.bjorn` path beats a
borrowed layout:

| Tier | Roots, lowest precedence first |
|------|--------------------------------|
| User | `~/.agents/skills`, `~/.claude/skills`, `~/.config/bjorn/skills` |
| Project | `<git root>/.agents/skills`, `.claude/skills`, `skills`, `.bjorn/skills` |

The borrowed roots are there because the SKILL.md convention is shared: a
machine's skills are usually already written for another agent, and
re-authoring them under `.bjorn` to say the same thing twice is how a corpus
rots. A root that a machine does not use is simply absent, so the extra
lookups cost one listdir each.

`name:` and `description:` come from a `---` frontmatter block, falling back
to the directory name and the first body paragraph. A folded block scalar
(`description: >`) is read across its continuation lines rather than yielding
the marker.

The index renders into the system prompt as a `## Skills` block capped at 2000
bytes. The budget buys breadth before depth: descriptions shrink so that every
skill is still named, because knowing a skill exists is what lets the model ask
for it. Only if the names alone overflow does the list truncate, and it then
states how many it dropped instead of ending silently.

`tools:` frontmatter is deliberately not read. A skill is injected as prompt
text and grants nothing; every resulting tool call is still gated by the
autonomy ladder, the session envelope and Sentinel, so honouring `tools:`
would only add a weaker second gate.

In the REPL, `/skill <name>` injects that skill's full SKILL.md as the next
prompt. `context/skills.py` exports `skills_block(cwd)` and `load_skill(name)`
for the full text.

## Recall, skills and memory proposals

Recall runs before the first model step of each local Ollama turn. It never
runs on frontier or claude-cli steps. The prompt is searched against the
second brain, top 5 hits across every vault. Hits are kept up to the local
tier cap. Prompts under 12 characters and slash commands are skipped. Notes
the operator pinned render under `## Pinned notes`. Recall hits render under
`## Recalled notes`, capped at 3000 bytes, and the two together at 6000. A
skill whose name appears whole in the prompt, or whose name and description
share two or more words with it, adds one line, `Consider /skill <name> for
this task.` Function words and generic task words (task, fix, file, code and
the like) do not count. The hint rides only REPL and serve sessions, where a
person reads it. Print mode and eval never get it, and `HARNESS_RECALL=0`
stops it too. No SKILL.md is loaded until the operator clicks a chip or the
model asks.

The block is one transient user message placed just before the turn's
prompt. It lives only in the list sent to the model. It is not in the
session history and not in the transcript, so replay and rewind never see
it. A hit or pin at confidential or above marks the session sensitive. Every
later turn then stays local.

Recall reads the index age first (in-process only; MCP cannot say, and an
unknown age never skips). Past 30 hours `brain_primed` carries
`stale: true`. Past a week automatic recall feeds nothing and logs
`recall_skipped_stale {age_hours}` once for the turn. An explicit Brain call
still answers a stale index, under a first line `Note: the index is N hours
old.` `GET /v1/brain/status` answers `{configured, backend, ok, age_hours,
stale, warnings}`, and the Brain tab shows the backend, the age and a stale
marker.

After `turn_done` on a local turn, a proposal pass runs on a worker thread.
It runs in the GUI serve, and in `bjorn serve` once user settings configure a
brain. The turn must hold an operator correction, a denial, a failed check it
then fixed, or a Brain or recall hit. A correction is a steer inside the turn,
or a follow-up prompt that opens with no, not, wrong, instead, actually,
rather, or "use X not Y". The local reviewer model (`HARNESS_REVIEW_MODEL`, else
the session model) drafts 0 to 3 memories as JSON, with a 60 second cap. Any
malformed item rejects the whole reply. Proposals wait under
`<meta>/memory_proposals/<session>/<n>.md`. Nothing reaches the memory
directory without a decision. Approve runs the settings' PreToolUse hooks
with a `Write` payload before the file lands, so a memory schema gate still
judges it. MEMORY.md is regenerated by a deployment's `pipelines.memory_index`
when importable, else gains one line.

| Route | Body | Answer |
|-------|------|--------|
| `GET /v1/skills/suggest?q=` | | `{suggestions: [{name, description, score}]}`, at most 3 |
| `GET /v1/sessions/{id}/memories` | | `{proposals: [{id, name, description, type, text, sha256}], pinned: [{path, tier}]}` |
| `POST /v1/sessions/{id}/memories` | `{id, decision: approve\|edit\|reject, sha256, text?}` | `{id, decision, path?}`; 409 name taken or text changed, 422 hook denied |
| `POST /v1/sessions/{id}/pin` | `{path, pinned}` | `{path, pinned, tier?}` |

Events: `brain_primed {turn, paths, tiers, bytes, index_age_hours, stale}`, `skills_suggested
{names}`, `memory_proposed {id, name, description, type, sha256}`, `memory_decided
{id, decision, path?}`, `note_pinned {path, pinned, tier}`. Kill switches:
`HARNESS_RECALL=0` stops the search (pins still render) and
`HARNESS_MEMORY_PROPOSALS=0` stops the proposal pass.
`HARNESS_REVIEW_BACKEND=off` does not stop proposals: they borrow the reviewer
model, not the review switch.

A session marked sensitive drafts no proposals, because an approved
memory's description reaches MEMORY.md and so every later prompt, frontier
included. The same mark sends the agentic review to the local reviewer even
when `HARNESS_REVIEW_BACKEND=claude-cli`, and emits `review_rerouted`.
Proposal ids are a millisecond stamp plus random hex, never reused. A
decision must carry `sha256`, the hash of the text the operator was shown;
a mismatch answers 409.

## Custom commands

`/<name> args` in the REPL expands `.bjorn/commands/<name>.md` (project) or
`~/.config/bjorn/commands/<name>.md` (user) into a prompt, with
`$ARGUMENTS` replaced by everything after the command name. A line that is
not a custom command passes through untouched, so built-in slash commands
keep priority.

## Model roster

Each job has a role (`coding_harness/core/model_roles.py`). Assign them in
the settings file, or in the GUI's Settings tab:

```json
{"model": "mistral-small3.2:latest",
 "models": {"chat": "granite4.1:8b", "explore": "granite4.1:8b",
            "review": "mistral-small3.2", "summarize": "granite4.1:8b"}}
```

| Role | Default | Used for |
|------|---------|----------|
| code | `mistral-small3.2:latest` | turns with tools; `--model`, the `model` key or `models.code` |
| chat | `granite4.1:8b` | chat sessions, which send no tools |
| explore | `granite4.1:8b` | the Explore subagent (GUI and REPL only) |
| review | `mistral-small3.2` | the reviewer; `HARNESS_REVIEW_MODEL` still overrides |
| summarize | `granite4.1:8b` | compaction summaries and memory proposals |
| review, claude-cli backend | `opus` | `DEFAULT_CLI_MODEL` in `core/review.py` |

One big model and one small. Review stays on the big model because the
reviewer bench showed small models cannot do it, and the big model is
resident for code anyway. A default that is not pulled falls back to the
code model. The GUI labels each model agent, chat only or untested from a
forced tool-call probe.

### Model policy

This project refuses Chinese-origin model families: Qwen, DeepSeek, Yi,
Baichuan, GLM, InternLM, MiniMax, Kimi, Hunyuan, ByteDance and the rest of
`BANNED_MODEL_PREFIXES` in `coding_harness/models/ollama.py`. This is a
maintainer supply-chain policy, enforced in code and by a CI test. It is not
configurable, and changes that loosen it are not accepted.

## Environment variables

| Variable | Default | Effect |
|----------|---------|--------|
| `OLLAMA_URL` | `http://localhost:11434` | Ollama server base URL (`models/ollama.py`) |
| `SENTINEL_GATE_HOOK` | unset | path to a deployment hook script, overrides the walk-up search (`security/hook_adapter.py`) |
| `HARNESS_META_DIR` | `~/.local/state/bjorn` | state root for audit logs, sessions and snapshots (`core/paths.py`) |
| `HARNESS_CLAUDE_HOME` | unset | HOME for the claude CLI subprocess only; the eval runner sets it to the real home on claude-cli review rows while the harness runs under a temp HOME (`models/claude_cli.py`) |
| `HARNESS_MEMORY_DIR` | `~/.config/bjorn/memory` | root of the per-repo memory directories; `~` expands (`context/memory.py`) |
| `HARNESS_SETTINGS` | unset | `off` skips both settings files (`core/settings.py`) |
| `HARNESS_HOOKS` | `1` | `0` disables the settings-declared lifecycle hooks (`core/hooks.py`) |
| `HARNESS_POLICY` | unset | `hook` bypasses the in-process policy and asks only the subprocess hook (`security/sentinel.py`) |
| `HARNESS_ENVELOPE` | unset | `off` removes the session envelope in print mode, restoring the ungated session (`modes/print_mode.py`) |
| `HARNESS_VERIFY_REPAIR` | `1` | `0` disables the post-edit verify-repair loop (`core/session.py`) |
| `HARNESS_DONE_GATE` | `1` | `0` disables the green-before-done gate and its baseline (`core/session.py`) |
| `HARNESS_SHADOW` | `1` | `0` disables the shadow git checkpoints kept under `<meta>/shadow/<session>` (`core/git.py`) |
| `HARNESS_STUCK_DETECT` | `1` | `0` disables the repeated-call detector: nudge after 2 identical steps, halt with `stuck` after 4 (`core/stuck.py`) |
| `HARNESS_TARGETED_TESTS` | `1` | `0` disables running the tests targeting an edited module (`core/session.py`) |
| `HARNESS_CONTEXT_BUDGET` | `1` | `0` disables per-tool output caps (Grep 8k, Glob 4k, other non-Read/Bash tools 8k chars) and the mid-turn shrink that, once a local prompt passes 60% of `num_ctx` minus 1500 tokens, elides superseded Reads and then compacts earlier steps (`core/context_budget.py`) |
| `HARNESS_DEADLINE_RESERVE_S` | `120` | seconds a turn keeps in hand before running the reviewer (`core/turn_guards.py`) |
| `HARNESS_REVIEW` | `1` | `0` disables the agentic review (`core/review.py`) |
| `HARNESS_REVIEW_BACKEND` | `local` | `local`, `claude-cli`, or `off` (`core/review.py`) |
| `HARNESS_REVIEW_MODEL` | per backend | reviewer model, banned origins refused (`core/review.py`) |
| `HARNESS_RECALL` | `1` | `0` stops the prompt-time brain search and the skill hint on local turns; pinned notes still render (`context/recall.py`) |
| `HARNESS_MEMORY_PROPOSALS` | `1` | `0` stops the post-turn memory proposal pass in the GUI serve (`core/memory_proposals.py`) |
| `HARNESS_REPO_MAP` | `1` | `0` skips the repo map at turn start (`modes/print_mode.py`) |
| `HARNESS_PROMPT_OVERLAY` | unset | path of a text file appended to the base prompt as "## Operating notes", capped at 4000 bytes; the prompt-evolution loop's mutation target (`context/overlay.py`) |
| `HARNESS_REPO_MAP_TOKENS` | `1024` | token budget for the repo map (`modes/print_mode.py`) |
| `HARNESS_TOOL_PROBE` | `1` | `0` skips probing python and CLI tools in cwd (`context/toolprobe.py`) |
| `HARNESS_EDIT_HINT` | unset | `0` drops the nearest-window hint from an Edit miss (`tools/edit.py`) |
| `HARNESS_EDIT_TIERS` | unset | `exact` turns off the tolerant Edit match tiers (`tools/edit_match.py`) |
| `HARNESS_NUM_CTX` | per model profile | the context window the harness budgets against (`models/profile.py`). Not sent to Ollama: the `/v1` endpoint ignores it, so it must match the model's own `num_ctx` |
| `HARNESS_NUM_PREDICT` | per model profile | Ollama `num_predict` override (`models/profile.py`) |
| `HARNESS_TEMPERATURE` | per model profile | sampling temperature override (`models/profile.py`) |
| `HARNESS_THINK` | per model profile | `0` or `1`, recorded on the profile (`models/profile.py`). Not sent to Ollama over `/v1` today, so it has no effect on the model |
| `HARNESS_KEEP_ALIVE` | per model profile | recorded on the profile (`models/profile.py`). Not sent to Ollama over `/v1` today, so it has no effect on the model |
| `HARNESS_READ_TIMEOUT` | `360` | seconds to wait on a streaming read (`models/transport.py`) |
| `HARNESS_TRUST_PROJECTS` | unset | `all` trusts every directory's `.bjorn/settings.json`, `.mcp.json` and sentinel hook without `bjorn trust`, for deployments and CI (`core/trust.py`) |
| `HARNESS_PROJECTS_ROOT` | `~/projects` | directory whose git checkouts the GUI project picker offers (`modes/serve_gui.py`) |
| `HARNESS_AGENTS_FILE` | `~/.config/bjorn/agents.yaml` | optional agent identities and public keys for audit signing; absent means unsigned entries (`security/audit.py`) |
| `HARNESS_HEARTBEAT_DIR` | `~/.config/bjorn/heartbeat` | optional scheduler state shown in the GUI Routines view; absent means an empty list (`modes/ops.py`) |
| `HARNESS_AUTH` | `1` | `0` disables the GUI login for non-loopback binds and proxied requests, with a startup warning (`modes/serve_auth.py`) |
| `HARNESS_AUTH_TOKEN_FILE` | `~/.config/bjorn/auth_token` | GUI login token file, created 0600 on a non-loopback bind or the first proxied request (`modes/serve_auth.py`) |

`coding_harness/tests/test_env_docs.py` fails when a `HARNESS_*` name in the
package is missing from this table.

## Standalone vs deployment

A deployment may provide an optional routing and cost-governance layer (a
`pipelines` package on the path). When it is absent the harness stays
local-only, so it never reaches a frontier model without an explicit router.
A deployment consumes it with `pip install -e ../bjorn-harness`.

## Develop

```bash
make install-dev          # editable install with pytest + ruff
make lint                 # ruff over the package and eval top level
make test                 # ruff + per-file test gate
make eval                 # harness eval, pins a baseline
make evolve               # prompt evolution over the overlay block
```

A task may use the `hidden_tests` grader: `repo/` is a base tree without
`.git`, and the grader copies the test files from the task's `hidden/` tree over
the workdir after the run and then runs them. A hidden test file qualifies only
if it fails (or errors on import) against the base snapshot and passes against
the solved tree, so a task cannot be resolved by leaving the snapshot alone.
Adding a task changes the task set, so the baseline must be re-pinned
(`make eval-gate GATE_ARGS=--pin`) before the gate reads as a verdict.
`eval/evolve.py` scores candidate operating-notes blocks through the eval and
writes the best one to `eval/results/overlay-best.md` for review, never applied.
