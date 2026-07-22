"""Shared Lean source-text helpers. One nested-aware comment stripper used by
the audit gate, the runner's signature extraction, and goal discovery — so
`sorry`/`axiom` detection can't be fooled by nested `/- /- -/ -/` comments.
"""
from __future__ import annotations


def mask_comments(text: str) -> str:
    """Replace Lean comments with whitespace without changing text length.

    Unlike :func:`strip_comments`, this helper preserves *every* character
    offset.  It is intended for source-location work where positions found in
    comment-free code must still index the original source.  Newline and
    carriage-return characters are retained; every other character belonging
    to a comment (including the delimiters) becomes a space.

    Lean block comments nest.  Comment-looking text inside an ordinary string
    literal is code, not a comment, so the small lexer also tracks escaped
    quotes.  Unterminated comments are masked through EOF; the Lean compiler is
    still responsible for rejecting malformed source.
    """
    out = list(text)
    depth = 0
    in_string = False
    escaped = False
    i, n = 0, len(text)

    def blank(pos: int) -> None:
        if text[pos] not in "\r\n":
            out[pos] = " "

    while i < n:
        two = text[i:i + 2]

        if depth:
            if two == "/-":
                blank(i)
                if i + 1 < n:
                    blank(i + 1)
                depth += 1
                i += 2
                continue
            if two == "-/":
                blank(i)
                if i + 1 < n:
                    blank(i + 1)
                depth -= 1
                i += 2
                continue
            blank(i)
            i += 1
            continue

        if in_string:
            if escaped:
                escaped = False
            elif text[i] == "\\":
                escaped = True
            elif text[i] == '"':
                in_string = False
            i += 1
            continue

        if text[i] == '"':
            in_string = True
            i += 1
            continue
        if two == "--":
            blank(i)
            if i + 1 < n:
                blank(i + 1)
            i += 2
            while i < n and text[i] not in "\r\n":
                blank(i)
                i += 1
            continue
        if two == "/-":
            blank(i)
            if i + 1 < n:
                blank(i + 1)
            depth = 1
            i += 2
            continue
        i += 1

    return "".join(out)


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
