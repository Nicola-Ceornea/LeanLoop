"""Soundness regression tests for the fixes from the 2026-06 adversarial review.
These target the exact bypasses the review found; they must never regress.
"""
import textwrap

from leanloop.audit import audit_source
from leanloop.config import AuditConfig
from leanloop.leantext import strip_comments
from leanloop.lean_runner import theorem_names, theorem_signatures

CFG = AuditConfig(axiom_whitelist=["keccak256_pure"])


# --- CRITICAL #3: a candidate may not DECLARE axioms, even whitelisted names -- #
def test_candidate_axiom_rejected_even_if_whitelisted_name():
    src = "axiom keccak256_pure : False\ntheorem t : False := keccak256_pure"
    r = audit_source(src, CFG)
    assert not r.ok and "keccak256_pure" in r.reasons[0]


def test_candidate_any_axiom_rejected():
    assert not audit_source("axiom backdoor : False", CFG).ok


# --- HIGH #4/#8: nested + unclosed block comments ---------------------------- #
def test_nested_block_comment_sorry_hidden_from_lean_is_stripped():
    # `sorry` truly inside a (nested) comment -> Lean ignores it -> we must too
    src = "/- outer /- inner -/ sorry still-commented -/\ntheorem t : True := trivial\n"
    assert "sorry" not in strip_comments(src)
    assert audit_source(src, CFG).ok


def test_sorry_after_balanced_nested_comment_is_detected():
    # the comment closes; the trailing `sorry` is real code -> must be caught
    src = "/- /- -/ -/ theorem t : True := by sorry\n"
    assert "sorry" in strip_comments(src)
    assert not audit_source(src, CFG).ok


def test_line_comment_sorry_ignored():
    assert audit_source("theorem t : True := trivial -- sorry\n", CFG).ok


# --- statement pinning: signatures distinguish a swapped theorem ------------- #
def test_signature_detects_weakened_statement():
    goal = "theorem add_comm (a b : Nat) : a + b = b + a := by sorry\n"
    cheat = "theorem add_comm : True := trivial\n"
    gs = theorem_signatures(goal)
    cs = theorem_signatures(cheat)
    assert "add_comm" in gs and "add_comm" in cs
    assert gs["add_comm"] != cs["add_comm"]   # the pin catches the swap


def test_signature_matches_when_only_proof_differs():
    goal = "theorem t (a : Nat) : a = a := by sorry\n"
    proof = "theorem t (a : Nat) : a = a := by rfl\n"
    assert theorem_signatures(goal)["t"] == theorem_signatures(proof)["t"]


def test_signature_normalizes_whitespace_and_comments():
    a = "theorem t (a : Nat) : a = a := by sorry\n"
    b = "theorem t   (a : Nat)  /- c -/ :  a = a   := by rfl\n"
    assert theorem_signatures(a)["t"] == theorem_signatures(b)["t"]


def test_signature_namespaced():
    src = textwrap.dedent("""
        namespace Foo
        theorem bar (a : Nat) : a = a := by sorry
        end Foo
    """)
    sigs = theorem_signatures(src)
    assert "Foo.bar" in sigs
    assert sigs["Foo.bar"].startswith("theorem bar")


def test_example_has_no_pinnable_theorem():
    # `example` can't be #print-axioms'd; goals using only it must fail closed
    assert theorem_names("example : True := by sorry\n") == []
