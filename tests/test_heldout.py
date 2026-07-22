"""Fail-closed held-out benchmark suite construction."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from leanloop.heldout import HeldoutError, discover_proof_spans, load_suite
from leanloop.lean_runner import theorem_signatures
from leanloop.leantext import mask_comments, strip_comments


MODULE = "Bench.Fixture"
TARGET = "Bench.Fixture.target"
PLACEHOLDER = "by\n  set_option maxRecDepth 16384 in\n    sorry"
SOURCE = """namespace Bench.Fixture

/-- Unicode before the target keeps character and UTF-8 byte offsets distinct: π. -/
theorem keep (n : Nat) : n = n := by
  rfl

-- A comment saying sorry is not a proof hole.
theorem target (n : Nat) : n + 1 = 1 + n := by
  /- Unicode inside the proof: π. -/
  simpa using Nat.add_comm n 1

theorem after : True := by
  trivial

end Bench.Fixture
"""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _target_span(source: str = SOURCE):
    spans = [span for span in discover_proof_spans(source) if span.theorem == TARGET]
    assert len(spans) == 1
    return spans[0]


def _case(source: str = SOURCE, **overrides):
    raw = source.encode("utf-8")
    span = _target_span(source)
    case = {
        "id": "target-add-comm",
        "module": MODULE,
        "theorem": TARGET,
        "source_sha256": _sha(raw),
        "proof_start": span.proof_start,
        "proof_end": span.proof_end,
        "proof_sha256": span.proof_sha256,
        "category": "arithmetic",
        "difficulty": "easy",
        "axiom_whitelist": ["trusted.hash"],
    }
    case.update(overrides)
    return case


def _write_manifest(path: Path, cases: list[dict]) -> None:
    chunks = []
    ordered = [
        "id", "module", "theorem", "source_sha256", "proof_start", "proof_end",
        "proof_sha256", "category", "difficulty", "axiom_whitelist",
    ]
    for case in cases:
        lines = ["[[case]]"]
        for key in ordered:
            if key not in case:
                continue
            value = case[key]
            if isinstance(value, str):
                encoded = json.dumps(value)
            elif isinstance(value, list):
                encoded = "[" + ", ".join(json.dumps(item) for item in value) + "]"
            else:
                encoded = str(value)
            lines.append(f"{key} = {encoded}")
        for key in sorted(set(case) - set(ordered)):
            lines.append(f"{key} = {json.dumps(case[key])}")
        chunks.append("\n".join(lines))
    path.write_text("\n\n".join(chunks) + "\n", encoding="utf-8")


def _project(tmp_path: Path, source: str = SOURCE) -> Path:
    root = tmp_path / "project"
    source_path = root / "Bench" / "Fixture.lean"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(source, encoding="utf-8")
    return root


def _load(tmp_path: Path, case: dict | None = None, source: str = SOURCE):
    root = _project(tmp_path, source)
    manifest = tmp_path / "suite.toml"
    _write_manifest(manifest, [case or _case(source)])
    return load_suite(root, manifest)


def test_comment_mask_preserves_offsets_and_handles_nesting_and_strings():
    source = 'def s := "-- not a comment /- either -/"\n/- outer /- inner -/ sorry -/\n-- tail\n'
    masked = mask_comments(source)
    assert len(masked) == len(source)
    assert [i for i, ch in enumerate(masked) if ch == "\n"] == [
        i for i, ch in enumerate(source) if ch == "\n"
    ]
    assert '"-- not a comment /- either -/"' in masked
    assert "sorry" not in masked


def test_discovery_returns_deterministic_utf8_byte_spans():
    span = _target_span()
    body = SOURCE.encode("utf-8")[span.proof_start:span.proof_end]
    assert body.decode("utf-8").startswith("by\n")
    assert body.decode("utf-8").endswith("simpa using Nat.add_comm n 1")
    assert span.proof_sha256 == _sha(body)
    char_start = SOURCE.index(body.decode("utf-8"))
    assert span.proof_start > char_start  # the preceding π occupies two UTF-8 bytes
    assert discover_proof_spans(SOURCE) == discover_proof_spans(SOURCE)


def test_load_builds_leak_free_trusted_scaffold_and_goal(tmp_path):
    (prepared,) = _load(tmp_path)
    spec = prepared.case
    gold = SOURCE.encode("utf-8")
    scaffold = prepared.scaffold.encode("utf-8")
    placeholder = PLACEHOLDER.encode()

    # Exact trusted splice: nothing outside the selected proof is rewritten.
    assert scaffold[:spec.proof_start] == gold[:spec.proof_start]
    assert scaffold[spec.proof_start + len(placeholder):] == gold[spec.proof_end:]
    assert scaffold[spec.proof_start:spec.proof_start + len(placeholder)] == placeholder

    assert prepared.prompt_prefix.endswith(PLACEHOLDER)
    assert "simpa using Nat.add_comm" not in prepared.prompt_prefix
    assert "theorem after" not in prepared.prompt_prefix
    assert prepared.goal.file_text == prepared.scaffold
    assert prepared.goal.prompt_text == prepared.prompt_prefix
    assert prepared.goal.target_fqns == [TARGET]
    assert prepared.goal.axiom_whitelist == ["trusted.hash"]
    start, end = prepared.goal.proof_hole
    assert prepared.scaffold[start:end] == PLACEHOLDER
    assert theorem_signatures(prepared.gold_source) == theorem_signatures(prepared.scaffold)
    assert len(__import__("re").findall(r"\bsorry\b", strip_comments(prepared.scaffold))) == 1

    candidate = prepared.goal.materialize_proof("by\n  simpa using Nat.add_comm n 1")
    assert candidate[:start] == prepared.scaffold[:start]
    assert candidate.endswith(prepared.scaffold[end:])
    assert "sorry" not in strip_comments(candidate)


@pytest.mark.parametrize("field", ["source_sha256", "proof_sha256"])
def test_hash_mismatch_fails_closed(tmp_path, field):
    case = _case(**{field: "0" * 64})
    with pytest.raises(HeldoutError, match=field + " mismatch"):
        _load(tmp_path, case)


def test_stale_offsets_fail_even_when_the_slice_hash_is_updated(tmp_path):
    raw = SOURCE.encode("utf-8")
    span = _target_span()
    stale_start = span.proof_start + len(b"by\n")
    case = _case(
        proof_start=stale_start,
        proof_sha256=_sha(raw[stale_start:span.proof_end]),
    )
    with pytest.raises(HeldoutError, match="stale/wrong proof offsets"):
        _load(tmp_path, case)


def test_invalid_utf8_boundary_fails_closed(tmp_path):
    raw = SOURCE.encode("utf-8")
    span = _target_span()
    inside_pi = raw.index("π".encode("utf-8"), span.proof_start) + 1
    case = _case(
        proof_start=inside_pi,
        proof_sha256=_sha(raw[inside_pi:span.proof_end]),
    )
    with pytest.raises(HeldoutError, match="proof_start .* not a UTF-8 boundary"):
        _load(tmp_path, case)


def test_duplicate_ids_and_targets_fail_before_loading_sources(tmp_path):
    manifest = tmp_path / "suite.toml"
    duplicate_id = _case(theorem="Bench.Fixture.other")
    _write_manifest(manifest, [_case(), duplicate_id])
    root = tmp_path / "project"
    root.mkdir()
    with pytest.raises(HeldoutError, match="duplicate held-out case id"):
        load_suite(root, manifest)

    duplicate_target = _case(id="second")
    _write_manifest(manifest, [_case(), duplicate_target])
    with pytest.raises(HeldoutError, match="duplicate held-out target"):
        load_suite(root, manifest)


def test_overlapping_ranges_fail_before_loading_sources(tmp_path):
    first = _case()
    second = _case(
        id="overlap",
        theorem="Bench.Fixture.after",
        proof_start=first["proof_start"] + 1,
        proof_end=first["proof_end"] + 1,
    )
    manifest = tmp_path / "suite.toml"
    _write_manifest(manifest, [first, second])
    root = tmp_path / "project"
    root.mkdir()
    with pytest.raises(HeldoutError, match="overlapping proof ranges"):
        load_suite(root, manifest)


def test_target_must_have_a_pinnable_signature(tmp_path):
    case = _case(theorem="Bench.Fixture.missing")
    with pytest.raises(HeldoutError, match="expected exactly one"):
        _load(tmp_path, case)


def test_real_sorry_in_golden_source_is_rejected_but_comment_is_not(tmp_path):
    source = SOURCE.replace(
        "theorem after : True := by\n  trivial",
        "theorem after : True := by\n  sorry",
    )
    case = _case(source)
    with pytest.raises(HeldoutError, match="golden source contains real proof placeholder"):
        _load(tmp_path, case, source)


def test_short_term_proof_can_be_masked(tmp_path):
    source = "namespace Bench.Fixture\ntheorem target : True := True.intro\nend Bench.Fixture\n"
    case = _case(source, theorem=TARGET)
    (prepared,) = _load(tmp_path, case, source)
    assert prepared.prompt_prefix.endswith(PLACEHOLDER)
    assert prepared.scaffold == source.replace("True.intro", PLACEHOLDER)


def test_discovery_ignores_assignment_inside_hoare_result_type():
    source = """namespace Bench.Fixture
theorem target (n : Nat) :
    True → ⦃ r =>
      let witness := by exact n
      r = witness ⦄ := by
  intro
  simp
end Bench.Fixture
"""
    span = _target_span(source)
    proof = source.encode()[span.proof_start:span.proof_end].decode()
    assert proof == "by\n  intro\n  simp"


def test_unknown_or_missing_case_fields_fail_closed(tmp_path):
    manifest = tmp_path / "suite.toml"
    root = tmp_path / "project"
    root.mkdir()
    _write_manifest(manifest, [{**_case(), "typo_hash": "x"}])
    with pytest.raises(HeldoutError, match="unknown field"):
        load_suite(root, manifest)

    missing = _case()
    del missing["difficulty"]
    _write_manifest(manifest, [missing])
    with pytest.raises(HeldoutError, match="missing required field"):
        load_suite(root, manifest)
