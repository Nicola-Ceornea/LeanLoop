---
name: leanloop
description: Operate LeanLoop — the adversarial LLM-driven Lean 4 verification loop for Aeneas-extracted Rust (run proving campaigns, act as the interactive frontier, run spec assurance, diagnose runs). Use when working with a LeanLoop-managed verification project, closing queued proof goals, or vetting specs.
---

# Operating LeanLoop

LeanLoop (github.com/Nicola-Ceornea/LeanLoop, Python pkg `leanloop`) grinds
`sorry`-goals in an Aeneas-generated/hand-written Lean 4 Lake project through a
tier cascade — Tier 0 tactic battery → Tier 1 local prover (Goedel via Ollama)
→ Tier 2 frontier — with the **Lean kernel as the only ground truth**. Models
only *propose*; acceptance is decided by gates.

Config: `leanloop.toml` in the cwd (or `-c <file>`, valid before OR after the
subcommand). Consumer-project config/manifests/queue artifacts live in the
*consumer's* repo, never in LeanLoop itself.

## The gates (why a candidate gets rejected)

Every candidate — from any tier, including you — must pass ALL:
1. **Statement pin** — must declare every goal theorem with an *identical
   normalized signature* (name + binders + type up to `:=`). Changing or
   weakening the statement = instant reject. Never edit a goal's theorem
   statement to make it provable.
2. **Source audit** — no `sorry`/`admit`; no `axiom` declarations (not even
   re-declaring a whitelisted name); no `native_decide`.
3. **Kernel gate** — `lake build` of the module must succeed.
4. **Axiom closure** — `#print axioms` of each goal theorem must reduce to
   `propext, Classical.choice, Quot.sound` + the project's `[audit]
   axiom_whitelist`. Fail-closed: an unresolvable closure is a rejection.

## Command map

| task | command |
|---|---|
| preflight (prover up? model on GPU? lake? disk?) | `leanloop doctor` |
| list goals (files with non-comment `sorry`) | `leanloop scan` |
| prove one module / all goals | `leanloop prove M` / `leanloop run --apply [--manifest g.toml]` |
| live run state over SSH | `leanloop status [--watch]` |
| prover tok/s + close-rate baseline | `leanloop bench [--samples N] [--goals M]` |
| spec probes (false/vacuous/negation) | `leanloop vet M [--explain]` |
| spec strength (mutate defs, proofs must break) | `leanloop mutate FILES --build-target M` |
| spec vs official test vectors (byte-eq) | `leanloop kat` (needs `[kat]` module/adapter/vectors) |
| frontier queue: list / next task / submit proof | `leanloop frontier [--next]` · `leanloop submit GOAL` |

## Acting as the interactive frontier (the flat-rate tier)

When goals are queued (`backend = "queue"`), you are the prover:
1. `leanloop frontier --next` → a task with the goal file, the **pinned
   signatures**, acceptance criteria, and the best local attempt's errors.
2. Write a COMPLETE Lean file to the task's `.candidate.lean` path.
3. `leanloop submit <goal>` — re-verifies through all gates; **applies to the
   project on accept**. On reject, read the gate error, revise, resubmit.

Proof techniques that work on Aeneas-extracted goals (learned on real targets):
- Unfold the function, then `step*` (chomps the `Result` monad; `@[step]`
  lemmas fire) with side goals closed by `scalar_tac` / `simp_all` / `omega`.
- Project params are often `@[global_simps, irreducible]` — `simp only
  [params.X]` BEFORE `step*` so `scalar_tac` sees literal values.
- Loops: `apply Aeneas.Std.loop.spec_decr_nat (measure := …) (inv := …)`;
  step the iterator with a `next`-spec lemma, then case on done/continue.
- Array writes: `List.set_getElem!_eq` / `set_getElem!_ne`; casts:
  `UScalar.cast_val_eq`; `lift x = ok x` needs `simp only [lift]`.
- Finding lemma names beats guessing: `exact?`, and grep the dependency
  sources (`.lake/packages/*/`, the toolchain's `src/lean/Init/`) — core
  often has the bit-arithmetic lemma Mathlib lacks (e.g.
  `Nat.testBit_two_pow_mul_add` closed a disjoint-OR=ADD goal).

## Spec assurance — the four spec-error classes

The kernel proves code↔spec; these check the SPEC. Use before trusting a
green proof of a new spec, and present results to non-Lean readers:
- **false / vacuous** → `vet` (plausible counterexample, hypothesis-witness,
  negation probes; traffic-light output). `--explain` emits an adversarial
  plain-English review prompt with the mechanical verdicts embedded.
- **weak** → `mutate` (mCoq scoring: mutate Lean *definitions*, never
  proofs; a mutant no proof kills = a spec gap, ~85% of survivors are real).
- **wrong-property** → `kat` (executable spec adapter `List UInt8 → List
  UInt8` run over official vectors, byte-equality; rebuilds the module first
  — never trust a stale `.olean`). KATs ground exercised functional paths
  only — not security properties or unexercised paths; say so.

## Operational gotchas (each cost real debugging time)

- Runs are **idempotent**: solved goals cache on (name, content-hash); re-run
  after any crash. A goal file that *changes* re-proves (hash miss).
- Per-goal budget `[prover] goal_timeout_s` (default 3600 s) skips to the
  frontier on expiry — raise it for slow quality-tier models.
- `status` ALIVE just means attempts are logging; check "working on it Xm"
  for a single long goal.
- Ollama: `OLLAMA_NUM_PARALLEL` must equal `[prover.local] concurrency` or
  samples queue. Context is SHARED across slots. 16 GB dGPU + 8B Q8: ctx
  24576/conc 2. 32B Q4_K_M: ~26 GB — fully resident only on ≥24 GB or an
  APU/iGPU via GTT (Vulkan, no ROCm needed); on a 16 GB dGPU it runs via
  partial offload (CPU/GPU split in `ollama ps` is EXPECTED) — conc 1,
  modest num_ctx (KV evicts layers), `GOEDEL_SIZE=32b` setup, cap
  `max_tokens` (~4096) so slow samples can't eat the HTTP timeout.
- `mutate` skips directive lines (`set_option` etc.) — they're equivalent
  mutants; if you add operators, keep proofs/specs strictly off-limits.
- `bench` uses an ephemeral DB on purpose (probe accepts must never poison
  the production solved-cache).
- The headless `claude -p` frontier backend is METERED (post 2026-06-15);
  `backend = "queue"` + an interactive session is the flat-rate path.

## Adding a new verification target (consumer-side recipe)

1. State the goal: a `.lean` file in the consumer's Lake project with the
   theorem ending `:= by sorry` (named theorem — `example` can't be pinned).
2. Add a `[[goal]]` manifest entry (module + a `context` hint: anchor spec,
   known-good tactics, prior failed approaches).
3. `leanloop vet <module>` FIRST — don't spend prover budget on a false or
   vacuous statement.
4. `leanloop run --manifest …` → battery → local prover → queue; clear the
   queue interactively; on accept the proof is applied — review the diff,
   then promote the module into the project's sorry-free default target +
   axiom-check file, and commit.
