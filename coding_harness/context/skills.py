"""Skill index: SKILL.md packs surfaced as a budgeted system-prompt block.

Exports ``SKILLS_USER``, ``SKILLS_PROJECT_REL``, ``SKILLS_USER_EXTRA``,
``SKILLS_PROJECT_EXTRA_REL``, ``Skill``, ``index_skills``, ``skills_block``,
``load_skill`` and ``score_skills``. A skill is one directory holding a ``SKILL.md``; a
directory without one is not a skill and is skipped. ``name:`` and
``description:`` come from a ``---`` frontmatter block, falling back to the
directory name and the first body paragraph.

Roots are searched in ``_roots`` order and a later root wins a name
collision, so project beats user and the harness-native ``.bjorn`` path beats
a borrowed layout. The borrowed roots exist because the SKILL.md convention is
shared: a machine's skills are typically already written for another agent,
and re-authoring them under ``.bjorn`` only to say the same thing twice is how
a corpus rots. Indexing a directory costs a listdir, so the extra roots are
close to free and simply absent on a machine that does not use them.

``tools:`` frontmatter is deliberately NOT read. Some corpora declare a tool
whitelist per skill, but a skill here is injected as prompt *text* by
``/skill``; it grants nothing. Every resulting tool call is still gated by the
autonomy ladder, the session envelope and Sentinel, so honouring ``tools:``
would add a second, weaker gate that could only ever be more permissive than
the real one. Treat it as advisory metadata belonging to whoever authored it.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from coding_harness.core.settings import _project_root

SKILLS_USER = Path("~/.config/bjorn/skills")
SKILLS_PROJECT_REL = Path(".bjorn") / "skills"
# Shared-convention roots, lower precedence than the native paths above.
SKILLS_USER_EXTRA = (Path("~/.agents/skills"), Path("~/.claude/skills"))
SKILLS_PROJECT_EXTRA_REL = (
    Path(".agents") / "skills",
    Path(".claude") / "skills",
    Path("skills"),
)
_BLOCK_BUDGET = 2000
_DESC_CAP = 200
_DESC_FLOOR = 24
_BLOCK_SCALARS = (">", "|", ">-", "|-", ">+", "|+")


@dataclass
class Skill:
    """One indexed skill pack."""

    name: str
    description: str
    path: Path


def _roots(cwd: str) -> tuple[Path, ...]:
    """User roots then project roots; later entries win on name collision."""
    project = _project_root(cwd)
    return (
        *(Path(os.path.expanduser(str(p))) for p in SKILLS_USER_EXTRA),
        Path(os.path.expanduser(str(SKILLS_USER))),
        *(project / rel for rel in SKILLS_PROJECT_EXTRA_REL),
        project / SKILLS_PROJECT_REL,
    )


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    """(meta, body) from a leading ``---`` block, folding YAML block scalars.

    ``description: >`` followed by indented lines is common in shared corpora.
    Reading only the marker yields a one-character description, which tells a
    model nothing, so continuation lines are folded into the value. Treating
    them as continuations also stops an indented line that happens to contain
    a colon from being mistaken for a new key.
    """
    if not text.startswith("---"):
        return {}, text
    meta: dict[str, str] = {}
    lines = text.splitlines()
    body = text
    pending: str | None = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            body = "\n".join(lines[i + 1:])
            break
        if pending is not None and (not line.strip() or line[:1].isspace()):
            meta[pending] = f"{meta[pending]} {line.strip()}".strip()
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip().strip("'\"")
        pending = key if value in _BLOCK_SCALARS else None
        meta[key] = "" if pending is not None else value
    return meta, body


def _clip(text: str, cap: int) -> str:
    """``text`` within ``cap`` characters, cut on a word boundary when it can."""
    if len(text) <= cap:
        return text
    cut = text[:cap]
    space = cut.rfind(" ")
    if space > cap // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:.-") + "..."


def _parse(text: str, fallback_name: str) -> tuple[str, str]:
    """(name, description) from frontmatter, with directory/body fallbacks."""
    meta, body = _frontmatter(text)
    name = meta.get("name") or fallback_name
    description = meta.get("description") or _first_paragraph(body)
    return name, _clip(" ".join(description.split()), _DESC_CAP)


def _first_paragraph(body: str) -> str:
    """The first non-heading paragraph of ``body``, whitespace-collapsed."""
    for block in body.split("\n\n"):
        stripped = block.strip()
        if stripped and not stripped.startswith("#"):
            return " ".join(stripped.split())
    return ""


def index_skills(cwd: str) -> list[Skill]:
    """Every skill under the user and project roots, sorted by name."""
    found: dict[str, Skill] = {}
    for root in _roots(cwd):
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            path = entry / "SKILL.md"
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            name, description = _parse(text, entry.name)
            found[name] = Skill(name=name, description=description, path=path)
    return sorted(found.values(), key=lambda s: s.name)


def _line(skill: Skill, desc_cap: int) -> str:
    """One index line, description truncated to ``desc_cap`` characters."""
    if desc_cap <= 0:
        return f"- {skill.name}\n"
    description = _clip(skill.description, desc_cap)
    return f"- {skill.name}: {description}\n" if description else f"- {skill.name}\n"


def _fits(skills: list[Skill], desc_cap: int, budget: int) -> list[str] | None:
    """Rendered lines for every skill at ``desc_cap``, or None if over budget."""
    lines = [_line(s, desc_cap) for s in skills]
    if sum(len(x.encode("utf-8")) for x in lines) > budget:
        return None
    return lines


def skills_block(cwd: str) -> str:
    """A ``## Skills`` index block within ``_BLOCK_BUDGET`` bytes, or "".

    Knowing a skill exists is what lets the model ask for it, so the budget is
    spent on breadth before depth: descriptions shrink so that every skill is
    named. Only when the names alone overflow does the list truncate, and it
    then says how many it dropped rather than ending silently.
    """
    skills = index_skills(cwd)
    if not skills:
        return ""
    header = (
        "\n## Skills\n\n"
        "Skill packs with task-specific instructions. Load one with the\n"
        "/skill command (or ask the operator to) before doing its task.\n\n"
    )
    budget = _BLOCK_BUDGET - len(header.encode("utf-8"))
    for desc_cap in (_DESC_CAP, 96, 48, _DESC_FLOOR, 0):
        lines = _fits(skills, desc_cap, budget)
        if lines is not None:
            return header + "".join(lines)
    return header + "".join(_truncated(skills, budget))


def _truncated(skills: list[Skill], budget: int) -> list[str]:
    """Names-only lines that fit, plus a truthful count of those omitted."""
    lines: list[str] = []
    used = 0
    for index, skill in enumerate(skills):
        line = _line(skill, 0)
        remaining = len(skills) - index
        footer = f"- (+{remaining} more, ask to list them)\n"
        reserve = len(footer.encode("utf-8")) if remaining > 1 else 0
        if used + len(line.encode("utf-8")) + reserve > budget:
            lines.append(f"- (+{remaining} more, ask to list them)\n")
            break
        used += len(line.encode("utf-8"))
        lines.append(line)
    return lines


def load_skill(name: str, cwd: str | None = None) -> str | None:
    """The full SKILL.md text for ``name``; None when no such skill."""
    for skill in index_skills(cwd or os.getcwd()):
        if skill.name == name:
            try:
                return skill.path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
    return None


SUGGEST_TOP = 3
SUGGEST_MIN_OVERLAP = 2
_NAME_BONUS = 3
_WORD = re.compile(r"[a-z0-9]+")
# Function words plus generic task words. "not" and "any" alone once matched
# a design skill to a prompt about removing an unused import.
_STOPWORDS = frozenset(
    "a about all also an and any are as at be been but by can could do does each "
    "for from has have how i if in into is it its just me more most my no not now "
    "of on only or other our out please should so some such than that the their "
    "them then there these they this those to too up use uses using very was we "
    "were what when where which while who why will with would you your "
    "add code file fix make task".split()
)


def _tokens(text: str) -> set[str]:
    """Lowercase words of ``text`` with punctuation and stopwords dropped."""
    return {w for w in _WORD.findall(text.lower()) if len(w) > 1 and w not in _STOPWORDS}


def _name_in(name: str, prompt: str) -> bool:
    """Whether the skill's name appears in the prompt as a whole word."""
    if len(name) < 4:
        return False  # "run" or "pdf" would match half of all prompts
    return re.search(rf"(?<![\w-]){re.escape(name.lower())}(?![\w-])", prompt.lower()) is not None


def score_skills(prompt: str, skills: list[Skill]) -> list[tuple[Skill, int]]:
    """Up to three (skill, score) pairs by keyword overlap; below the threshold is dropped.

    A skill qualifies with at least ``SUGGEST_MIN_OVERLAP`` prompt words in
    its name and description, or its name in the prompt as a whole word.
    """
    words = _tokens(prompt)
    scored: list[tuple[Skill, int]] = []
    for skill in skills:
        overlap = len(words & _tokens(f"{skill.name} {skill.description}"))
        named = _name_in(skill.name, prompt)
        if overlap >= SUGGEST_MIN_OVERLAP or named:
            scored.append((skill, overlap + (_NAME_BONUS if named else 0)))
    scored.sort(key=lambda pair: (-pair[1], pair[0].name))
    return scored[:SUGGEST_TOP]
