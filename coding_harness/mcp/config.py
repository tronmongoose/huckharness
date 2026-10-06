"""Load ``.mcp.json`` and expand ``${VAR}`` placeholders.

The repo's ``.mcp.json`` is the single source of truth for which MCP servers
the harness can reach. Format mirrors Claude Code's:

    {"mcpServers": {"<name>": {
        "command": "...",
        "args": ["..."],
        "cwd": "...",        # optional
        "env": {"K": "V"}    # optional, values may reference ${VAR}
    }}}

``${VAR}`` references resolve against the current process environment. An
unset variable expands to the empty string and is logged via a warning to
stderr — that matches Claude Code's behavior and avoids hard-failing the
harness when an optional credential is absent.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from coding_harness.core import trust

DEFAULT_CONFIG_NAME = ".mcp.json"

_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


@dataclass
class MCPServerConfig:
    """One server entry. ``env`` values are already expanded.

    Two transports are supported, mutually exclusive:

    - **Stdio** — set ``command`` (and optionally ``args``/``cwd``/``env``).
      The harness spawns the binary and frames JSON-RPC over its pipes.
    - **HTTP** — set ``url``. The harness POSTs requests and reads an SSE
      stream for notifications. ``oauth=True`` (default when ``url`` is
      set) negotiates OAuth 2.0 Auth Code + PKCE + Dynamic Client
      Registration on first run and caches refresh tokens to disk.

    ``plan_safe_names`` and ``plan_safe_patterns`` declare which
    of this server's tools are read-only and therefore visible in Plan
    mode. Both are optional; when omitted, every tool from the server is
    treated as act-only (fail-closed) by ``core.mode.is_plan_safe``.
    Declarations live in the per-server block of ``.mcp.json`` so adding
    a new server doesn't require editing harness source.
    """

    name: str
    # Stdio
    command: str | None = None
    args: list[str] = field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    # HTTP + OAuth
    url: str | None = None
    oauth: bool = True
    oauth_scopes: list[str] | None = None
    # Plan/Act gating
    plan_safe_names: list[str] = field(default_factory=list)
    plan_safe_patterns: list[str] = field(default_factory=list)


def _expand(value: str, environ: dict[str, str]) -> str:
    def _sub(match: re.Match[str]) -> str:
        var = match.group(1)
        replacement = environ.get(var, "")
        if replacement == "" and var not in environ:
            print(
                f"warning: .mcp.json references unset env var ${{{var}}}",
                file=sys.stderr,
            )
        return replacement

    return _VAR_RE.sub(_sub, value)


def _expand_obj(obj: object, environ: dict[str, str]) -> object:
    if isinstance(obj, str):
        return _expand(obj, environ)
    if isinstance(obj, list):
        return [_expand_obj(v, environ) for v in obj]
    if isinstance(obj, dict):
        return {k: _expand_obj(v, environ) for k, v in obj.items()}
    return obj


def find_config(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` (or the working directory) looking for ``.mcp.json``.

    The default was this file's own location, so an installed harness searched
    its package directory and loaded nothing for the project it was run in.
    """
    here = (start or Path.cwd()).resolve()
    candidates = [here] if here.is_dir() else [here.parent]
    candidates.extend(here.parents)
    for parent in candidates:
        candidate = parent / DEFAULT_CONFIG_NAME
        if candidate.exists():
            return candidate
    return None


def load_mcp_config(
    path: Path | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> list[MCPServerConfig]:
    """Load ``.mcp.json`` and return one ``MCPServerConfig`` per server.

    ``path`` overrides discovery (useful for tests). ``environ`` overrides
    the process environment (also useful for tests).
    """
    config_path = path or find_config()
    if config_path is None:
        return []
    if path is None and not trust.is_trusted(config_path.parent):
        return []

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    servers_raw = raw.get("mcpServers") or {}
    if not isinstance(servers_raw, dict):
        raise ValueError(
            f"{config_path}: 'mcpServers' must be an object, got {type(servers_raw).__name__}"
        )

    env_source = environ if environ is not None else dict(os.environ)
    out: list[MCPServerConfig] = []
    for name, entry in servers_raw.items():
        if not isinstance(entry, dict):
            raise ValueError(
                f"{config_path}: server '{name}' must be an object"
            )
        expanded = _expand_obj(entry, env_source)
        if not isinstance(expanded, dict):
            raise ValueError(f"{config_path}: server '{name}' did not expand to an object")
        command_raw = expanded.get("command")
        url_raw = expanded.get("url")
        command: str | None
        url: str | None
        if command_raw is not None and url_raw is not None:
            raise ValueError(
                f"{config_path}: server '{name}' must set exactly one of "
                f"'command' or 'url', not both"
            )
        if command_raw is None and url_raw is None:
            raise ValueError(
                f"{config_path}: server '{name}' must set 'command' "
                f"(stdio) or 'url' (http)"
            )
        if command_raw is not None:
            if not isinstance(command_raw, str) or not command_raw:
                raise ValueError(
                    f"{config_path}: server '{name}' 'command' must be a non-empty string"
                )
            command = command_raw
            url = None
        else:
            if not isinstance(url_raw, str) or not url_raw:
                raise ValueError(
                    f"{config_path}: server '{name}' 'url' must be a non-empty string"
                )
            command = None
            url = url_raw
        oauth_raw = expanded.get("oauth")
        if oauth_raw is None:
            oauth = url is not None  # default on when remote
        elif isinstance(oauth_raw, bool):
            oauth = oauth_raw
        else:
            raise ValueError(
                f"{config_path}: server '{name}' 'oauth' must be a boolean"
            )
        if oauth and url is None:
            raise ValueError(
                f"{config_path}: server '{name}' 'oauth' only valid when 'url' is set"
            )
        oauth_scopes_raw = expanded.get("oauth_scopes")
        oauth_scopes: list[str] | None
        if oauth_scopes_raw is None:
            oauth_scopes = None
        elif isinstance(oauth_scopes_raw, list) and all(
            isinstance(s, str) for s in oauth_scopes_raw
        ):
            oauth_scopes = list(oauth_scopes_raw)
        else:
            raise ValueError(
                f"{config_path}: server '{name}' 'oauth_scopes' must be a list of strings"
            )
        args = expanded.get("args") or []
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ValueError(
                f"{config_path}: server '{name}' 'args' must be a list of strings"
            )
        cwd = expanded.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise ValueError(
                f"{config_path}: server '{name}' 'cwd' must be a string"
            )
        env = expanded.get("env") or {}
        if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            raise ValueError(
                f"{config_path}: server '{name}' 'env' must be a string→string map"
            )
        plan_safe_names = expanded.get("plan_safe_names") or []
        if not isinstance(plan_safe_names, list) or not all(
            isinstance(v, str) for v in plan_safe_names
        ):
            raise ValueError(
                f"{config_path}: server '{name}' 'plan_safe_names' must be a list of strings"
            )
        plan_safe_patterns = expanded.get("plan_safe_patterns") or []
        if not isinstance(plan_safe_patterns, list) or not all(
            isinstance(v, str) for v in plan_safe_patterns
        ):
            raise ValueError(
                f"{config_path}: server '{name}' 'plan_safe_patterns' must be a list of strings"
            )
        out.append(
            MCPServerConfig(
                name=name,
                command=command,
                args=list(args),
                cwd=cwd or None,
                env=dict(env),
                url=url,
                oauth=oauth,
                oauth_scopes=oauth_scopes,
                plan_safe_names=list(plan_safe_names),
                plan_safe_patterns=list(plan_safe_patterns),
            )
        )
    return out
