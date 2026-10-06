"""Environment scrubber for tool subprocesses.

Exports ``SECRET_NAME_PATTERNS``, ``is_secret_name`` and ``scrub``. ``scrub(env)`` returns a copy
of ``env`` without any variable whose name looks like a credential (``*_KEY``,
``*_TOKEN``, ``*_SECRET``, ``*PASSWORD*``, ``AWS_*``, ``ANTHROPIC_*``,
``OPENAI_*``, ``GH_*``, ``GITHUB_TOKEN``). Everything else, including PATH,
HOME, VIRTUAL_ENV, LANG, TERM and TMPDIR, passes through untouched.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

SECRET_NAME_PATTERNS: tuple[str, ...] = (
    r".*_KEY$",
    r".*_TOKEN$",
    r".*_SECRET$",
    r".*PASSWORD.*",
    r"AWS_.*",
    r"ANTHROPIC_.*",
    r"OPENAI_.*",
    r"GH_.*",
    r"GITHUB_TOKEN$",
)
_SECRET_RE = re.compile("|".join(f"(?:{p})" for p in SECRET_NAME_PATTERNS), re.IGNORECASE)


def scrub(env: Mapping[str, str]) -> dict[str, str]:
    """Copy of ``env`` with every credential-shaped variable name dropped."""
    return {name: value for name, value in env.items() if not is_secret_name(name)}


def is_secret_name(name: str) -> bool:
    """Whether a variable name matches one of ``SECRET_NAME_PATTERNS``."""
    return _SECRET_RE.fullmatch(name) is not None
