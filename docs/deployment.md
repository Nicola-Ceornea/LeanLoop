# Deployment & tuning

LeanLoop runs in two shapes; both use the same code and differ only by the
prover `base_url`.

```
SINGLE MACHINE                         REMOTE PROVER
┌────────────────────────┐            ┌───────────────────┐   ┌──────────────────────┐
│ loop + Lean + Ollama   │            │ loop + Lean       │──▶│ Ollama (RX 6950 XT)  │
│ base_url=localhost     │            │ base_url=gpu-box  │LAN│ OLLAMA_HOST=0.0.0.0   │
└────────────────────────┘            └───────────────────┘   └──────────────────────┘
```

## VRAM, context, and concurrency (the knobs that interact)

On a 16 GB card running **Goedel-Prover-V2-8B Q8_0** (~8.7 GB weights), the
rest of VRAM (~7 GB) is the KV cache, and **Ollama shares one context budget
across `OLLAMA_NUM_PARALLEL` slots**. So:

```
effective context per concurrent sample  ≈  num_ctx / OLLAMA_NUM_PARALLEL
```

LeanLoop's `[prover.local] concurrency` is how many samples it sends at once —
it should **equal `OLLAMA_NUM_PARALLEL`** on the host, or extra samples just
queue. The setup script sets `OLLAMA_NUM_PARALLEL=2` to match the default
`concurrency=2`.

Safe 16 GB defaults (what ships):

| knob | value | why |
|------|-------|-----|
| quant | `Q8_0` | formal output is quant-sensitive; Q8 fidelity matters |
| `num_ctx` (Modelfile + config) | `24576` | total ctx; ÷2 slots ≈ 12k tokens/sample |
| `concurrency` / `OLLAMA_NUM_PARALLEL` | `2` | 2 slots × ~3 GB KV ≈ 6 GB, fits |
| `samples` (pass@N) | `32` | per-goal budget |

If you have more VRAM (24 GB+), raise `concurrency`+`OLLAMA_NUM_PARALLEL`
together and/or `num_ctx`. To trade fidelity for KV headroom, drop to `Q6_K`
(`GOEDEL_QUANT=Q6_K bash scripts/setup_prover_host.sh`, ~6.7 GB).

Confirm the model is actually on the GPU: `ollama ps` should show ~100% GPU.

## AMD RX 6950 XT (RDNA2, gfx1030)

Ollama uses ROCm. The 6950 XT is community-supported (not officially tested);
if it falls back to CPU, force the arch for the service:

```bash
sudo mkdir -p /etc/systemd/system/ollama.service.d
printf '[Service]\nEnvironment="HSA_OVERRIDE_GFX_VERSION=10.3.0"\n' | \
  sudo tee /etc/systemd/system/ollama.service.d/rocm-rdna2.conf
sudo systemctl daemon-reload && sudo systemctl restart ollama
```

## Throughput expectations (no rush is the point)

A whole-proof sample (plan + proof) is ~500–2000 output tokens. On a 6950 XT
running 8B Q8_0 expect roughly **15–40 tok/s** (benchmark with `llama-bench` /
`ollama run --verbose`), i.e. tens of seconds per sample, a pass@32 budget in
~10–30 min, and a goal (with self-correction) in well under an hour. This is an
overnight, throughput-oriented tool — `samples`, `concurrency`, and
`self_correct_rounds` trade wall-clock for close-rate.

Note: LeanLoop verifies one candidate at a time with `lake build`, which on a
big Aeneas+Mathlib project is the real wall-clock cost (10–60 s/check cold). A
Kimina-Lean-Server backend (roadmap) amortizes that by keeping the environment
warm — the single biggest speedup available, larger than any model choice.

## Why Ollama (the founding doc benchmarks llama.cpp)

The design doc tunes `llama.cpp` (`--parallel`, `--batch-size`, ROCm vs
Vulkan) for maximum throughput. LeanLoop defaults to **Ollama** for one-command
setup and a stable HTTP API; Ollama wraps llama.cpp under the hood. If you
outgrow it, set `backend = "openai"` and point `base_url` at a hand-tuned
`llama-server` / vLLM — LeanLoop speaks the OpenAI `/v1/chat/completions` wire
format too, no code change.

## Remote security

The Ollama API is unauthenticated. When serving on the LAN, restrict it to the
loop machine — don't expose `0.0.0.0:11434` to untrusted networks:

```bash
sudo ufw allow from <loop-machine-ip> to any port 11434 proto tcp
sudo ufw deny 11434
```

For anything beyond a trusted LAN, tunnel over SSH instead
(`ssh -L 11434:localhost:11434 gpu-box`) and keep `base_url=localhost`.
