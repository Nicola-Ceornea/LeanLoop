"""Prover ensembling — run several prover backends and pool their candidates.

The founding insight behind pass@N is that independent samples decorrelate
failures; ENSEMBLING extends that across MODELS. Goedel-Prover-V2, DeepSeek-
Prover-V2-7B and Kimina-Prover-RL fail on *different* goals, so the union of
their proposals closes more goals than pass@N on any one — for the same total
sample budget, a diverse ensemble dominates a single model (the standard
prover-ensembling result).

`EnsembleProver` is itself a `Prover` (same `propose() -> list[str]` Protocol),
so it is a drop-in wherever a single prover was used: the loop's gate (Lean
kernel + audit) still decides acceptance, so a weak member is harmless — its
proposals simply fail the gate. Members are typically `LocalProver`s pointed at
DIFFERENT endpoints/models (e.g. Goedel on the 6950 XT, DeepSeek-Prover on a
rented endpoint), so we call them CONCURRENTLY and a dead endpoint never blocks
the others.
"""
from __future__ import annotations

import concurrent.futures as cf

from .base import Goal, Prover


def _norm(text: str) -> str:
    """Whitespace-insensitive key for dedup — two members proposing the same
    proof modulo trailing spaces / blank lines shouldn't double-count."""
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


class EnsembleProver:
    """Pool the proposals of several `Prover` members for one goal.

    Order: member 0's candidates first (so a `[goedel, deepseek, kimina]` list
    front-loads the primary model), then each subsequent member's NEW (unseen)
    candidates, de-duplicated by normalized text. A member that raises (a down
    endpoint, a timeout) contributes nothing and is skipped — never fatal.
    """

    def __init__(self, members: list[Prover], *, max_workers: int | None = None):
        if not members:
            raise ValueError("EnsembleProver needs at least one member")
        self.members = members
        self._max_workers = max_workers or len(members)
        self.name = "ensemble(" + "+".join(getattr(m, "name", "?") for m in members) + ")"

    def propose(self, goal: Goal, *, feedback: str = "") -> list[str]:
        # Call members concurrently; preserve member ORDER in the merged result
        # regardless of which finishes first (results indexed by member).
        results: list[list[str]] = [[] for _ in self.members]
        with cf.ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futs = {
                pool.submit(self._safe_propose, m, goal, feedback): i
                for i, m in enumerate(self.members)
            }
            for fut in cf.as_completed(futs):
                results[futs[fut]] = fut.result()

        merged: list[str] = []
        seen: set[str] = set()
        for cand_list in results:
            for cand in cand_list:
                key = _norm(cand)
                if key and key not in seen:
                    seen.add(key)
                    merged.append(cand)
        return merged

    @staticmethod
    def _safe_propose(member: Prover, goal: Goal, feedback: str) -> list[str]:
        try:
            return list(member.propose(goal, feedback=feedback))
        except Exception:  # a down endpoint / timeout must not kill the round
            return []
