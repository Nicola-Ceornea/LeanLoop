"""Spec-vet pure logic: signature splitting, probe construction, verdicts."""
from leanloop.spec_vet import Probe, build_probes, judge, split_signature


SIG = ("theorem lor_eq_add_disjoint (val x b : Nat) (hval : val < 2 ^ (8 * b)) "
       "(hx : x < 256) : val ||| (x <<< (8 * b)) = val + x * 2 ^ (8 * b)")


# ----------------------------- split_signature ------------------------------ #
def test_split_top_level_colon():
    name, binders, concl = split_signature(SIG)
    assert name == "lor_eq_add_disjoint"
    assert binders.endswith("(hx : x < 256)")       # binder colons not split on
    assert concl.startswith("val |||")


def test_split_no_binders():
    name, binders, concl = split_signature("theorem t : True")
    assert name == "t" and binders == "" and concl == "True"


def test_split_handles_brackets():
    s = "theorem t {α : Type} [DecidableEq α] (xs : List α) : xs = xs"
    name, binders, concl = split_signature(s)
    assert concl == "xs = xs" and "[DecidableEq α]" in binders


def test_split_unrecognizable():
    assert split_signature("def foo := 1") is None


# ----------------------------- build_probes --------------------------------- #
def test_probes_built_per_theorem():
    goal = "import Aeneas\nnamespace N\ntheorem t (a : Nat) : a = a := by sorry\nend N\n"
    probes = build_probes(goal, {"N.t": "theorem t (a : Nat) : a = a"})
    kinds = sorted(p.kind for p in probes)
    assert kinds == ["cex", "hyp", "neg"]
    cex = next(p for p in probes if p.kind == "cex")
    assert "import Plausible" in cex.file_text
    # the probe re-opens the namespace AFTER the original file's `end N`
    assert "namespace N\ntheorem _vet_cex_t" in cex.file_text
    assert "plausible" in cex.file_text
    hyp = next(p for p in probes if p.kind == "hyp")
    assert ": False := by plausible" in hyp.file_text
    neg = next(p for p in probes if p.kind == "neg")
    assert "¬ (a = a)" in neg.file_text and "| sorry" in neg.file_text


# ----------------------------- judge ----------------------------------------- #
def _p(kind):
    return Probe(kind, "N.t", "")


def test_cex_found_is_red():
    v = judge(_p("cex"), "===\nFound a counter-example!\na := 0\n-------\n")
    assert v.level == "red" and "a := 0" in v.detail


def test_cex_none_is_green():
    assert judge(_p("cex"), "Unable to find a counter-example").level == "green"


def test_cex_gave_up_is_yellow():
    out = "warning: Gave up after failing to generate values that fulfill the preconditions 100 times."
    assert judge(_p("cex"), out).level == "yellow"


def test_hyp_witness_is_green():
    assert judge(_p("hyp"), "Found a counter-example!\na := 3\n-------").level == "green"


def test_hyp_gave_up_is_yellow_vacuous():
    out = "warning: Gave up after failing to generate values that fulfill the preconditions 100 times."
    v = judge(_p("hyp"), out)
    assert v.level == "yellow" and "vacuous" in v.message.lower()


def test_neg_sorry_fallback_is_green():
    assert judge(_p("neg"), "warning: declaration uses `sorry`").level == "green"


def test_neg_proved_is_red():
    # builds clean, no sorry warning => a tactic proved the negation
    assert judge(_p("neg"), "Build completed successfully").level == "red"


def test_plausible_missing_is_skip():
    v = judge(_p("cex"), "error: unknown module prefix 'Plausible'")
    assert v.level == "skip" and "plausible" in v.message.lower()
