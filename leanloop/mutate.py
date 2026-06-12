"""`leanloop mutate` — spec-STRENGTH measurement by proof-break scoring.

The idea (mCoq, ICSE 2020 — verified 2026-06): mCoq mutates the DEFINITIONS
(the implementation model), NOT the specs or proofs, then re-checks the proofs.
Our directly-runnable analogue mutates the extracted Lean DEFINITION files
(`Funs.lean` etc.) and rebuilds the pinned-proof target:
  - a pinned proof now FAILS to build -> the spec CAUGHT the bug  (mutant "killed")
  - the target still builds clean      -> a real bug slips past every spec
                                          (mutant "lived" 🔴 — a spec gap)
  - the mutant itself won't elaborate  -> unviable (excluded from the score)

A surviving mutant is the high-value signal a non-Lean reader can act on:
"this concrete change to the model was caught by NO proof" — shown as a diff.
mCoq data: ~85% of survivors are real spec gaps (the rest equivalent mutants).

This needs NO Aeneas re-extraction — it mutates the already-extracted Lean and
relies on the existing kernel gate. (A deeper Rust-side tier — mutate the Rust
with cargo-mutants, then re-extract — is sketched at the bottom for when the
extraction pipeline is wired; cargo-mutants is generator-only because its kill
criterion is `cargo test`, not proof re-checking.)

Pure logic (operators, scoring) here; orchestration in cli.cmd_mutate.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .leantext import strip_comments


# --------------------------------------------------------------------------- #
# mutation operators over Lean DEFINITION source
# --------------------------------------------------------------------------- #
@dataclass
class Mutant:
    op: str              # operator name
    file: str            # path relative to project root
    line_no: int         # 1-based
    col: int             # 0-based offset within the line
    original: str        # exact matched text
    mutated: str         # replacement text
    rationale: str = ""

    @property
    def name(self) -> str:
        return f"{self.file}:{self.line_no} [{self.op}] {self.original!r}→{self.mutated!r}"


# (regex, op-name, replacement-fn(matchtext)->newtext|None, rationale).
# Replacements that matter for crypto/parser correctness lead the list.
def _byteflip(t: str) -> str:
    return f"0x{int(t[2:], 16) ^ 0x01:02X}"

_OPERATORS = [
    (re.compile(r"0x[0-9A-Fa-f]{2}\b"), "crypto-byte-flip", _byteflip,
     "a domain-separator/ADRS/padding byte — a spec that doesn't pin it lets "
     "two messages collide in the hash tree"),
    (re.compile(r"(?<![\w.])\d{1,4}(?![\w.])"), "const-off-by-one",
     lambda t: str(int(t) + 1),
     "a numeric constant (length/index/bound) — off-by-one is the classic "
     "parser/loop bug a weak spec misses"),
    (re.compile(r"&&&"), "bitand-to-bitor", lambda t: "|||",
     "bitwise AND→OR — flips masking/combining logic"),
    (re.compile(r"<<<"), "shl-to-shr", lambda t: ">>>",
     "left shift→right shift — corrupts byte-packing"),
    (re.compile(r"(?<![<>=!])<=(?!=)"), "le-to-lt", lambda t: "<",
     "≤→< boundary flip (off-by-one on a bound)"),
    (re.compile(r"(?<![<>=!])>=(?!=)"), "ge-to-gt", lambda t: ">",
     ">=→> boundary flip"),
]
# tokens that mark a PROOF (we mutate definitions, never proofs/specs — a
# mutated proof would be circular). A file/region with these is skipped.
_PROOF_MARKERS = re.compile(r"\b(theorem|lemma|:= by|@\[simp\]|@\[progress\]|@\[step\])\b")
# non-semantic directive lines: mutating these yields equivalent mutants (e.g.
# `set_option maxRecDepth 2048` -> 2049 changes nothing the function computes).
_DIRECTIVE_LINE = re.compile(r"^\s*(set_option|import|open|attribute|namespace|end|#\w+|/-)")


def generate_mutants(file_rel: str, file_text: str, *, max_per_file: int = 40) -> list[Mutant]:
    """Enumerate definition mutants for one Lean file. Lines that look like
    proofs/specs are skipped (we only perturb the implementation model)."""
    out: list[Mutant] = []
    code = strip_comments(file_text)
    for i, line in enumerate(code.splitlines(), start=1):
        if _PROOF_MARKERS.search(line) or _DIRECTIVE_LINE.match(line):
            continue
        if not re.search(r"\b(def|abbrev)\b", line) and ":=" not in line and "=>" not in line:
            # keep to definition-bearing lines; cheap filter to cut noise
            if not any(rx.search(line) for rx, *_ in _OPERATORS):
                continue
        for rx, op, repl, why in _OPERATORS:
            for m in rx.finditer(line):
                new = repl(m.group(0))
                if new is None or new == m.group(0):
                    continue
                out.append(Mutant(op, file_rel, i, m.start(), m.group(0), new, why))
                if len(out) >= max_per_file:
                    return out
    return out


def apply_mutant(file_text: str, m: Mutant) -> str:
    """Apply one mutant to the file text (replace the exact occurrence at its
    line/col). Returns the mutated file text."""
    lines = file_text.splitlines(keepends=True)
    if not (1 <= m.line_no <= len(lines)):
        return file_text
    line = lines[m.line_no - 1]
    # the col is from the comment-stripped view; re-find the occurrence on the
    # real line to stay robust (comments shift columns).
    idx = line.find(m.original)
    if idx < 0:
        return file_text
    lines[m.line_no - 1] = line[:idx] + m.mutated + line[idx + len(m.original):]
    return "".join(lines)


def diff(m: Mutant) -> str:
    return f"  {m.file}:{m.line_no}\n  - …{m.original}…\n  + …{m.mutated}…\n  ({m.rationale})"


# --------------------------------------------------------------------------- #
# scoring (mCoq semantics)
# --------------------------------------------------------------------------- #
@dataclass
class MutantResult:
    mutant: Mutant
    outcome: str         # "killed" | "lived" | "unviable" | "timeout" | "error"
    detail: str = ""


def score(results: list[MutantResult]) -> dict:
    """mCoq mutation score = killed / (killed + lived). Unviable/timeout are
    EXCLUDED from the denominator (a mutant that won't even elaborate tells you
    nothing about spec strength)."""
    by = {k: [r for r in results if r.outcome == k]
          for k in ("killed", "lived", "unviable", "timeout", "error")}
    scored = len(by["killed"]) + len(by["lived"])
    return {
        "mutation_score": round(100 * len(by["killed"]) / scored, 1) if scored else None,
        "killed": len(by["killed"]),
        "lived": len(by["lived"]),
        "unviable": len(by["unviable"]),
        "timeout": len(by["timeout"]),
        "error": len(by["error"]),
        "total": len(results),
        "survivors": [diff(r.mutant) for r in by["lived"]],
    }


# --------------------------------------------------------------------------- #
# optional Rust-side tier (cargo-mutants as generator; needs extraction wired)
# --------------------------------------------------------------------------- #
def parse_cargo_mutants_list(json_text: str) -> list[dict]:
    """Parse `cargo mutants --list --json` into [{name, diff}]. Tolerant of
    schema drift. Used only by the (future) Rust-side mutate tier."""
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        return []
    items = data if isinstance(data, list) else data.get("mutants", [])
    out = []
    for m in items:
        if isinstance(m, dict):
            out.append({"name": m.get("name", str(m)), "diff": m.get("diff", "")})
    return out
