"""KAT vector parsing, harness generation, output parsing."""
from leanloop.kat import (Vector, build_harness, parse_harness_output, parse_vectors,
                          validate, vectors_to_tsv)


# ----------------------------- parse_vectors -------------------------------- #
def test_jsonl():
    text = '{"name":"v0","input":"00","expected":"ab"}\n{"name":"v1","input":"01","expected":"aa"}\n'
    vs = parse_vectors(text, "jsonl")
    assert [v.name for v in vs] == ["v0", "v1"]
    assert vs[0].input_hex == "00" and vs[0].expected_hex == "ab"


def test_hex_columns():
    vs = parse_vectors("v0 00 ab\nv1 01 aa\n", "hex")
    assert len(vs) == 2 and vs[1].expected_hex == "aa"


def test_rsp():
    text = "Count = 0\nMsg = deadbeef\nOutput = c0ffee\n\nCount = 1\nMsg = 00\nOutput = ff\n"
    vs = parse_vectors(text, "rsp", input_key="Msg", expected_key="Output")
    assert len(vs) == 2
    assert vs[0].input_hex == "deadbeef" and vs[0].expected_hex == "c0ffee"


def test_auto_sniff():
    assert parse_vectors('{"input":"00","expected":"ab"}\n')[0].expected_hex == "ab"
    assert parse_vectors("Msg = 00\nOutput = ab\n", input_key="Msg", expected_key="Output")


# ----------------------------- validate ------------------------------------- #
def test_validate_catches_bad_hex():
    bad = [Vector("v", "0", "ab"), Vector("w", "00", "xyz")]
    problems = validate(bad)
    assert len(problems) == 2          # odd-length input, non-hex expected


def test_validate_ok():
    assert validate([Vector("v", "00ff", "abcd")]) == []


# ----------------------------- harness -------------------------------------- #
def test_build_harness_substitutes_and_keeps_interpolation():
    h = build_harness("Spec.Toy", "Toy.xorConst")
    assert "import Spec.Toy" in h
    assert ":= Toy.xorConst" in h
    assert 's!"KAT ' in h               # Lean string interpolation survives (no .format)
    assert "{module}" not in h and "{adapter}" not in h


def test_vectors_to_tsv_lowercases():
    tsv = vectors_to_tsv([Vector("v0", "00AB", "FFcd")])
    assert tsv.strip() == "v0\t00ab\tffcd"


# ----------------------------- output parsing ------------------------------- #
def test_parse_pass():
    r = parse_harness_output("KAT 4/4 passed")
    assert r.ran and r.passed == 4 and r.total == 4 and r.failures == []


def test_parse_fail():
    out = "FAIL v2: expected 00 got ab\nKAT 3/4 passed"
    r = parse_harness_output(out)
    assert r.ran and r.passed == 3 and r.total == 4
    assert r.failures == [("v2", "expected 00 got ab")]


def test_parse_did_not_run():
    r = parse_harness_output("error: type mismatch in adapter")
    assert not r.ran
