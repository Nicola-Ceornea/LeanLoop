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
                │ Tier 2  FRONTIER (sparse) — pick one:          │
                │   queue→interactive Claude Code (free) ·       │
                │   claude -p (metered) · openai · none          │
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

Design + ops docs: [`architecture.md`](docs/architecture.md) (tier stack +
gates), [`gpu-box-setup.md`](docs/gpu-box-setup.md) (AMD ROCm + Ollama +
**Tailscale** for the remote prover box), [`deployment.md`](docs/deployment.md)
(VRAM/concurrency tuning), and [`operations.md`](docs/operations.md) (unattended
runs over SSH, monitoring, recovery).

## Status (v0.1 — what actually runs today)

**Implemented and tested:** the tier cascade (tactic battery → local Ollama
prover with pass@N + verifier-guided self-correction → frontier `claude` CLI);
the kernel gate; the audit gate including **statement-pinning** (a candidate
must prove the goal's exact theorem name + signature — not a weakened one),
no-`sorry`/`admit`, no candidate-declared `axiom`s, no `native_decide`, and a
**fail-closed** `#print axioms` closure check; goal scan + ordered manifests;
the sqlite run log; single-machine and remote-Ollama deployment; and a
**free interactive-frontier mode** (queue hard goals for a flat-rate Claude Code
session via `/leanloop-frontier` + `leanloop submit`); and **spec assurance**
(`leanloop vet` counterexample/vacuity/negation probes + LLM review handoff,
`leanloop mutate` spec-strength via Lean-definition mutation, `leanloop kat`
spec-correctness via official test vectors — all four spec-error classes; see
"Spec assurance"). The
soundness gate is validated end-to-end against a real Lean toolchain
(`tests/test_soundness.py`).

**Roadmap (not yet implemented — named honestly):** Kimina-Lean-Server backend,
per-theorem goal splitting + lemma-library harvesting, error-class-routed
repair prompts, prover ensembling, Rust mutation / differential testing for
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

**On the GPU box** (e.g. the 6950 XT machine) — full walkthrough incl. AMD ROCm
and **Tailscale** (so the boxes can be on different networks, encrypted, no
port-forwarding) is in [`docs/gpu-box-setup.md`](docs/gpu-box-setup.md). Quick
version for a same-LAN setup:

```bash
bash scripts/setup_prover_host.sh --serve-lan
# installs Ollama, downloads + imports the model, sets OLLAMA_HOST=0.0.0.0.
sudo ufw allow from <loop-machine-ip> to any port 11434 proto tcp
```

AMD RDNA2 note: if `ollama ps` shows CPU instead of GPU, set
`HSA_OVERRIDE_GFX_VERSION=10.3.0` for the Ollama service (gfx1030 — the exact
override is printed by the setup script). **Recommended: connect over Tailscale**
and firewall 11434 to the `tailscale0` interface (see the setup guide) — the
Ollama API is unauthenticated, so don't expose it on the open LAN/internet.

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

## The 32B prover on an iGPU / APU box (shared-memory capacity tier)

Goedel-Prover-V2 also ships a **32B** that is markedly stronger than the 8B —
but at ~26 GB (Q4_K_M + KV) it **won't fit fully resident in a 16 GB dGPU**
like the 6950 XT (a 16 GB card can still run it via *partial offload* — see
below). It runs *fully* on an **iGPU/APU with enough shared system memory**,
which makes a laptop/mini-PC APU a genuine *capacity tier*: it hosts the
bigger model your discrete card can't hold whole.

Tested target: **AMD Ryzen AI 9 HX 370 + Radeon 890M (Strix Point), DDR5.**

```bash
# on the iGPU box:
GOEDEL_SIZE=32b bash scripts/setup_prover_host.sh        # add --serve-lan for remote
# on the loop box:
cp configs/local-32b.toml leanloop.toml                  # (or set base_url for remote)
leanloop check-prover && leanloop run --apply
```

What to expect, measured on the 890M:

* **It just works on Vulkan — no ROCm.** Ollama 0.24+ drives the iGPU through
  its Vulkan backend; `ollama ps` shows **100% GPU**. (Install the Vulkan
  loader + ICD; `vulkaninfo` should list the GPU.)
* **No BIOS tweak needed.** The iGPU has no dedicated VRAM — it uses the BIOS
  UMA frame buffer (a few GB) **plus the Linux amdgpu GTT** (system RAM, default
  ~half of RAM). A 26 GB model loaded straight into GTT at 100% GPU. Only raise
  the UMA size or the `amdgpu.gttsize=<MB>` kernel param if you want a model
  *larger* than the GTT cap; **capacity is rarely the limit.**
* **Bandwidth is the limit, not memory.** Per-token speed is fixed by RAM
  bandwidth, so: **8B Q8_0 ≈ 8 tok/s, 32B Q4_K_M ≈ 3.4 tok/s** on dual-channel
  DDR5. Enable EXPO / the fastest stable RAM clock for more. The XDNA2 NPU does
  **not** help (Ollama/llama.cpp can't use it).

Because it's slow-but-strong, treat it as the escalation tier: run the cheap
8B first-pass on a fast dGPU, and point the loop at the 32B box for the hard
goals the 8B can't close (`configs/local-32b.toml` uses `samples = 8`,
`concurrency = 1`, a long `timeout_s`, and keep `OLLAMA_KEEP_ALIVE` high so the
~37 s model load isn't paid per call). `docs/gpu-box-setup.md` covers the
remote/Tailscale wiring; the 8b vs 32b switch is just `GOEDEL_SIZE`.

### Can a 16 GB dGPU (6950 XT) run the 32B too?

Yes — **via partial offload**, with the right settings. Ollama automatically
puts ~3/4 of the Q4_K_M layers in VRAM and streams the rest from system RAM
through the CPU; `ollama ps` then shows a CPU/GPU split, which is **expected,
not a fault**. Per-token speed is gated by the CPU-resident slice, so expect
roughly **2–4× an APU's tok/s** (≈ 7–12 tok/s with desktop RAM) — well below
what a fully-resident model would do. Settings that matter:

* `concurrency = 1` and `OLLAMA_PARALLEL=1` (the setup script defaults to 1
  for `GOEDEL_SIZE=32b`) — parallel slots just split bandwidth.
* **Keep `num_ctx` modest** (`local-32b.toml` uses 12288): on a dGPU every GB
  of KV cache evicts another layer to the slow CPU side — this matters far
  more than on an APU's shared memory.
* Stay on **Q4_K_M**, not Q3: formal output is quant-sensitive, and Q3_K_M's
  ~16 GB would still leave no KV room anyway.
* The 32B evicts the 8B from the card. Rule of thumb: pass@8 with the 32B at
  ~10 tok/s costs about the same wall-clock as pass@32 with the 8B — run
  `leanloop bench --goals N` with each and keep whichever closes more goals
  per hour. (The two-box split — 8B on the dGPU, 32B on the APU — keeps both.)

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
module  = "Extracted.NextLemma"   # uses Bits lemmas once they're proven
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

## Monitoring & recovery (unattended runs over SSH)

Built for long runs on a remote GPU box. Full playbook in
[`docs/operations.md`](docs/operations.md); the essentials:

```bash
leanloop doctor                 # preflight: prover up? model ON GPU? lake? disk? queue?
leanloop bench --samples 5      # Phase 0: tokens/sec on this GPU + per-goal time estimate
#                                 (add --goals N to also measure local close-rate)
tmux new -s ll 'leanloop run --apply'   # survives SSH disconnect
leanloop status --watch         # live: ALIVE/STALLED/FINISHED, done/total, last activity
```

- **`doctor`** checks the prover endpoint, whether the model is actually
  GPU-resident (Ollama `/api/ps` — catches the silent partial-CPU slowdown),
  the `lake` toolchain, disk, and the queue; non-zero exit on hard failures.
- **`status`/`--watch`** reads the run's sqlite heartbeat (WAL — never blocks the
  run) and flags **STALLED/DEAD** if no attempt logged in 3 min.
- **Crash-resilient:** if the prover goes down mid-run (OOM / GPU-box reboot),
  the loop waits + retries with backoff (`health_max_wait_s`, default 600 s) and
  resumes automatically. If the *loop* dies, just re-run — solved goals are
  cached and skipped, so it picks up where it left off.
- **On the GPU box:** `scripts/gpu_box_status.sh [--watch|--restart|--logs]` —
  Ollama health, GPU residency, and recovery, over SSH.

## Run log

Every attempt (accepted or not) is logged to sqlite (`leanloop_runs.sqlite`):
goal, tier, model, sampling params, Lean errors, axiom closure, wall-clock.
That's the triage trail *and* the future expert-iteration (QLoRA) corpus.
`leanloop stats` summarizes per-tier yield.

## Cost modes (the frontier tier)

**Tier 0 and Tier 1 are always free** (your hardware). Only the frontier tier
can cost money, and you pick how via `[prover.frontier] backend`:

| `backend` | what happens | cost |
|-----------|--------------|------|
| `"queue"` | hard goals are **queued for an interactive Claude Code session** to clear | **free** — interactive Claude Code is subscription flat-rate |
| `"claude_cli"` | the loop calls headless `claude -p` automatically | metered: headless CLI uses the credit pool ($20–$200/mo by plan, post 2026-06-15) |
| `"openai"` | an OpenAI-compatible endpoint | pay per token |
| `"none"` | no frontier tier (local-only) | free |

### `queue` — use interactive Claude Code (flat-rate) as the frontier

This is the cheapest way to get frontier-quality proofs. The billing split is
the reason it exists: **headless** `claude -p` draws on the metered credit pool,
but an **interactive** Claude Code session stays on your subscription flat-rate.
So the loop hands hard goals off to you instead of spending credits:

```toml
[prover.frontier]
backend = "queue"
```

```bash
# the overnight loop runs Tier 0 + local prover, and QUEUES what it can't close:
leanloop run --apply
#   → N goal(s) queued for the interactive frontier

# then, in an interactive Claude Code session in the SAME dir:
/leanloop-frontier            # the bundled slash command clears the queue
# …or drive it by hand:
leanloop frontier             # list pending tasks
leanloop frontier --next      # print the next task's prompt (goal + criteria)
#   write the proof to the task's <goal>.candidate.lean, then:
leanloop submit <goal>        # re-verifies through ALL gates; applies on pass
```

`leanloop submit` runs the **exact same** statement-pin + audit + kernel +
axiom-closure gates as the automated tiers — so a proof produced in an
interactive session is just as sound (a cheat or a `sorry` is rejected the same
way). The tradeoff vs `claude_cli`: `queue` is free but not unattended (you, or
a Claude Code session you start, clear the queue); `claude_cli` is unattended
but metered. You can also disable the frontier entirely with `backend = "none"`
or `LEANLOOP_FRONTIER_DISABLE=1`.

## Spec assurance: `leanloop vet` (trusting specs without reading Lean)

The kernel proves "the code matches the spec" — it cannot tell you the **spec
says what you meant**. That's the one human checkpoint, and you don't need Lean
expertise to do it: `vet` gives mechanical, kernel/test-backed signals, and
`--explain` turns the spec into a plain-English adversarial review.

```bash
leanloop vet Extracted.Bits             # mechanical probes
leanloop vet Extracted.Bits --explain   # + plain-English spec review prompt
```

Three probes per theorem (each a real Lean run inside your project, never an
LLM opinion):

| probe | question | signal |
|-------|----------|--------|
| **CEX** | is the statement *false*? (`plausible` random-tests it) | 🔴 RED + a **concrete counterexample** if so |
| **HYP** | are the hypotheses *satisfiable*? (witness search) | 🟢 witness found → not vacuous · 🟡 none → may be **vacuously true** |
| **NEG** | does the conclusion's *negation* prove with cheap tactics? | 🔴 RED if so (kernel-checked falsity) |

A spec that's *false* or *vacuous* is caught before any prover budget is spent
— and before you trust a green proof of it. (Validated live: a planted
off-by-one spec → RED with witness; a planted contradictory-hypothesis spec →
YELLOW.) Probes need the `Plausible` package (ships with Mathlib-based
projects); without it they degrade to SKIP, never to false confidence.

`--explain` then writes a self-contained review prompt (statement-by-statement:
*what does this guarantee in plain English, what does it NOT say, could a wrong
implementation still pass it?*) — drop it into an interactive Claude Code
session (flat-rate), or it runs directly via `claude_cli` if that's your
frontier backend. The mechanical verdicts are embedded in the prompt so the
LLM review is anchored to ground truth.

**Division of labor:** the LLM *drafts and critiques* specs (it's good at
translating Lean ↔ English and spotting gaps); the *trust* comes from the
mechanical probes + the kernel. LLM opinion is never the acceptance signal.

### The four classes of spec error, and what catches each

`vet` is one of three spec-assurance commands. A spec can fail in four
mechanical ways; LeanLoop now covers all four (design verified by the
two-pass research in `docs/`):

| spec error | what it means | command |
|---|---|---|
| **false** | provably wrong | `vet` (CEX/NEG) |
| **vacuous** | true only because nothing satisfies its hypotheses | `vet` (HYP) |
| **weak** | true & non-vacuous, but a *buggy* impl still satisfies it | **`mutate`** |
| **wrong-property** | precise & strong, but about the *wrong thing* | **`kat`** |

**`leanloop mutate` — spec STRENGTH.** Mutates the extracted Lean *definitions*
(mCoq's method: mutate the model, never the proofs) and rebuilds the pinned
proofs. A mutant the proofs **catch** = killed (good); one that **survives** =
a bug no spec noticed, shown as a plain-English diff — *~85% of survivors are
real spec gaps* (mCoq). Custom crypto operators flip domain-separator bytes,
ADRS constants, length fields, shifts.

```bash
leanloop mutate Extracted/Parser/Funs.lean --build-target Extracted.ParserProofs --sample 20
#   ✓killed 'bound 11→12'   ✗LIVED '8→9' (a constant the proofs don't pin — real gap)
#   mutation score: …%   survivors listed as Rust/Lean diffs
```

**`leanloop kat` — spec WRONGNESS (the gap no probe can close).** Runs the
project's executable Lean spec on official standard test vectors (NIST
SLH-DSA / SPHINCS+ KATs) and requires **byte-equality**. This is the
ML-KEM/SymCrypt "executable and testable specs" pattern: if the spec
reproduces the standard's vectors *and* a kernel-checked refinement theorem
ties spec → code, KAT-grounding is **transitive** — the verified code is
anchored to the standard, not to a spec someone hoped was right.

```toml
[kat]
module  = "Spec.MyScheme"
adapter = "Spec.sign_bytes"   # a Lean `List UInt8 → List UInt8` view of the spec
vectors = "vectors/slh-dsa-shake-128f.rsp"   # official KAT file (jsonl/hex/.rsp)
```
```bash
leanloop kat        # ✓ N/N vectors passed — spec matches the standard byte-for-byte
```
(Validated end-to-end: a corrupted vector and a one-byte-wrong spec are both
caught. It grounds *functional correctness on exercised paths* — **not**
security properties, unexercised paths, or the abstraction function, which stay
human review.)

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
* Rust mutation testing + differential testing for spec assurance
  (statement-level negation probing + counterexample search: **done** — `leanloop vet`)
* QLoRA expert iteration on the logged (goal, proof) corpus
