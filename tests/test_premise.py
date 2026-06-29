"""Premise load-bearingness: binder parsing, droppable-leaf detection, variant
construction, and necessity scoring."""
from leanloop.premise import (Variant, NecessityResult, build_variants, classify,
                              droppable_hyps, score, split_binder_groups, theorem_spans)


# ----------------------------- binder parsing ------------------------------- #
def test_split_binder_groups_kinds_and_names():
    gs = split_binder_groups("(a b : Nat) {x : T} [inst : C] (h : a < x)")
    assert [g.kind for g in gs] == ["(", "{", "[", "("]
    assert gs[0].names == ["a", "b"] and gs[0].type_src == "Nat"
    assert gs[3].names == ["h"] and gs[3].type_src == "a < x"


def test_split_handles_nested_brackets():
    gs = split_binder_groups("(f : (Nat -> Nat))")
    assert len(gs) == 1 and gs[0].type_src == "(Nat -> Nat)"


# --------------------------- droppable leaves ------------------------------- #
def test_droppable_picks_leaf_assumption_only():
    # x is a PARAMETER (used in conclusion); h is a leaf ASSUMPTION.
    drops = droppable_hyps("(x : Nat) (h : x < 100)", "x + 1 < 101")
    assert [d.text for d in drops] == ["(h : x < 100)"]


def test_droppable_skips_implicit_and_instance():
    drops = droppable_hyps("{x : T} [inst : C] (h : P)", "Q")
    assert [d.names for d in drops] == [["h"]]   # only the explicit leaf


def test_droppable_skips_hyp_used_by_later_binder():
    # h is referenced in h2's type -> structurally needed, NOT droppable.
    # h2 is a leaf (nothing references it) -> droppable. So h is excluded, h2 kept.
    drops = droppable_hyps("(h : P) (h2 : Q h)", "R")
    assert [d.names for d in drops] == [["h2"]]


def test_droppable_two_independent_assumptions():
    drops = droppable_hyps("(h1 : P) (h2 : Q)", "R")
    assert {d.text for d in drops} == {"(h1 : P)", "(h2 : Q)"}


# --------------------------- theorem spans ---------------------------------- #
def test_theorem_spans_namespace_aware():
    src = ("namespace Foo\n"
           "theorem bar (h : P) : Q := by exact h\n"
           "end Foo\n")
    sps = theorem_spans(src)
    assert len(sps) == 1 and sps[0].fqn == "Foo.bar"
    assert sps[0].conclusion.strip() == "Q"


# --------------------------- variant construction --------------------------- #
def test_build_variants_drops_the_binder_from_signature():
    src = "theorem t (n : Nat) (h : n < 5) : n < 10 := by omega\n"
    vs = build_variants(src)
    # n is a parameter (in conclusion) -> not dropped; h is the leaf assumption.
    assert len(vs) == 1
    assert vs[0].dropped == "(h : n < 5)"
    assert "(h : n < 5)" not in vs[0].code
    assert "(n : Nat)" in vs[0].code and ": n < 10" in vs[0].code


def test_build_variants_none_when_no_leaf_hyp():
    # every binder is a parameter referenced in the conclusion -> nothing to drop.
    src = "theorem id2 (x : Nat) : x = x := rfl\n"
    assert build_variants(src) == []


# ----------------------------- classify/score ------------------------------- #
def test_classify_lived_on_clean_build():
    assert classify(True, "", ["h"]) == "lived"


def test_classify_killed_on_unknown_identifier():
    out = "error: unknown identifier 'h'"
    assert classify(False, out, ["h"]) == "killed"


def test_classify_killed_on_proof_failure():
    assert classify(False, "error: unsolved goals", ["h"]) == "killed"


def test_classify_unviable_on_unrelated_break():
    assert classify(False, "error: file not found in import graph", ["h"]) == "unviable"


def test_score_counts_dead_premises():
    v = Variant("t", "(h : P)", "rationale", code="")
    res = [
        NecessityResult(v, "lived"),     # dead premise
        NecessityResult(v, "killed"),    # load-bearing
        NecessityResult(v, "killed"),
        NecessityResult(v, "unviable"),  # excluded from score
    ]
    sc = score(res)
    assert sc["dead_premises"] == 1 and sc["load_bearing"] == 2
    assert sc["necessity_score"] == round(100 * 2 / 3, 1)
    assert len(sc["dead"]) == 1 and "dead premise" in sc["dead"][0]
