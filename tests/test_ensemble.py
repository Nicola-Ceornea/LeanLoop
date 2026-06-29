"""EnsembleProver: pooling + dedup + member-failure tolerance + config parse."""
from leanloop.provers.base import Goal
from leanloop.provers.ensemble import EnsembleProver


class _Mock:
    def __init__(self, name, cands, *, boom=False):
        self.name = name
        self._cands = cands
        self._boom = boom

    def propose(self, goal, *, feedback=""):
        if self._boom:
            raise RuntimeError("endpoint down")
        return list(self._cands)


GOAL = Goal(name="t", file_text="theorem t : True := by sorry", context="")


def test_pools_in_member_order():
    e = EnsembleProver([_Mock("a", ["pa"]), _Mock("b", ["pb"])])
    assert e.propose(GOAL) == ["pa", "pb"]
    assert e.name == "ensemble(a+b)"


def test_dedup_across_members():
    # member b re-proposes member a's candidate (modulo trailing space) → once.
    e = EnsembleProver([_Mock("a", ["proof X"]), _Mock("b", ["proof X  ", "proof Y"])])
    assert e.propose(GOAL) == ["proof X", "proof Y"]


def test_down_member_is_skipped_not_fatal():
    e = EnsembleProver([_Mock("a", ["pa"]), _Mock("b", [], boom=True), _Mock("c", ["pc"])])
    assert e.propose(GOAL) == ["pa", "pc"]


def test_all_members_down_returns_empty():
    e = EnsembleProver([_Mock("a", [], boom=True), _Mock("b", [], boom=True)])
    assert e.propose(GOAL) == []


def test_empty_members_rejected():
    try:
        EnsembleProver([])
        assert False, "should have raised"
    except ValueError:
        pass


def test_config_parses_ensemble_list(tmp_path):
    from leanloop.config import Config
    toml = tmp_path / "c.toml"
    toml.write_text(
        "[prover.local]\nmodel = 'goedel'\n\n"
        "[[prover.ensemble]]\nmodel = 'deepseek-prover-v2-7b'\nbase_url = 'http://gpu2:11434'\n\n"
        "[[prover.ensemble]]\nmodel = 'kimina-prover-rl'\n"
    )
    cfg = Config.load(str(toml))
    assert cfg.prover.local.model == "goedel"
    assert [m.model for m in cfg.prover.ensemble] == ["deepseek-prover-v2-7b", "kimina-prover-rl"]
    # each ensemble member is a full LocalProverConfig with its own endpoint
    assert cfg.prover.ensemble[0].base_url == "http://gpu2:11434"
