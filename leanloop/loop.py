"""The orchestration loop: for each goal, escalate through the tiers
(tactic battery -> local prover pass@N + self-correction -> frontier), and the
Lean kernel + audit gate decide acceptance at every step.

The kernel is ground truth: a candidate is ACCEPTED iff it builds AND passes
the source audit AND its `#print axioms` closure is within the allowed set.
Nothing a model emits can bypass this.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from rich.console import Console

from . import audit, tactic_battery
from .config import Config
from .db import RunDB
from .lean_runner import LeanRunner, theorem_signatures
from .provers.base import Goal, ProofAttempt
from .provers.frontier import FrontierProver
from .provers.ollama import LocalProver

console = Console()


@dataclass
class GoalOutcome:
    goal_name: str
    accepted: bool
    tier: str = ""
    proof_text: str = ""


class Orchestrator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.runner = LeanRunner(cfg.project)
        self.db = RunDB(cfg.db_path)
        self.local = LocalProver(cfg.prover.local) if cfg.prover.local.enabled else None
        self.frontier = FrontierProver(cfg.prover.frontier) if cfg.prover.frontier.enabled else None
        self._goal_sigs: dict[str, str] = {}   # pinned per goal in prove()

    # ------------------------------------------------------------------ #
    def prove(self, goal: Goal, *, module_stem: str) -> GoalOutcome:
        if self.db.is_solved(goal.name):
            console.print(f"[dim]{goal.name}: already solved (cached)[/]")
            return GoalOutcome(goal.name, True, "cached")

        console.rule(f"[bold]{goal.name}")

        # PIN the goal's theorems up front: a candidate is only accepted if it
        # proves EXACTLY these names with these (normalized) signatures. Without
        # this, a model could submit `theorem foo : True := trivial` and pass.
        self._goal_sigs = theorem_signatures(goal.file_text)
        if not self._goal_sigs:
            console.print(f"[red]✗ {goal.name}: no named theorem/lemma to prove "
                          f"(goals must declare a `theorem`/`lemma`, not `example`)[/]")
            return GoalOutcome(goal.name, False)

        # --- Tier 0: tactic battery (free) ---
        for tac, cand in tactic_battery.candidates(goal.file_text, self.cfg.prover.tactic_battery):
            att = self._verify(goal, cand, tier="tactic", module_stem=module_stem,
                               model=f"tactic:{tac}")
            if att.accepted:
                console.print(f"[green]✓ closed by tactic[/] `{tac}`")
                return GoalOutcome(goal.name, True, "tactic", cand)

        # --- Tier 1: local prover pass@N with self-correction ---
        if self.local:
            outcome = self._prover_rounds(goal, self.local, "local", module_stem)
            if outcome.accepted:
                return outcome

        # --- Tier 2: frontier hands-off ---
        if self.frontier and self.cfg.prover.frontier.enabled:
            outcome = self._prover_rounds(goal, self.frontier, "frontier", module_stem,
                                          rounds=self.cfg.prover.local.self_correct_rounds)
            if outcome.accepted:
                return outcome

        console.print(f"[yellow]✗ {goal.name}: open after all tiers (flag for review)[/]")
        return GoalOutcome(goal.name, False)

    # ------------------------------------------------------------------ #
    def _prover_rounds(self, goal: Goal, prover, tier: str, module_stem: str,
                       rounds: int | None = None) -> GoalOutcome:
        rounds = rounds if rounds is not None else self.cfg.prover.local.self_correct_rounds
        feedback = ""
        for rnd in range(rounds + 1):
            label = f"{tier} round {rnd}" + (" (self-correct)" if rnd else "")
            console.print(f"[cyan]{label}: sampling…[/]")
            candidates = prover.propose(goal, feedback=feedback)
            best_errors = ""
            for cand in candidates:
                att = self._verify(goal, cand, tier=tier, module_stem=module_stem,
                                   model=getattr(prover.cfg, "model", tier),
                                   sampling={"round": rnd})
                if att.accepted:
                    console.print(f"[green]✓ closed by {tier}[/] (round {rnd})")
                    return GoalOutcome(goal.name, True, tier, cand)
                if att.build_ok and not att.audit_ok:
                    # built but failed audit — keep its axioms as the feedback
                    best_errors = best_errors or f"audit failed: {att.axioms}"
                elif att.lean_errors and not best_errors:
                    best_errors = att.lean_errors
            feedback = best_errors  # feed the most informative failure into next round
            if not candidates:
                break
        return GoalOutcome(goal.name, False)

    # ------------------------------------------------------------------ #
    def _verify(self, goal: Goal, candidate: str, *, tier: str, module_stem: str,
                model: str = "", sampling: dict | None = None) -> ProofAttempt:
        att = ProofAttempt(goal_name=goal.name, tier=tier, proof_text=candidate,
                           model=model, sampling=sampling or {})
        t0 = time.time()

        def done(reason: str = "") -> ProofAttempt:
            if reason:
                att.lean_errors = reason
            att.wall_clock_s = time.time() - t0
            self.db.log(att)
            return att

        # 1) STATEMENT-PINNING gate: the candidate must contain every goal
        # theorem with an identical normalized signature (name + binders + type).
        # This is what stops a model from proving a different/weaker statement.
        cand_sigs = theorem_signatures(candidate)
        for name, want in self._goal_sigs.items():
            if name not in cand_sigs:
                return done(f"statement pin: candidate is missing required theorem `{name}`")
            if cand_sigs[name] != want:
                return done(f"statement pin: theorem `{name}` signature changed\n"
                            f"  required: {want}\n  candidate: {cand_sigs[name]}")

        # 2) cheap source audit (sorry/axiom/native_decide) before building
        src = audit.audit_source(candidate, self.cfg.audit)
        if not src.ok:
            return done("source audit: " + "; ".join(src.reasons))

        # 3) kernel gate (build) + axiom gate in one write/restore window.
        # We pin the axiom check to the GOAL's theorem FQNs (not the candidate's)
        # and require exactly that many resolved closures (fail-closed).
        goal_fqns = list(self._goal_sigs.keys())
        res = self.runner.verify(candidate, goal_fqns, module_stem=module_stem)
        att.build_ok = res.build_ok
        att.axioms = res.axioms
        if not res.build_ok:
            return done(res.errors)

        ax = audit.audit_axioms(res.axioms, self.cfg.audit, expected=len(goal_fqns))
        att.audit_ok = ax.ok
        att.accepted = att.build_ok and att.audit_ok
        if not ax.ok:
            return done("axiom audit: " + "; ".join(ax.reasons))
        return done()

    def close(self) -> None:
        self.db.close()
