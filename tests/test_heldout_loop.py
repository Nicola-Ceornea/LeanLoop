"""Held-out goals stay inside the ordinary statement/kernel/axiom gates."""

from leanloop.config import Config
from leanloop.lean_runner import VerifyResult, theorem_signatures
from leanloop.loop import Orchestrator
from leanloop.provers.base import Goal


SOURCE = """theorem keep : True := by trivial
theorem target : True := by sorry
"""


class _DB:
    def __init__(self):
        self.logged = []

    def log(self, attempt, *, goal_hash=""):
        self.logged.append(attempt)


class _Runner:
    def __init__(self, axioms="'target' does not depend on any axioms"):
        self.axioms = axioms
        self.calls = []

    def verify(self, candidate, theorem_fqns, *, module_stem):
        self.calls.append((candidate, theorem_fqns, module_stem))
        return VerifyResult(build_ok=True, axioms=self.axioms)


def _orchestrator(axioms="'target' does not depend on any axioms"):
    orch = Orchestrator.__new__(Orchestrator)
    orch.cfg = Config()
    orch.db = _DB()
    orch.runner = _Runner(axioms)
    orch._goal_hash = "hash"
    orch._goal_sigs = {"target": theorem_signatures(SOURCE)["target"]}
    return orch


def _goal(whitelist=None):
    start = SOURCE.index("by sorry")
    return Goal(
        name="heldout-target",
        file_text=SOURCE,
        target_module="Bench",
        target_fqns=["target"],
        proof_hole=(start, start + len("by sorry")),
        axiom_whitelist=whitelist,
    )


def test_heldout_signature_pin_selects_only_target():
    orch = _orchestrator()
    assert list(orch._pin_signatures(_goal())) == ["target"]


def test_model_cannot_change_trusted_scaffold():
    orch = _orchestrator()
    goal = _goal([])
    candidate = goal.materialize_proof("by trivial").replace("theorem keep", "lemma keep")
    attempt = orch._verify(goal, candidate, tier="local", module_stem="Bench")
    assert not attempt.accepted
    assert "trusted scaffold" in attempt.lean_errors
    assert orch.runner.calls == []


def test_model_cannot_escape_hole_with_a_new_command():
    orch = _orchestrator()
    goal = _goal([])
    candidate = goal.materialize_proof(
        "by trivial\nset_option pp.universes true in\n#check True"
    )
    attempt = orch._verify(goal, candidate, tier="local", module_stem="Bench")
    assert not attempt.accepted
    assert "escapes the designated proof expression" in attempt.lean_errors
    assert orch.runner.calls == []


def test_proof_only_candidate_must_be_a_by_expression():
    orch = _orchestrator()
    goal = _goal([])
    candidate = goal.materialize_proof("True.intro")
    attempt = orch._verify(goal, candidate, tier="local", module_stem="Bench")
    assert not attempt.accepted
    assert "not one `by ...` proof expression" in attempt.lean_errors
    assert orch.runner.calls == []


def test_per_goal_axiom_whitelist_overrides_broad_project_config():
    closure = "'target' depends on axioms: [trusted.hash]"
    orch = _orchestrator(closure)
    orch.cfg.audit.axiom_whitelist = ["trusted.hash", "unrelated.hash"]
    goal = _goal([])
    candidate = goal.materialize_proof("by trivial")
    attempt = orch._verify(goal, candidate, tier="local", module_stem="Bench")
    assert not attempt.accepted
    assert "unreviewed axiom `trusted.hash`" in attempt.lean_errors

    orch = _orchestrator(closure)
    allowed = _goal(["trusted.hash"])
    attempt = orch._verify(
        allowed, allowed.materialize_proof("by trivial"), tier="local", module_stem="Bench"
    )
    assert attempt.accepted
    assert orch.runner.calls[0][1] == ["target"]
