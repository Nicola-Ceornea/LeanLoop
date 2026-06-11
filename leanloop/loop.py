"""The orchestration loop: for each goal, escalate through the tiers
(tactic battery -> local prover pass@N + self-correction -> frontier), and the
Lean kernel + audit gate decide acceptance at every step.

The kernel is ground truth: a candidate is ACCEPTED iff it builds AND passes
the source audit AND its `#print axioms` closure is within the allowed set.
Nothing a model emits can bypass this.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from rich.console import Console

from . import audit, frontier_queue, tactic_battery
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
    def __init__(self, cfg: Config, *, config_path: str = ""):
        self.cfg = cfg
        self.config_path = config_path     # surfaced in queued-task instructions
        self.runner = LeanRunner(cfg.project)
        self.db = RunDB(cfg.db_path)
        self.local = LocalProver(cfg.prover.local) if cfg.prover.local.enabled else None
        fb = cfg.prover.frontier
        # `queue` is handled by the loop directly (enqueue), not a Prover.
        self.frontier = (FrontierProver(fb)
                         if fb.enabled and fb.backend in ("claude_cli", "openai") else None)
        self._goal_sigs: dict[str, str] = {}   # pinned per goal in prove()
        self._goal_hash: str = ""              # content hash, keys the solved cache
        self._last_feedback: str = ""          # best local failure, for queued tasks

    # ------------------------------------------------------------------ #
    def prove(self, goal: Goal, *, module_stem: str) -> GoalOutcome:
        goal_hash = hashlib.sha256(goal.file_text.encode()).hexdigest()[:16]
        self._goal_hash = goal_hash
        if self.db.is_solved(goal.name, goal_hash):
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
            if self._ensure_prover():
                outcome = self._prover_rounds(goal, self.local, "local", module_stem)
                if outcome.accepted:
                    return outcome
            else:
                console.print("[yellow]⚠ local prover unreachable — skipping Tier 1 "
                              "for this goal[/]")

        # --- Tier 2: frontier ---
        fb = self.cfg.prover.frontier
        if fb.enabled and fb.backend == "queue":
            # hand off to an interactive Claude Code session (flat-rate)
            task = frontier_queue.enqueue(
                goal, module_stem=module_stem, project_root=str(self.runner.root),
                queue_dir=self.cfg.frontier_queue_dir, config_path=self.config_path,
                allowed_axioms=self.cfg.audit.allowed_kernel_axioms,
                whitelist=self.cfg.audit.axiom_whitelist, feedback=self._last_feedback)
            console.print(f"[magenta]→ queued for interactive frontier:[/] {task.md_path}")
            return GoalOutcome(goal.name, False, "queued")
        if self.frontier:
            outcome = self._prover_rounds(goal, self.frontier, "frontier", module_stem,
                                          rounds=self.cfg.prover.local.self_correct_rounds)
            if outcome.accepted:
                return outcome

        console.print(f"[yellow]✗ {goal.name}: open after all tiers (flag for review)[/]")
        return GoalOutcome(goal.name, False)

    # ------------------------------------------------------------------ #
    def _ensure_prover(self) -> bool:
        """Wait for the local prover to be reachable, surviving a transient
        Ollama crash/OOM or a GPU-box reboot. Returns False if it stays down
        past `health_max_wait_s` (the caller then skips Tier 1 for this goal)."""
        ok, msg = self.local.health()
        if ok:
            return True
        max_wait = self.cfg.prover.local.health_max_wait_s
        if max_wait <= 0:
            console.print(f"[yellow]⚠ prover down ({msg})[/]")
            return False
        console.print(f"[yellow]⚠ prover down ({msg}) — waiting up to "
                      f"{int(max_wait)}s for it to recover…[/]")
        waited, delay = 0.0, 10.0
        while waited < max_wait:
            time.sleep(min(delay, max_wait - waited))
            waited += delay
            delay = min(delay * 2, 60.0)   # backoff, capped at 60s
            ok, msg = self.local.health()
            if ok:
                console.print(f"[green]✓ prover recovered after {int(waited)}s[/]")
                return True
        console.print(f"[red]✗ prover still down after {int(waited)}s[/]")
        return False

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
            if best_errors:
                self._last_feedback = best_errors  # context for a queued frontier task
            if not candidates:
                break
        return GoalOutcome(goal.name, False)

    # ------------------------------------------------------------------ #
    def submit(self, goal: Goal, candidate: str, *, module_stem: str,
               model: str = "interactive-frontier") -> ProofAttempt:
        """Verify a candidate produced OUTSIDE the loop (e.g. by an interactive
        Claude Code session clearing the frontier queue) through the SAME gates.
        Returns the attempt; the caller applies it on acceptance."""
        self._goal_hash = hashlib.sha256(goal.file_text.encode()).hexdigest()[:16]
        self._goal_sigs = theorem_signatures(goal.file_text)
        if not self._goal_sigs:
            att = ProofAttempt(goal_name=goal.name, tier="frontier", proof_text=candidate)
            att.lean_errors = "goal declares no named theorem/lemma to pin"
            return att
        return self._verify(goal, candidate, tier="frontier", module_stem=module_stem,
                            model=model)

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
            self.db.log(att, goal_hash=self._goal_hash)
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
