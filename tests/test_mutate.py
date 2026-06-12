"""Mutation operators, application, and mCoq scoring."""
from leanloop.mutate import (Mutant, MutantResult, apply_mutant, generate_mutants,
                             parse_cargo_mutants_list, score)


# ----------------------------- generate ------------------------------------- #
def test_generates_crypto_and_const_mutants():
    src = "def f (xs : List UInt8) : List UInt8 := xs.map (· ^^^ (0xAB : UInt8))\n"
    ms = generate_mutants("F.lean", src)
    ops = {m.op for m in ms}
    assert "crypto-byte-flip" in ops          # 0xAB -> 0xAA
    byte = next(m for m in ms if m.op == "crypto-byte-flip")
    assert byte.original == "0xAB" and byte.mutated == "0xAA"


def test_skips_proof_lines():
    src = "theorem t : 1 < 2 := by omega\n"
    assert generate_mutants("T.lean", src) == []         # proofs never mutated


def test_const_off_by_one():
    src = "def n := 143\n"
    ms = generate_mutants("N.lean", src)
    off = next(m for m in ms if m.op == "const-off-by-one")
    assert off.original == "143" and off.mutated == "144"


def test_bitand_to_bitor():
    src = "def g (a b : Nat) := a &&& b\n"
    ms = generate_mutants("G.lean", src)
    assert any(m.op == "bitand-to-bitor" and m.mutated == "|||" for m in ms)


# ----------------------------- apply ---------------------------------------- #
def test_apply_replaces_exact_occurrence():
    src = "def f := 0xAB\n"
    m = Mutant("crypto-byte-flip", "F.lean", 1, 9, "0xAB", "0xAA")
    assert apply_mutant(src, m) == "def f := 0xAA\n"


def test_apply_robust_to_comment_shift():
    # the col came from the comment-stripped view; apply re-finds on the real line
    src = "def f := 0xAB -- a comment\n"
    m = Mutant("crypto-byte-flip", "F.lean", 1, 9, "0xAB", "0xAA")
    assert apply_mutant(src, m) == "def f := 0xAA -- a comment\n"


# ----------------------------- score (mCoq) --------------------------------- #
def _mr(op, outcome):
    return MutantResult(Mutant(op, "f", 1, 0, "x", "y"), outcome)


def test_score_excludes_unviable():
    results = [_mr("a", "killed"), _mr("b", "killed"), _mr("c", "lived"),
               _mr("d", "unviable"), _mr("e", "timeout")]
    s = score(results)
    # score = killed/(killed+lived) = 2/3, unviable+timeout excluded
    assert s["mutation_score"] == 66.7
    assert s["killed"] == 2 and s["lived"] == 1 and s["unviable"] == 1
    assert len(s["survivors"]) == 1


def test_score_all_killed_is_100():
    assert score([_mr("a", "killed"), _mr("b", "killed")])["mutation_score"] == 100.0


def test_score_no_scorable_is_none():
    assert score([_mr("a", "unviable")])["mutation_score"] is None


# ----------------------------- cargo-mutants parse -------------------------- #
def test_parse_cargo_mutants_list():
    js = '[{"name":"src/x.rs: replace + with -","diff":"- a + b\\n+ a - b"}]'
    out = parse_cargo_mutants_list(js)
    assert out[0]["name"].startswith("src/x.rs") and "a - b" in out[0]["diff"]


def test_parse_cargo_mutants_garbage():
    assert parse_cargo_mutants_list("not json") == []


def test_skips_directive_lines():
    # set_option / import / open / attribute are non-semantic — equivalent
    # mutants (the 2026-06 FORS run produced a false-positive on maxRecDepth 2048)
    from leanloop.mutate import generate_mutants
    src = ("set_option maxRecDepth 2048\nimport Foo.Bar\nattribute [simp] x\n"
           "def f := 0xAB\n")
    ms = generate_mutants("F.lean", src)
    assert all(m.line_no == 4 for m in ms)   # only the `def` line is mutated
    assert ms and ms[0].original == "0xAB"
