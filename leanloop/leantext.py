"""Shared Lean source-text helpers. One nested-aware comment stripper used by
the audit gate, the runner's signature extraction, and goal discovery — so
`sorry`/`axiom` detection can't be fooled by nested `/- /- -/ -/` comments.
"""
from __future__ import annotations


def strip_comments(text: str) -> str:
    """Remove Lean comments. Block comments `/- ... -/` are NESTABLE in Lean,
    so we track depth; line comments `--` run to end of line. Newlines are
    preserved so line-based scanning still works on the result."""
    out: list[str] = []
    depth = 0
    i, n = 0, len(text)
    while i < n:
        two = text[i:i + 2]
        if depth == 0 and two == "--":
            j = text.find("\n", i)
            if j < 0:
                break
            out.append("\n")
            i = j + 1
            continue
        if two == "/-":
            depth += 1
            i += 2
            continue
        if two == "-/" and depth > 0:
            depth -= 1
            i += 2
            continue
        if depth == 0:
            out.append(text[i])
        else:
            # keep newlines inside block comments so line numbers/scan align
            if text[i] == "\n":
                out.append("\n")
        i += 1
    return "".join(out)
