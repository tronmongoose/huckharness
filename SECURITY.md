# Security

bjorn-harness lets a language model run shell commands and edit files on your
machine. Read this before pointing it at code you did not write.

## Reporting a vulnerability

Report privately through GitHub: the repository's Security tab, "Report a
vulnerability". Please do not open a public issue for an unfixed problem.
Include the version or commit, the steps, and what an attacker gains.

## What the harness defends

- **Destructive commands.** A built-in blocklist refuses raw disk writes,
  `mkfs`, `sudo`, a remote script piped to a shell and `git push --force` at
  every autonomy level. Recursive `rm`, `git clean -f`, `git reset --hard` and
  working-tree resets are gated by level. Settings can extend the list and can
  never shorten it.
- **Protected paths.** Write and Edit refuse `~/.ssh`, `~/.aws`, `~/.gnupg`,
  `~/.config/gh`, the keychain directory, `.git`, `.claude` and `.bjorn`.
- **Untrusted repositories.** A project's `.bjorn/settings.json` cannot set
  hooks, autonomy, the command allowlist or extra read roots, its `.mcp.json`
  is not loaded and its `.claude/hooks/sentinel-gate.py` is not run until you
  trust the directory with `bjorn trust`. Review those files first.
- **The local API.** The GUI server binds loopback by default. It refuses
  requests whose `Host` is not this machine, state-changing requests from
  another web origin, and bodies that are not declared JSON. A non-loopback
  bind requires a login token.
- **Gates fail closed.** A hook or gate that times out, crashes or returns
  something unexpected denies the call.
- **Child environments.** Shell commands, hooks and MCP stdio servers do not
  inherit credential-shaped variables (`*_KEY`, `*_TOKEN`, `*_SECRET`, `AWS_*`
  and similar). An MCP server gets a secret only if its `env` block names it.
- **Audit.** Every dispatch is appended to a hash-chained log. Verify it with
  `make audit-verify`.

## What it does not defend

- **There is no sandbox.** From autonomy LOW up, a command runs with your
  user's full permissions. The blocklist is a pattern match on the command
  text, and a determined prompt injection can write a script that evades it.
- **Shell reads are not confined.** The Read, Grep and Glob tools stay inside
  the workspace. Bash does not: `cat` on any file your user can read is
  allowed. Do not run the harness as a user with secrets it should not see.
- **Model output is untrusted.** File contents, tool results and web pages can
  carry instructions. Keep autonomy at `off` or `low` in repositories you have
  not reviewed, and read diffs before you commit.
- **Trusted means trusted.** After `bjorn trust`, that directory's hooks and
  MCP servers run as you. Trust is recorded in
  `~/.config/bjorn/trusted_projects.json` and covers subdirectories.
- **A remote Ollama sees your prompts.** `OLLAMA_URL` defaults to localhost.
  If you point it elsewhere, prompts, file contents and recalled notes go there.

Run it in a container or a throwaway VM when you need a real boundary.
