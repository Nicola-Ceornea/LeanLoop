# GPU box setup: Linux + AMD ROCm + Ollama + Tailscale

End-to-end setup for the machine that runs the local prover (the **GPU box** —
e.g. an AMD RX 6950 XT), reachable from your loop machine over **Tailscale** so
the two can be on different networks, encrypted, with no port-forwarding.

```
   loop machine (runs LeanLoop + Lean)                 GPU box (this guide)
   base_url = http://gpu-box:11434  ──── Tailscale ───▶  Ollama + Goedel + 6950 XT
                                    (100.x.y.z / MagicDNS)
```

Target OS: a recent Ubuntu/Debian (22.04+/12+) or similar. Commands use `apt`
and `systemd`; adapt for your distro.

---

## 1. AMD GPU driver + ROCm

Ollama ships its own ROCm libraries, so you often **don't** need a full ROCm
install — just a working `amdgpu` kernel driver (in-tree on modern kernels) and
your user in the right groups. Do this first:

```bash
sudo usermod -aG render,video "$LOGNAME"     # GPU device access
# log out/in (or reboot) for group changes to take effect
ls -l /dev/kfd /dev/dri/renderD*             # should exist and be group render/video
```

If you want the full ROCm stack (for `rocm-smi` monitoring, or if Ollama can't
find a GPU), install AMD's packages:

```bash
# AMD's installer (pick the version matching your kernel; see rocm.docs.amd.com)
wget https://repo.radeon.com/amdgpu-install/latest/ubuntu/jammy/amdgpu-install_*.deb
sudo apt install ./amdgpu-install_*.deb
sudo amdgpu-install --usecase=rocm --no-dkms
sudo reboot
rocm-smi                                      # confirms the GPU is visible
```

The RX 6950 XT is **gfx1030** (RDNA2). It's community-supported by ROCm/Ollama,
not officially certified — the one knob you may need is `HSA_OVERRIDE_GFX_VERSION`
(step 3).

---

## 2. Install Ollama + the Goedel model

Use the repo's setup script (installs Ollama, downloads Goedel-Prover-V2-8B
Q8_0 ≈ 8.7 GB, imports it as an Ollama model):

```bash
git clone https://github.com/Nicola-Ceornea/LeanLoop && cd LeanLoop
bash scripts/setup_prover_host.sh
# smaller/more-KV-headroom alternative:
#   GOEDEL_QUANT=Q6_K bash scripts/setup_prover_host.sh
```

Verify it's on the GPU:

```bash
./scripts/gpu_box_status.sh        # endpoint UP, model installed, % on GPU
```

---

## 3. Make the AMD GPU actually get used (gfx1030)

If `gpu_box_status.sh` / `ollama ps` shows the model on CPU (or 0% GPU), force
the ROCm arch for the Ollama service:

```bash
sudo mkdir -p /etc/systemd/system/ollama.service.d
printf '[Service]\nEnvironment="HSA_OVERRIDE_GFX_VERSION=10.3.0"\n' | \
  sudo tee /etc/systemd/system/ollama.service.d/rocm-rdna2.conf
sudo systemctl daemon-reload && sudo systemctl restart ollama
./scripts/gpu_box_status.sh        # expect "100% GPU (GPU-resident)"
```

If it still won't use the GPU, check `journalctl -u ollama -n 50` for ROCm
init errors, and confirm `/dev/kfd` access (step 1).

---

## 4. Tailscale: connect the two machines

Tailscale gives the GPU box a stable address reachable from your loop machine
anywhere, over an encrypted WireGuard mesh — no router config, no public ports.

**On BOTH machines** (GPU box and loop machine):

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up            # opens a browser link; log in to the SAME tailnet
tailscale ip -4              # this node's 100.x.y.z address
```

Give the GPU box a memorable name in the Tailscale admin console (Machines →
rename to e.g. `gpu-box`). With **MagicDNS** on (admin console → DNS → enable),
the loop machine can then reach it as `gpu-box` or `gpu-box.<tailnet>.ts.net`.

---

## 5. Serve Ollama on the tailnet only (not the public/LAN)

Bind Ollama to all interfaces but **firewall port 11434 to the Tailscale
interface only**, so it's reachable over the tailnet and nowhere else:

```bash
# Ollama listens on all interfaces:
sudo mkdir -p /etc/systemd/system/ollama.service.d
printf '[Service]\nEnvironment="OLLAMA_HOST=0.0.0.0"\nEnvironment="OLLAMA_NUM_PARALLEL=2"\n' | \
  sudo tee /etc/systemd/system/ollama.service.d/leanloop-serve.conf
sudo systemctl daemon-reload && sudo systemctl restart ollama

# Firewall: allow 11434 ONLY on the tailscale interface, deny it elsewhere:
sudo ufw allow in on tailscale0 to any port 11434 proto tcp
sudo ufw deny 11434
sudo ufw enable          # if not already on (make sure SSH/22 is allowed first!)
```

`OLLAMA_NUM_PARALLEL` should match LeanLoop's `[prover.local] concurrency`
(default 2). Optionally tighten further in the Tailscale admin console with an
ACL that only lets your loop machine reach `gpu-box:11434`.

> Why not expose it on the LAN? The Ollama API is unauthenticated. Tailscale +
> the interface firewall means only devices on your tailnet (which you control)
> can reach the prover.

---

## 6. Point LeanLoop at it (on the loop machine)

```toml
# leanloop.toml  (from configs/remote-goedel.toml)
[prover.local]
backend  = "ollama"
base_url = "http://gpu-box:11434"          # MagicDNS name (stable)
# or the tailscale IP:  http://100.x.y.z:11434
model    = "goedel-prover-v2-8b"
concurrency = 2
```

Verify and benchmark from the loop machine:

```bash
leanloop check-prover                       # ✓ reachable over the tailnet
leanloop doctor                             # full preflight incl. GPU residency
leanloop bench --samples 5                  # tokens/sec on the 6950 XT (Phase 0)
```

`leanloop bench` is how you get the real throughput number for this card (the
founding doc flags it as unmeasured) and a per-goal time estimate. Add
`--goals N` once `project.root` points at an Aeneas Lean project to also measure
the local tier's first-pass close-rate.

---

## 7. Keep it running

Ollama installs as a systemd service (auto-starts on boot, restarts on crash) —
nothing extra needed for the prover. For the loop itself (on the loop machine),
launch under `tmux`/`nohup` so it survives SSH disconnects — see
[`operations.md`](operations.md).

## Troubleshooting quickref

| symptom | check |
|---|---|
| `check-prover` unreachable | both nodes `tailscale status` up? `ping gpu-box`? ufw allows `tailscale0`:11434? |
| model on CPU / slow | step 3 (`HSA_OVERRIDE_GFX_VERSION=10.3.0`); `./scripts/gpu_box_status.sh` |
| Ollama OOM under load | lower `concurrency`+`OLLAMA_NUM_PARALLEL`, or `num_ctx`, or use `Q6_K` ([deployment.md](deployment.md)) |
| GPU not found at all | `/dev/kfd` perms (step 1), `rocm-smi`, `journalctl -u ollama` |
