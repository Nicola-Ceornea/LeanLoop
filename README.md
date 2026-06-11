# LeanLoop

**An adversarial LLM-driven formal-verification loop for Rust firmware:
Rust → Charon → Aeneas → Lean 4, with a tiered prover stack and the Lean
kernel as the only ground truth.**

LeanLoop takes an Aeneas-generated Lean 4 project plus a list of proof goals
(theorems ending in `sorry`) and grinds them closed with a tier cascade:

```
                ┌────────────────────────────────────────────────┐
 goals  ──────▶ │ Tier 0  non-LLM tactic battery (free, instant) │
 (files with    │   scalar_tac → omega → simp/grind → step* …    │
  `sorry`)      ├────────────────────────────────────────────────┤
                │ Tier 1  LOCAL PROVER  (Goedel-Prover-V2-8B)    │
                │   pass@N sampling + verifier-guided            │
                │   self-correction; via Ollama — local OR remote│
                ├────────────────────────────────────────────────┤
                │ Tier 2  FRONTIER (claude CLI, sparse)          │
                │   whole-proof + repair on hard goals           │
                └───────────────┬────────────────────────────────┘
                                ▼
                ┌────────────────────────────────────────────────┐
                │ KERNEL GATE   lake build (Lean kernel checks)  │
                │ AUDIT GATE    no sorry/admit ▸ no new axioms ▸ │
                │               no native_decide ▸ #print axioms │
                │               closure ⊆ allowed set            │
                │               (fail-closed)                    │
                └───────────────┬────────────────────────────────┘
                                ▼
                  accepted proof → applied / PR     +  sqlite run log
                                                       (expert-iteration corpus)
```

**Soundness model:** every candidate proof — no matter which tier produced it —
must (1) build under the Lean kernel, (2) contain no `sorry`/`admit`, no
non-whitelisted `axiom`, no `native_decide`, and (3) have an `#print axioms`
closure inside the allowed set, **failing closed** if the closure can't be
verified. A wrong or malicious model output simply fails the gate; nothing
unverified survives. Models are heuristics; the kernel is the judge.

Design rationale: [`docs/architecture.md`](docs/architecture.md) (the tier
stack and the acceptance gates) and [`docs/deployment.md`](docs/deployment.md).

## Status (v0.1 — what actually runs today)

**Implemented and tested:** the tier cascade (tactic battery → local Ollama
prover with pass@N + verifier-guided self-correction → frontier `claude` CLI);
the kernel gate; the audit gate including **statement-pinning** (a candidate
must prove the goal's exact theorem name + signature — not a weakened one),
no-`sorry`/`admit`, no candidate-declared `axiom`s, no `native_decide`, and a
**fail-closed** `#print axioms` closure check; goal scan + ordered manifests;
the sqlite run log; single-machine and remote-Ollama deployment. The soundness
gate is validated end-to-end against a real Lean toolchain
(`tests/test_soundness.py`).

**Roadmap (not yet implemented — named honestly):** Kimina-Lean-Server backend,
per-theorem goal splitting + lemma-library harvesting, error-class-routed
repair prompts, prover ensembling, negation probing / Rust mutation testing for
spec assurance, QLoRA expert iteration. Listed again at the bottom.

---

## Install

```bash
git clone https://github.com/Nicola-Ceornea/LeanLoop && cd LeanLoop
pip install -e .          # needs Python ≥3.10
leanloop --help
```

Prerequisites on the **loop machine** (where Lean runs):
* `elan`/`lake` with your Aeneas Lean project building (`lake build` works)
* optionally the `claude` CLI, logged in, for the frontier tier

---

## Running everything on a single machine

One box runs the loop, Lean, *and* the Goedel prover (CPU or any GPU Ollama
supports).

```bash
# 1. set up the prover (installs Ollama, downloads Goedel-Prover-V2-8B Q8_0
#    ≈ 8.7 GB, imports it as an Ollama model):
bash scripts/setup_prover_host.sh

# 2. configure:
cp configs/local.toml leanloop.toml
$EDITOR leanloop.toml          # set project.root to your Lean project

# 3. sanity-check and go:
leanloop check-prover          # ✓ reachable, model present
leanloop scan                  # list goal modules (files containing `sorry`)
leanloop prove Extracted.Bits  # one goal through the tier cascade
leanloop run --apply           # the overnight loop over every goal
```

`--apply` writes accepted proofs back into the project — review and commit
them yourself; the diff/PR is the human checkpoint (specs are the unverified
layer).

To run **fully offline** (no frontier tier at all), set
`[prover.frontier] backend = "none"` — Tier 0 + the local prover still run.

---

## Running with self-hosted Goedel via Ollama (remote GPU box)

The loop + Lean run on your main machine; inference runs on a second box with
a GPU (tested target: AMD RX 6950 XT, 16 GB — Goedel-8B Q8_0 fits with ~7 GB
to spare for KV cache). The *only* config difference is the URL.

**On the GPU box** (e.g. the 6950 XT machine):

```bash
bash scripts/setup_prover_host.sh --serve-lan
# installs Ollama, downloads + imports the model, and sets OLLAMA_HOST=0.0.0.0
# so the server listens on the LAN. Open the firewall for your loop machine:
sudo ufw allow from <loop-machine-ip> to any port 11434 proto tcp
```

AMD RDNA2 note: if `ollama ps` shows CPU instead of GPU, set
`HSA_OVERRIDE_GFX_VERSION=10.3.0` for the Ollama service (gfx1030 — the exact
override is printed by the setup script).

VRAM / context / concurrency tuning (they interact — Ollama shares one context
budget across parallel slots) is in **`docs/deployment.md`**.

**On the loop machine:**

```bash
cp configs/remote-goedel.toml leanloop.toml
$EDITOR leanloop.toml                      # set base_url to the GPU box's IP
leanloop check-prover                      # verifies reachability + model
leanloop run --apply
```

Or override per-run without editing config:

```bash
LEANLOOP_PROVER_URL=http://192.168.1.50:11434 leanloop run
```

Anything OpenAI-compatible also works (llama.cpp `llama-server`, vLLM):
set `backend = "openai"` and point `base_url` at it — useful if you outgrow
Ollama and want llama.cpp's parallel slots tuned by hand.

---

## Goal manifests (bottom-up proving)

`leanloop run` defaults to scanning for every file containing `sorry`. For
call-graph-ordered proving (leaf lemmas first — they become premises for
callers), pass an ordered manifest:

```toml
# goals.toml
[[goal]]
module  = "Extracted.Bits"
context = "Disjoint-OR equals ADD; try Nat.eq_of_testBit_eq if search fails."

[[goal]]
module  = "Extracted.ForsSpec"   # uses Bits lemmas once they're proven
```

```bash
leanloop run --manifest goals.toml --apply
```

## The audit whitelist

If your project legitimately depends on reviewed content axioms (e.g. an
uninterpreted hash function modeling a hardware primitive), whitelist them:

```toml
[audit]
axiom_whitelist = ["keccak256_pure"]
```

Everything else — `sorryAx`, surprise axioms, `native_decide` — is rejected.

## Run log

Every attempt (accepted or not) is logged to sqlite (`leanloop_runs.sqlite`):
goal, tier, model, sampling params, Lean errors, axiom closure, wall-clock.
That's the triage trail *and* the future expert-iteration (QLoRA) corpus.
`leanloop stats` summarizes per-tier yield.

## Cost notes

* **Tier 0 and Tier 1 are free** (your hardware).
* The frontier tier shells out to `claude -p`. Anthropic's June 15, 2026
  billing change moved headless CLI calls to a metered credit pool ($20–$200
  monthly allotment by plan) — that's why the architecture keeps the frontier
  tier *sparse* (~5–10 % of calls) and the local prover does the volume.
  Disable it entirely with `backend = "none"` or `LEANLOOP_FRONTIER_DISABLE=1`.

## Trust chain (what a green run does and doesn't mean)

A proof here is a machine-checked theorem **about the Aeneas model of your
Rust** under your stated axioms. Residual trust: (1) specs say what you meant
— mitigate with mutation/negation testing; (2) rustc→Charon→Aeneas translation
is faithful — mitigate with differential testing; (3) your whitelisted
hardware axioms are true; (4) rustc + the Lean kernel. Constant-time and
information-flow are out of scope entirely.

## Roadmap

* Kimina Lean Server backend (batched, persistent checking — 1.5–2× faster)
* per-theorem goal splitting + auxiliary-lemma harvesting into a project
  lemma library (`@[grind]`/`@[progress]`)
* error-class-routed repair prompts (AutoVerus-style taxonomy from the run log)
* prover ensembling (DeepSeek-Prover-V2-7B, Kimina-RL-1.7B fast tier)
* negation probing + Rust mutation testing for spec assurance
* QLoRA expert iteration on the logged (goal, proof) corpus
