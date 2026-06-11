"""Prover interface + the unit of work it returns."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Goal:
    """One proof obligation: a complete Lean file with a single `sorry` to close.

    `header` is everything above the theorem (imports, opens, options); `theorem`
    is the `theorem NAME ... := by` signature; `sorry_body` is the placeholder we
    ask a prover to replace. `context` is optional extra material (available
    lemmas, the Aeneas-generated function, a proof plan) injected into prompts.
    """
    name: str
    file_text: str               # the full Lean file, with `sorry` where the proof goes
    target_module: str = ""      # Lean module to `lake build` for the kernel gate
    context: str = ""            # extra premises / plan to put in the prompt


@dataclass
class ProofAttempt:
    """A single candidate proof + how it did."""
    goal_name: str
    tier: str                    # "tactic" | "local" | "frontier"
    proof_text: str              # the full Lean file the candidate produced
    accepted: bool = False
    # diagnostics
    build_ok: bool = False
    audit_ok: bool = False
    lean_errors: str = ""
    axioms: str = ""
    model: str = ""
    sampling: dict = field(default_factory=dict)
    wall_clock_s: float = 0.0


class Prover(Protocol):
    """A prover proposes candidate proof FILES for a goal. It NEVER decides
    acceptance — the Lean kernel (LeanRunner) + audit gate do. A wrong proposal
    is harmless: it simply fails the gate."""

    name: str

    def propose(self, goal: Goal, *, feedback: str = "") -> list[str]:
        """Return one or more complete candidate Lean files for `goal`.
        `feedback` carries Lean errors from a prior round (self-correction)."""
        ...
