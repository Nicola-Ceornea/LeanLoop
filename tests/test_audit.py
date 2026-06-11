"""The audit gate is the soundness boundary — test it adversarially."""
from leanloop.audit import audit_axioms, audit_source
from leanloop.config import AuditConfig

CFG = AuditConfig(axiom_whitelist=["keccak256_pure"])


# --------------------------- source audit ----------------------------------- #
def test_rejects_sorry():
    r = audit_source("theorem t : True := by sorry", CFG)
    assert not r.ok and "sorry" in r.reasons[0]


def test_rejects_admit():
    assert not audit_source("theorem t : True := by admit", CFG).ok


def test_allows_sorry_in_comments_only():
    src = "-- this used to be sorry\n/- sorry lives here -/\ntheorem t : True := trivial\n"
    assert audit_source(src, CFG).ok


def test_rejects_new_axiom():
    r = audit_source("axiom evil : False\ntheorem t : False := evil", CFG)
    assert not r.ok and "evil" in r.reasons[0]


def test_candidate_axiom_rejected_even_when_name_whitelisted():
    # the whitelist applies to the axiom CLOSURE, never to source `axiom` decls:
    # a candidate proof must not DECLARE axioms (else it could ride the name).
    assert not audit_source("axiom keccak256_pure : Nat -> Nat", CFG).ok


def test_rejects_native_decide():
    r = audit_source("theorem t : 2 + 2 = 4 := by native_decide", CFG)
    assert not r.ok and "native_decide" in r.reasons[0]


def test_word_boundary_no_false_positive():
    # identifiers merely containing the substrings must not trip the gate
    src = "theorem unsorried : True := trivial\ndef axiomatic_style := 1\n"
    assert audit_source(src, CFG).ok


# --------------------------- axiom-closure audit ---------------------------- #
def test_clean_closure_accepted():
    out = "'t' depends on axioms: [propext, Classical.choice, Quot.sound]"
    assert audit_axioms(out, CFG, expected=1).ok


def test_whitelisted_content_axiom_accepted():
    out = "'t' depends on axioms: [keccak256_pure, propext]"
    assert audit_axioms(out, CFG, expected=1).ok


def test_sorryax_rejected():
    out = "'t' depends on axioms: [propext, sorryAx]"
    r = audit_axioms(out, CFG, expected=1)
    assert not r.ok and any("sorryAx" in x for x in r.reasons)


def test_unreviewed_axiom_rejected():
    out = "'t' depends on axioms: [propext, backdoor_axiom]"
    assert not audit_axioms(out, CFG, expected=1).ok


def test_fail_closed_when_closure_missing():
    """The critical property: if the checker couldn't resolve the theorem
    (e.g. unknown constant), the gate must FAIL, not pass vacuously."""
    out = "error: unknown constant 't'"
    assert not audit_axioms(out, CFG, expected=1).ok


def test_fail_closed_on_partial_closure():
    out = "'a' depends on axioms: [propext]"  # expected two theorems
    assert not audit_axioms(out, CFG, expected=2).ok


def test_no_axioms_at_all_is_clean():
    out = "'t' does not depend on any axioms"
    assert audit_axioms(out, CFG, expected=1).ok
