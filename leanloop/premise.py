"""`leanloop necessity` — premise load-bearingness (the dead-hypothesis check).

The three spec-assurance questions, and which tool answers each:

  * `mutate`  — "is the SPEC strong enough to catch an IMPLEMENTATION bug?"
                (mutate the definitions, expect a pinned proof to break)
  * `vet` HYP — "are the hypotheses SATISFIABLE (not vacuously true)?"
                (`plausible` hunts a witness that makes them all hold)
  * `necessity` (this module) — the orthogonal question the other two miss:
                "does the PROOF actually USE each hypothesis the theorem
                 DECLARES?"  A satisfiable, bug-catching theorem can still carry
                 a DEAD premise — a hypothesis nothing in the proof consumes,
                 so the statement is weaker than its signature implies (it would
                 hold without that assumption) or the assumption is decoration.

For a PROVEN theorem `name (h1 : P) (h2 : Q) : R := <proof>`, drop each LEAF
hypothesis binder — a `(h : …)` group whose bound name is NOT referenced in the
conclusion or in a later binder's type (i.e. an *assumption*, not a *parameter*
the theorem is about) — and rebuild the module with the SAME proof:

  - rebuild FAILS  -> the proof needed it       -> KILLED (load-bearing)   ok
  - rebuild PASSES -> the proof ignored it       -> LIVED  (DEAD premise)  flag
  - won't elaborate for another reason           -> UNVIABLE (excluded)

This is the NON-CIRCULAR slice of proof mutation that `mutate.py` deliberately
excludes ("a mutated proof would be circular"): dropping a STATEMENT hypothesis
and re-running the UNCHANGED proof tests consumption — it never rewrites the
proof. Heuristic, like all mutation testing: dependent-type binders are filtered
out by the reference check; tactic automation that scoops hypotheses
(`simp_all`, `omega`) is handled correctly — a dropped *unused* hypothesis still
lets the automation close, so it shows up as LIVED.

Pure logic (binder parsing, variant construction, scoring) lives here (testable);
orchestration in cli.cmd_necessity reuses LeanRunner.verify as the kernel gate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .leantext import strip_comments

_OPEN = "([{⦃⟨"
_CLOSE = ")]}⦄⟩"
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_'.]*")
# a theorem/lemma head (namespace tracking mirrors lean_runner._iter_decls)
_HEAD_RE = re.compile(r"(?:^|\n)[ \t]*(?:@\[[^\]]*\][ \t]*)*(?:theorem|lemma)[ \t]+([A-Za-z0-9_'.]+)")
_NS_RE = re.compile(r"^\s*namespace\s+([A-Za-z0-9_'.]+)", re.M)
_END_RE = re.compile(r"^\s*end(?:\s+([A-Za-z0-9_'.]+))?\s*$", re.M)


# --------------------------------------------------------------------------- #
# binder parsing
# --------------------------------------------------------------------------- #
@dataclass
class Binder:
    start: int        # offset into the binder-region string
    end: int          # exclusive
    kind: str         # '(' explicit | '{' implicit | '[' instance | '⦃' | '⟨'
    names: list[str]
    type_src: str
    text: str


def _idents(s: str) -> set[str]:
    return set(_IDENT_RE.findall(s))


def split_binder_groups(binders: str) -> list[Binder]:
    """Split a binder region `(a b : T) {x : U} [inst : C]` into top-level
    bracket groups, capturing each group's names + type. Offsets are into
    `binders`."""
    out: list[Binder] = []
    i, n = 0, len(binders)
    while i < n:
        ch = binders[i]
        if ch in _OPEN:
            opener = ch
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                if binders[j] in _OPEN:
                    depth += 1
                elif binders[j] in _CLOSE:
                    depth -= 1
                j += 1
            inner = binders[i + 1: j - 1]
            names, type_src = _split_names_type(inner)
            out.append(Binder(i, j, opener, names, type_src, binders[i:j]))
            i = j
        else:
            i += 1
    return out


def _split_names_type(inner: str) -> tuple[list[str], str]:
    """`a b : T` -> (["a","b"], "T"). Splits at the FIRST top-level colon.
    A group with no top-level colon (anonymous/autobound) yields ([], inner)."""
    depth = 0
    for k, ch in enumerate(inner):
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth -= 1
        elif ch == ":" and depth == 0:
            if inner[k:k + 2] in (":=", "::"):
                continue
            names = inner[:k].split()
            return names, inner[k + 1:].strip()
    return [], inner.strip()


def droppable_hyps(binders: str, conclusion: str) -> list[Binder]:
    """The LEAF hypothesis binders worth dropping: EXPLICIT `(…)` groups whose
    bound names appear NOWHERE in the conclusion nor in any LATER binder's type.
    Such a binder is a pure assumption — removing it keeps the statement
    well-typed, so a still-building proof proves it was never used."""
    groups = split_binder_groups(binders)
    concl_ids = _idents(conclusion)
    out: list[Binder] = []
    for idx, b in enumerate(groups):
        if b.kind != "(" or not b.names:
            continue
        later_ids: set[str] = set()
        for later in groups[idx + 1:]:
            later_ids |= _idents(later.type_src)
        if any(nm in concl_ids or nm in later_ids for nm in b.names):
            continue  # a parameter the theorem is ABOUT, not an assumption
        out.append(b)
    return out


# --------------------------------------------------------------------------- #
# theorem location + variant construction (over comment-stripped code)
# --------------------------------------------------------------------------- #
@dataclass
class TheoremSpan:
    fqn: str
    head_end: int      # offset just after the theorem name (binders start here)
    colon: int         # offset of the top-level `:` (conclusion start)
    assign: int        # offset of the proof `:=`
    binders: str
    conclusion: str


def _top_level_scan(code: str, start: int) -> tuple[int, int]:
    """From `start` (just after a theorem name), find the top-level `:`
    (conclusion) and the proof `:=`. Returns (colon_idx, assign_idx); either is
    -1 if not found."""
    depth = 0
    colon = -1
    i, n = start, len(code)
    while i < n:
        ch = code[i]
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth -= 1
        elif depth == 0 and ch == ":":
            if code[i:i + 2] == ":=":
                return colon, i
            if code[i:i + 2] != "::" and colon == -1:
                colon = i
        i += 1
    return colon, -1


def theorem_spans(file_text: str) -> list[TheoremSpan]:
    """Locate every theorem/lemma in the COMMENT-STRIPPED file, with its
    binder region and conclusion. (Stripping comments is semantically neutral —
    the variant we build & rebuild is the stripped file.)"""
    code = strip_comments(file_text)
    ns_stack: list[str] = []
    out: list[TheoremSpan] = []
    # walk line by line for namespace tracking, but resolve spans over `code`.
    for m in re.finditer(r"[^\n]*\n?", code):
        line = m.group(0)
        ls = line.strip()
        nm = _NS_RE.match(ls)
        if nm:
            ns_stack.append(nm.group(1))
            continue
        em = _END_RE.match(ls)
        if em:
            name = em.group(1)
            if name and ns_stack and ns_stack[-1] == name:
                ns_stack.pop()
            elif ns_stack:
                ns_stack.pop()
            continue
        hm = re.match(r"[ \t]*(?:@\[[^\]]*\][ \t]*)*(?:theorem|lemma)[ \t]+([A-Za-z0-9_'.]+)", line)
        if hm:
            name = hm.group(1)
            head_end = m.start() + hm.end()
            colon, assign = _top_level_scan(code, head_end)
            if colon == -1 or assign == -1 or colon >= assign:
                continue
            prefix = ".".join(ns_stack)
            fqn = f"{prefix}.{name}" if prefix else name
            out.append(TheoremSpan(
                fqn, head_end, colon, assign,
                code[head_end:colon], code[colon + 1:assign]))
    return out


@dataclass
class Variant:
    theorem_fqn: str
    dropped: str          # the binder text removed
    rationale: str
    code: str = field(repr=False, default="")  # full stripped-file text, binder removed


def build_variants(file_text: str) -> list[Variant]:
    """One variant per (theorem, droppable leaf hypothesis): the comment-stripped
    file with exactly that binder removed from the theorem's signature."""
    code = strip_comments(file_text)
    out: list[Variant] = []
    for sp in theorem_spans(code):
        for b in droppable_hyps(sp.binders, sp.conclusion):
            # b offsets are into sp.binders, which starts at sp.head_end in `code`.
            gstart = sp.head_end + b.start
            gend = sp.head_end + b.end
            variant_code = code[:gstart] + code[gend:]
            if variant_code == code:
                continue
            out.append(Variant(
                sp.fqn, b.text.strip(),
                f"the proof of {sp.fqn} may not use the hypothesis {b.text.strip()}",
                variant_code))
    return out


# --------------------------------------------------------------------------- #
# scoring (mirrors mutate.score semantics: LIVED is the signal to act on)
# --------------------------------------------------------------------------- #
@dataclass
class NecessityResult:
    variant: Variant
    outcome: str          # "killed" | "lived" | "unviable" | "timeout" | "error"
    detail: str = ""


def classify(build_ok: bool, build_output: str, dropped_names: list[str]) -> str:
    """Map a variant rebuild to an outcome. build_ok -> LIVED (dead premise).
    A failure that cites the dropped name (or a proof error) -> KILLED. A failure
    that is clearly an unrelated elaboration breakage -> UNVIABLE."""
    if build_ok:
        return "lived"
    out = build_output
    if any(re.search(rf"unknown (?:identifier|constant) '?{re.escape(nm)}'?", out)
           for nm in dropped_names):
        return "killed"
    # a proof/goal failure means the proof genuinely consumed the hypothesis
    if re.search(r"unsolved goals|linarith failed|omega could not|simp made no progress"
                 r"|type mismatch|failed to|no goals|tactic '", out):
        return "killed"
    return "unviable"


def score(results: list[NecessityResult]) -> dict:
    by = {k: [r for r in results if r.outcome == k]
          for k in ("killed", "lived", "unviable", "timeout", "error")}
    scored = len(by["killed"]) + len(by["lived"])
    return {
        # necessity score = killed / (killed + lived): the fraction of declared
        # hypotheses the proofs actually USE. 100% = no dead premises.
        "necessity_score": round(100 * len(by["killed"]) / scored, 1) if scored else None,
        "load_bearing": len(by["killed"]),
        "dead_premises": len(by["lived"]),
        "unviable": len(by["unviable"]),
        "timeout": len(by["timeout"]),
        "error": len(by["error"]),
        "total": len(results),
        "dead": [f"{r.variant.theorem_fqn}: DROPPED {r.variant.dropped} and the proof "
                 f"STILL builds — dead premise" for r in by["lived"]],
    }
