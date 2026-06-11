# LeanLoop architecture

LeanLoop verifies an **Aeneas-generated Lean 4 project**: a Rust crate is
translated `Rust → Charon → Aeneas → Lean 4` into a pure functional model, and
LeanLoop closes the resulting proof obligations (theorems ending in `sorry`)
with a tiered prover stack. The Lean kernel is the only ground truth.

## Why this shape

Two ideas from the surrounding research (Verus LLM tooling: AutoVerus,
VeruSAGE, SAFE/AlphaVerus; Lean provers: Goedel-Prover-V2, Prover Agent; the
Aeneas/SymCrypt line of work) drive the design:

1. **Cheapest tier first.** Most Aeneas obligations are arithmetic/bounds side
   conditions that non-LLM tactics close for free. Only the residue reaches a
   model, and only the hardest residue reaches a frontier model.
2. **The kernel is unfakeable; everything else is untrusted.** A model is a
   heuristic proposal generator. Its output is accepted only if the Lean kernel
   type-checks it *and* it passes an audit gate. A wrong or adversarial proposal
   simply fails — it cannot corrupt results.

## The tiers

| Tier | What | Cost |
|------|------|------|
| 0 | Tactic battery (`scalar_tac`, `omega`, `simp`/`grind`, `step*`/`progress`, `bv_decide`, `exact?`) | free, instant |
| 1 | Local prover (Goedel-Prover-V2-8B via Ollama): pass@N sampling, temperature diversity, verifier-guided self-correction | your hardware |
| 2 | Frontier (`claude` CLI): whole-proof + repair on hard goals, kept sparse | metered, sparse |

Each tier emits **candidate Lean files**; none decide acceptance.

## The gates (acceptance)

A candidate is ACCEPTED iff all hold:

1. **Statement pin** — the candidate declares every theorem the goal requires,
   each with an identical *normalized signature* (name + binders + type up to
   `:=`). This stops a model from "proving" a weaker/renamed statement
   (`theorem foo : True := trivial`). Signatures are comment-stripped and
   whitespace-normalized; namespaces are tracked for fully-qualified names.
2. **Source audit** — no `sorry`/`admit`, no candidate-declared `axiom`
   (not even a whitelisted *name* — the whitelist is for the closure, never for
   source declarations), no `native_decide`. Defense-in-depth + clear errors.
3. **Kernel gate** — `lake build` of the module succeeds (the Lean kernel
   type-checks the proof).
4. **Axiom closure** — `#print axioms` for each goal theorem reduces to the
   allowed kernel axioms (`propext`, `Classical.choice`, `Quot.sound`) plus the
   project's reviewed axiom whitelist, with **no `sorryAx`** and no
   `native_decide`-introduced axiom. **Fail-closed:** if the closure can't be
   resolved (e.g. the checker hit an unknown constant), acceptance is denied.

Build + axiom-check happen in one write/restore window so the `.olean` the
axiom checker reads is exactly the candidate that built. The original project
file is restored from an on-disk backup afterwards.

## Soundness boundary, restated

`sorry`/`admit` always surface as `sorryAx`, and `native_decide` as
`Lean.ofReduceBool`, in the axiom closure — regardless of source obfuscation —
so gate (4) is authoritative; gates (1)–(3) add early rejection and better
diagnostics. The statement pin (1) is what makes the kernel's "this builds"
mean "this proves the goal we asked", not merely "this is some valid Lean".

## What a green run means (and doesn't)

A proof is a machine-checked theorem about the **Aeneas model** of the Rust,
under the project's stated axioms. Residual trust, all enumerable: (a) specs
capture intent (mitigate with negation/mutation testing — roadmap); (b) the
unverified `rustc → Charon → Aeneas` translation is faithful (mitigate with
differential testing); (c) reviewed hardware axioms hold; (d) `rustc` + the
Lean kernel. Constant-time / information-flow are out of scope.

## Run log

Every attempt — accepted or not — is logged to sqlite: goal, tier, model,
sampling params, Lean errors, axiom closure, wall-clock. It is the triage
trail and the corpus for later QLoRA expert iteration.
