"""The audit gate. The Lean kernel already guarantees a built proof is valid;
these checks additionally forbid the known ways a machine proof can type-check
while proving nothing (the transcript's "silent failure" surface).

Per the founding doc:
  - ban `sorry`/`admit`
  - ban new `axiom` declarations (only whitelisted hardware axioms allowed)
  - flag/forbid `native_decide` (larger TCB than the kernel)
  - `#print axioms` must reduce to the allowed kernel axioms + whitelist, with
    NO `sorryAx`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import AuditConfig
from .leantext import strip_comments

_SORRY_RE = re.compile(r"\b(sorry|admit)\b")
_AXIOM_DECL_RE = re.compile(r"^\s*axiom\s+([A-Za-z0-9_'.]+)", re.M)
_NATIVE_DECIDE_RE = re.compile(r"\bnative_decide\b")
# `#print axioms X` prints e.g. "'X' depends on axioms: [propext, sorryAx, ...]"
_AXIOMS_LINE_RE = re.compile(r"depends on axioms:\s*\[([^\]]*)\]")


@dataclass
class AuditResult:
    ok: bool
    reasons: list[str]


def audit_source(file_text: str, cfg: AuditConfig) -> AuditResult:
    """Static checks on the candidate file (cheap; run before building).

    NOTE: this is defense-in-depth and a source of clear error messages. The
    AUTHORITATIVE soundness gate is the axiom-closure check (`audit_axioms`):
    `sorry`/`admit` always surface as `sorryAx`, and `native_decide` as
    `Lean.ofReduceBool`, in `#print axioms` — regardless of how cleverly the
    source obfuscates them — so an obfuscation that slips past these regexes is
    still caught by the closure check.
    """
    reasons: list[str] = []
    # strip comments (nested-aware) so we don't false-positive on "sorry" in a doc
    code = strip_comments(file_text)
    if cfg.forbid_sorry and _SORRY_RE.search(code):
        reasons.append("contains `sorry`/`admit`")
    if cfg.forbid_new_axioms:
        # A candidate PROOF must never DECLARE axioms — not even whitelisted
        # names (else a model could declare `axiom keccak256_pure : False` and
        # ride the whitelist). The whitelist applies ONLY to the axiom CLOSURE
        # of pre-existing project axioms (see audit_axioms). Any `axiom` in the
        # candidate source is rejected.
        for name in _AXIOM_DECL_RE.findall(code):
            reasons.append(f"candidate declares axiom `{name}` "
                           f"(proofs may not declare axioms; whitelist is for the closure)")
    if cfg.forbid_native_decide and _NATIVE_DECIDE_RE.search(code):
        reasons.append("uses `native_decide` (larger TCB than the kernel)")
    return AuditResult(ok=not reasons, reasons=reasons)


def audit_axioms(axiom_output: str, cfg: AuditConfig, *, expected: int = 0) -> AuditResult:
    """Check `#print axioms` output: every closure must lie within the allowed
    kernel axioms + the reviewed whitelist, and NEVER include `sorryAx`.

    FAIL-CLOSED: if we expected `expected` theorems but parsed fewer closure
    lines (e.g. the checker hit `unknown constant` because FQN resolution
    failed), the audit FAILS — an unverifiable closure is never accepted.
    """
    reasons: list[str] = []
    allowed = set(cfg.allowed_kernel_axioms) | set(cfg.axiom_whitelist)
    # `#print axioms X` prints "'X' does not depend on any axioms" for pure
    # theorems — count those as verified-clean closures too.
    clean = len(re.findall(r"does not depend on any axioms", axiom_output))
    lines = _AXIOMS_LINE_RE.findall(axiom_output)
    for group in lines:
        axioms = [a.strip().strip("'\"") for a in group.split(",") if a.strip()]
        for ax in axioms:
            if ax == "sorryAx":
                reasons.append("depends on `sorryAx` (an unfilled `sorry`)")
            elif ax not in allowed:
                reasons.append(f"depends on unreviewed axiom `{ax}`")
    found = len(lines) + clean
    if expected and found < expected:
        reasons.append(
            f"axiom closure unverifiable: expected {expected} `#print axioms` "
            f"results, parsed {found} (checker output: {axiom_output[:500]!r})")
    if "sorryAx" in axiom_output and not lines:
        reasons.append("depends on `sorryAx`")
    return AuditResult(ok=not reasons, reasons=reasons)


