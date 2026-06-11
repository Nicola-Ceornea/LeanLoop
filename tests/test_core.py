"""Tests for goal plumbing: tactic battery, FQN extraction, config loading."""
import textwrap

from leanloop import tactic_battery
from leanloop.config import Config
from leanloop.lean_runner import theorem_names


# --------------------------- tactic battery --------------------------------- #
def test_battery_replaces_by_sorry():
    src = "theorem t : 1 + 1 = 2 := by sorry\n"
    cands = tactic_battery.candidates(src, ["omega"])
    assert cands == [("omega", "theorem t : 1 + 1 = 2 := by omega\n")]


def test_battery_replaces_bare_sorry():
    src = "theorem t : 1 + 1 = 2 := by\n  sorry\n"
    (tac, cand), = tactic_battery.candidates(src, ["omega"])
    assert "sorry" not in cand and cand.rstrip().endswith("omega")


def test_battery_no_sorry_no_candidates():
    assert tactic_battery.candidates("theorem t : True := trivial\n", ["omega"]) == []


# --------------------------- FQN extraction --------------------------------- #
def test_fqn_with_namespace():
    src = textwrap.dedent("""
        import Aeneas
        namespace Extracted.Equiv
        theorem foo : True := trivial
        end Extracted.Equiv
    """)
    assert theorem_names(src) == ["Extracted.Equiv.foo"]


def test_fqn_nested_namespaces_and_sections():
    src = textwrap.dedent("""
        namespace A
        section
        theorem one : True := trivial
        end
        namespace B
        lemma two : True := trivial
        end B
        theorem three : True := trivial
        end A
    """)
    assert theorem_names(src) == ["A.one", "A.B.two", "A.three"]


def test_fqn_top_level():
    assert theorem_names("theorem bare : True := trivial\n") == ["bare"]


# --------------------------- config ----------------------------------------- #
def test_config_nested_toml(tmp_path):
    p = tmp_path / "leanloop.toml"
    p.write_text(textwrap.dedent("""
        [project]
        root = "/tmp/proj"

        [prover.local]
        base_url = "http://192.168.1.50:11434"
        samples = 8

        [audit]
        axiom_whitelist = ["keccak256_pure"]
    """))
    cfg = Config.load(p)
    # nested tables must become real dataclasses (PEP-563 regression guard)
    assert cfg.project.root == "/tmp/proj"
    assert cfg.prover.local.base_url == "http://192.168.1.50:11434"
    assert cfg.prover.local.samples == 8
    assert cfg.prover.local.model == "goedel-prover-v2-8b"  # default survives
    assert cfg.audit.axiom_whitelist == ["keccak256_pure"]


def test_config_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANLOOP_PROVER_URL", "http://gpu-box:11434")
    monkeypatch.setenv("LEANLOOP_FRONTIER_DISABLE", "1")
    cfg = Config.load(None)
    assert cfg.prover.local.base_url == "http://gpu-box:11434"
    assert cfg.prover.frontier.enabled is False
