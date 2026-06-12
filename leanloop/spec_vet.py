"""`leanloop vet` — spec-assurance probes for the UNVERIFIED layer.

The kernel guarantees "the proof matches the spec"; it cannot tell you the spec
says what you meant. These probes give a non-Lean-expert MECHANICAL signals
about a spec's health (each one kernel- or test-backed, not LLM opinion):

  CEX   counterexample search (`plausible`) on the statement itself
        -> "Found a counter-example!"  = the spec is FALSE (concrete witness)   RED
  HYP   satisfiability of the hypotheses (plausible hunting a witness that
        makes them all true, by refuting `hyps -> False`)
        -> witness found = hypotheses are satisfiable (not vacuous)             GREEN
        -> none found    = possibly VACUOUS (true only because nothing
           satisfies the hypotheses)                                            YELLOW
  NEG   try to PROVE the negation of the conclusion with cheap tactics
        -> if it proves, the spec is definitively false (kernel-checked)        RED

Pure construction/parsing lives here (testable); orchestration in cli.cmd_vet.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_OPEN = "([{⟨"
_CLOSE = ")]}⟩"


def split_signature(sig: str) -> tuple[str, str, str] | None:
    """Split a normalized signature `theorem name <binders> : <conclusion>`
    into (name, binders, conclusion) at the first TOP-LEVEL colon (colons
    inside `(h : T)` binder groups are at bracket depth >= 1).
    Returns None if the shape isn't recognizable."""
    m = re.match(r"\s*(?:theorem|lemma)\s+([A-Za-z0-9_'.]+)\s*(.*)$", sig, re.S)
    if not m:
        return None
    name, rest = m.group(1), m.group(2)
    depth = 0
    for i, ch in enumerate(rest):
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth -= 1
        elif ch == ":" and depth == 0:
            # ignore `:=` (shouldn't appear in a signature) and `::`
            if rest[i:i + 2] in (":=", "::"):
                continue
            return name, rest[:i].strip(), rest[i + 1:].strip()
    return None


# --------------------------------------------------------------------------- #
# probe file construction
# --------------------------------------------------------------------------- #
@dataclass
class Probe:
    kind: str          # "cex" | "hyp" | "neg"
    theorem_fqn: str
    file_text: str


_IMPORT_RE = re.compile(r"^import\s+\S+", re.M)


def _with_plausible_import(goal_text: str) -> str:
    """Insert `import Plausible` after the goal file's import block."""
    matches = list(_IMPORT_RE.finditer(goal_text))
    if not matches:
        return "import Plausible\n" + goal_text
    last = matches[-1]
    return goal_text[: last.end()] + "\nimport Plausible" + goal_text[last.end():]


def _ns_wrap(fqn: str, decl: str) -> str:
    """Re-open the theorem's namespace so binder/conclusion text resolves the
    same way it did at its original declaration site."""
    ns = fqn.rsplit(".", 1)[0] if "." in fqn else ""
    if not ns:
        return decl
    return f"namespace {ns}\n{decl}\nend {ns}"


def build_probes(goal_text: str, signatures: dict[str, str],
                 neg_tactics: list[str] | None = None) -> list[Probe]:
    """One probe file per (theorem, kind). Each file = the original goal file
    (so all its imports/opens/defs are in scope) + `import Plausible` + the
    probe declaration appended in a re-opened namespace."""
    negs = neg_tactics or ["simp_all", "omega", "(intros; omega)", "(intros; simp_all)"]
    base = _with_plausible_import(goal_text.rstrip()) + "\n\n"
    out: list[Probe] = []
    for fqn, sig in signatures.items():
        parts = split_signature(sig)
        if parts is None:
            continue
        name, binders, concl = parts
        short = re.sub(r"[^A-Za-z0-9_]", "_", name)
        # CEX: plausible hunts a counterexample to the statement itself
        cex = f"theorem _vet_cex_{short} {binders} : {concl} := by plausible"
        out.append(Probe("cex", fqn, base + _ns_wrap(fqn, cex) + "\n"))
        # HYP: plausible refutes `hyps -> False` == finds a witness of the hyps
        hyp = f"theorem _vet_hyp_{short} {binders} : False := by plausible"
        out.append(Probe("hyp", fqn, base + _ns_wrap(fqn, hyp) + "\n"))
        # NEG: cheap tactics try to PROVE the conclusion's negation
        alts = " | ".join(negs)
        neg = (f"theorem _vet_neg_{short} {binders} : ¬ ({concl}) := by\n"
               f"  first | {alts} | sorry")
        out.append(Probe("neg", fqn, base + _ns_wrap(fqn, neg) + "\n"))
    return out


# --------------------------------------------------------------------------- #
# verdicts
# --------------------------------------------------------------------------- #
@dataclass
class Verdict:
    theorem_fqn: str
    kind: str
    level: str          # "red" | "yellow" | "green" | "skip"
    message: str
    detail: str = ""


_CEX_RE = re.compile(r"Found a counter-example!")
_NO_CEX_RE = re.compile(r"Unable to find a counter-example")
# plausible could not even GENERATE inputs satisfying the hypotheses — for a
# spec, that is the vacuity smell itself.
_GAVE_UP_RE = re.compile(r"Gave up after failing to generate values")
_SORRY_WARN_RE = re.compile(r"declaration uses `?sorry`?")
_PLAUSIBLE_MISSING_RE = re.compile(
    r"unknown (?:module|package) (?:prefix )?.?Plausible|unknown tactic|unknown identifier .?plausible")


def judge(probe: Probe, lean_output: str) -> Verdict:
    """Map a probe run's compiler output to a traffic-light verdict."""
    out = lean_output
    if _PLAUSIBLE_MISSING_RE.search(out) and probe.kind in ("cex", "hyp"):
        return Verdict(probe.theorem_fqn, probe.kind, "skip",
                       "plausible not available in this project — probe skipped "
                       "(add the Plausible package to enable counterexample search)")
    if probe.kind == "cex":
        if _CEX_RE.search(out):
            wit = _extract_witness(out)
            return Verdict(probe.theorem_fqn, "cex", "red",
                           "SPEC IS FALSE — plausible found a concrete counterexample",
                           wit)
        if _GAVE_UP_RE.search(out):
            return Verdict(probe.theorem_fqn, "cex", "yellow",
                           "plausible could not generate inputs satisfying the "
                           "hypotheses — likely VACUOUS (see hyp probe)")
        if _NO_CEX_RE.search(out):
            return Verdict(probe.theorem_fqn, "cex", "green",
                           "no counterexample found (random testing)")
        return Verdict(probe.theorem_fqn, "cex", "skip",
                       "probe did not elaborate — no signal", out[-400:])
    if probe.kind == "hyp":
        if _CEX_RE.search(out):
            wit = _extract_witness(out)
            return Verdict(probe.theorem_fqn, "hyp", "green",
                           "hypotheses are satisfiable (witness found — spec is not vacuous)",
                           wit)
        if _GAVE_UP_RE.search(out) or _NO_CEX_RE.search(out):
            return Verdict(probe.theorem_fqn, "hyp", "yellow",
                           "no witness for the hypotheses found — the spec MAY be "
                           "vacuously true (nothing satisfies its assumptions); review them")
        return Verdict(probe.theorem_fqn, "hyp", "skip",
                       "probe did not elaborate — no signal", out[-400:])
    # neg: the file always builds (final `| sorry` alternative); if the sorry
    # warning is ABSENT, a real tactic proved the negation.
    if "error" in out.lower() and "_vet_neg_" in out:
        return Verdict(probe.theorem_fqn, "neg", "skip",
                       "probe did not elaborate — no signal", out[-400:])
    if _SORRY_WARN_RE.search(out):
        return Verdict(probe.theorem_fqn, "neg", "green",
                       "negation not provable by cheap tactics")
    return Verdict(probe.theorem_fqn, "neg", "red",
                   "NEGATION PROVED — the spec's conclusion is false under its "
                   "hypotheses (kernel-checked)")


def _extract_witness(out: str) -> str:
    m = _CEX_RE.search(out)
    if not m:
        return ""
    tail = out[m.end():]
    stop = tail.find("-------")
    return tail[:stop if stop > 0 else 400].strip()


# --------------------------------------------------------------------------- #
# the explain prompt (the LLM half: translate the spec for a human reviewer)
# --------------------------------------------------------------------------- #
EXPLAIN_PROMPT = """You are reviewing a formal specification for someone who does NOT read Lean.
Your job is adversarial: find what the spec FAILS to capture, not to praise it.
The Lean kernel already guarantees the code matches this spec — the only open
question is whether the SPEC says the right thing. Do not restate the Lean;
translate it. For EACH theorem below, produce sections 1-5.

1. **Plain-English claim** — one or two sentences: what does this theorem
   actually guarantee, for which inputs? Be precise about quantifiers
   ("for EVERY input…", "ASSUMING x < n…").

2. **What it does NOT say** — the gaps a reader might wrongly assume are
   covered: it bounds a value but doesn't pin it; covers one function but not
   its caller; hypotheses that silently narrow the claim; the happy path but
   not error paths.

3. **Strength audit** (this is the load-bearing check — research shows a
   test-passing spec kills real bugs only ~60% of the time). Classify the
   conclusion:
   - WEAK-CLASS if it's only a type/format/null/bound check, an existence
     ("∃ … ok"), or a one-sided inequality. These let many wrong
     implementations pass — FLAG them and say what stronger claim is missing.
   - STRONG-CLASS if it's a full functional equality (output = exact spec
     function) or a tight two-sided bound. Confirm it pins the behavior.

4. **Reconstruct-and-compare** (Clover triangle): from the theorem statement
   ALONE, write the one-sentence behavior you'd expect the function to have.
   Then read the actual function/definition in the file. Do they match? A
   mismatch — or a statement you can't reconstruct a clear behavior from — is
   a red flag (the spec may describe the wrong property).

5. **Misalignment / spec-gaming check** (AlphaVerus): give ONE concrete wrong
   implementation that would still satisfy this exact statement. If you can
   write a plausible one easily, the spec is too weak — say how to strengthen
   it. If the only impls that pass are correct ones, say so.

End each theorem with a one-line **VERDICT**: SOUND-AND-USEFUL / TOO-WEAK /
SUSPICIOUS-MAY-BE-WRONG-PROPERTY, and a final note on what a human still has
to judge by hand (security properties, unexercised paths, whether the spec's
notion of "correct" matches the real-world requirement — these are NOT
mechanically checkable).

Mechanical probe results (kernel/test-backed — trust these over intuition):
{probes}

The specification file:
```lean4
{goal}```"""
