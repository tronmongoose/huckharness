"""Shell tokenizer for the command classifier.

Exports ``segments``, ``strip_redirects`` and ``write_targets``.

``segments(cmd)`` splits a command into token lists per pipeline, list,
subshell, process-substitution and line (a heredoc body stays with its
line). ``strip_redirects(tokens)`` returns the words of one segment without
redirection operators plus the paths its redirects write to, and
``write_targets(tokens)`` adds ``git --output`` operands to those paths.
"""
from __future__ import annotations

import re
import shlex

_READ_REDIRECTS = frozenset({"<", "<<", "<<<", "<&"})
_PUNCT = frozenset("();|&<>\n")
_CONTINUATION = re.compile(r"(?<!\\)((?:\\\\)*)\\\n")


def segments(command: str) -> list[list[str]]:
    """Token lists per pipeline, list, subshell or line; a heredoc body stays on its line."""
    out: list[list[str]] = [[]]
    delimiter: str | None = None
    in_body = False
    line_start = False
    for tok in _tokens(command):
        if _kind(tok) == "separator":
            in_body = in_body or (delimiter is not None and "\n" in tok)
            if not (in_body and set(tok) == {"\n"}):
                out.append([])
            line_start = "\n" in tok
            continue
        if in_body and line_start and tok == delimiter:
            delimiter, in_body = None, False
        else:
            delimiter = delimiter or _heredoc_delimiter(out[-1], tok)
            out[-1].append(tok)
        line_start = False
    return [seg for seg in out if seg]


def _tokens(command: str) -> list[str]:
    """shlex tokens with newlines as operators, ``#`` as a word and each paren alone."""
    # A comment would swallow the newline that ends it, and ``>(`` must read as
    # a redirect followed by a subshell rather than one operator.
    lex = shlex.shlex(_CONTINUATION.sub(r"\1 ", command), posix=True, punctuation_chars="();|&<>\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        raw = list(lex)
    except ValueError:
        raw = re.split(r"[ \t\r]+", command.replace("\n", " \n "))
    return [
        part
        for tok in raw
        for part in (re.split(r"([()])", tok) if _kind(tok) != "word" else [tok])
        if part
    ]


def _heredoc_delimiter(seg: list[str], tok: str) -> str | None:
    """The terminator when ``tok`` is the word after ``<<`` or ``<<-``, else None."""
    if seg[-1:] == ["<<"]:
        return tok.lstrip("-") or None
    if seg[-2:] == ["<<", "-"]:
        return tok
    return None


def _kind(tok: str) -> str:
    """word, separator (; && || | & parens) or redirect (< >) for one token."""
    if not tok or not set(tok) <= _PUNCT:
        return "word"
    return "redirect" if ("<" in tok or ">" in tok) else "separator"


def write_targets(tokens: list[str]) -> list[str]:
    """Paths one segment writes to: redirect targets plus ``git --output``."""
    words, targets = strip_redirects(tokens)
    return targets + _git_outputs(words)


def _git_outputs(words: list[str]) -> list[str]:
    """``--output=<path>`` and ``--output <path>`` operands of a git segment."""
    if words[:1] != ["git"]:
        return []
    out: list[str] = []
    for i, word in enumerate(words):
        if word.startswith("--output="):
            out.append(word[len("--output="):])
        elif word == "--output" and i + 1 < len(words):
            out.append(words[i + 1])
    return out


def strip_redirects(tokens: list[str]) -> tuple[list[str], list[str]]:
    """Words without redirection operators, plus the paths the segment writes to."""
    words: list[str] = []
    targets: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if _kind(tok) != "redirect":
            words.append(tok)
            i += 1
            continue
        if words and words[-1].isdigit():
            words.pop()
        target = tokens[i + 1] if i + 1 < n and _kind(tokens[i + 1]) == "word" else None
        if target is not None and _writes_path(tok, target):
            targets.append(target)
        i += 2 if target is not None else 1
    return words, targets


def _writes_path(op: str, target: str) -> bool:
    """Whether a redirection operator sends output to a file path."""
    if op in _READ_REDIRECTS:
        return False
    if op == ">&":
        return not (target.isdigit() or target == "-")
    return True
