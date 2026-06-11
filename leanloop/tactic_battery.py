"""Tier 0 — the non-LLM tactic battery. Run this BEFORE any model call: it's
free, fast, and (per the founding doc) closes a surprising fraction of the
arithmetic/bound side-conditions Aeneas produces.

Each entry becomes a candidate proof that replaces `:= by sorry` with `:= by
<tactic>`. We also try the Aeneas-idiomatic `step* <;> scalar_tac` opener. The
LeanRunner verifies each; the first that builds wins.
"""
from __future__ import annotations

import re

# An Aeneas-flavored cascade. Order matters: cheap/decisive first.
DEFAULT_OPENERS = [
    "scalar_tac",
    "simp_all",
    "omega",
    "grind",
    "decide",
    "(progress; scalar_tac)",
    "(step* <;> scalar_tac)",
    "(step* <;> simp_all)",
    "(unfold_let; scalar_tac)",
]

_SORRY_PROOF_RE = re.compile(r":=\s*by\s+sorry", re.M)
_BARE_SORRY_RE = re.compile(r"\bsorry\b")


def candidates(file_text: str, tactics: list[str] | None = None) -> list[tuple[str, str]]:
    """Return [(tactic, candidate_file_text)] replacing the goal's `sorry`.

    Handles both `:= by sorry` and a trailing `sorry` as the proof body.
    """
    # None => use the default cascade; [] => caller explicitly disabled Tier 0.
    tacs = DEFAULT_OPENERS if tactics is None else tactics
    out: list[tuple[str, str]] = []
    for tac in tacs:
        if _SORRY_PROOF_RE.search(file_text):
            cand = _SORRY_PROOF_RE.sub(f":= by {tac}", file_text, count=1)
        else:
            # replace the last bare `sorry` token
            cand = _replace_last(file_text, "sorry", tac)
        if cand != file_text:
            out.append((tac, cand))
    return out


def _replace_last(text: str, token: str, repl: str) -> str:
    idx = text.rfind(token)
    if idx < 0:
        return text
    return text[:idx] + repl + text[idx + len(token):]
