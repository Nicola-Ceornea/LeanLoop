#!/usr/bin/env bash
# ============================================================================
# LeanLoop prover-host setup — run ON the machine that serves Goedel inference.
# Works for BOTH deployments:
#   * single-machine: run it on the same box as the loop
#   * remote GPU box (e.g. RX 6950 XT): run it there, then `--serve-lan`
#
# What it does:
#   1. installs Ollama if missing
#   2. downloads the Goedel-Prover-V2-8B GGUF (default Q8_0, ~8.7 GB)
#   3. imports it into Ollama via scripts/Modelfile.goedel
#   4. (--serve-lan) configures Ollama to listen on 0.0.0.0 for LAN clients
#
# Env overrides:
#   GOEDEL_QUANT=Q6_K     smaller (~6.7 GB), more KV-cache headroom
#   GGUF_DIR=~/models     where to store the GGUF
#
# AMD RDNA2 note (RX 6950 XT = gfx1030): Ollama uses ROCm; if the GPU is not
# detected, set HSA_OVERRIDE_GFX_VERSION=10.3.0 for the Ollama service (see
# the troubleshooting block printed at the end).
# ============================================================================
set -euo pipefail

QUANT="${GOEDEL_QUANT:-Q8_0}"
GGUF_DIR="${GGUF_DIR:-$HOME/models}"
MODEL_NAME="goedel-prover-v2-8b"
GGUF_FILE="Goedel-Prover-V2-8B.${QUANT}.gguf"
# Verified to exist (mradermacher repo, June 2026): Goedel-Prover-V2-8B.Q8_0.gguf
# and .Q6_K.gguf. If HF layout changes, download any Goedel-V2-8B GGUF manually
# into $GGUF_DIR with this exact filename, or `ollama pull hf.co/<repo>:<quant>`.
GGUF_URL="https://huggingface.co/mradermacher/Goedel-Prover-V2-8B-GGUF/resolve/main/${GGUF_FILE}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- 1. Ollama --------------------------------------------------------------
if ! command -v ollama >/dev/null 2>&1; then
  echo "== installing Ollama =="
  curl -fsSL https://ollama.com/install.sh | sh
else
  echo "== Ollama already installed: $(ollama --version 2>/dev/null | head -1) =="
fi

# --- 2. GGUF ----------------------------------------------------------------
mkdir -p "$GGUF_DIR"
if [ ! -f "$GGUF_DIR/$GGUF_FILE" ]; then
  echo "== downloading $GGUF_FILE (~8.7 GB for Q8_0; resume-capable) =="
  curl -fL --retry 5 -C - -o "$GGUF_DIR/$GGUF_FILE" "$GGUF_URL"
else
  echo "== GGUF already present: $GGUF_DIR/$GGUF_FILE =="
fi

# --- 3. import into Ollama ---------------------------------------------------
if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "${MODEL_NAME}:latest"; then
  echo "== model '${MODEL_NAME}' already created =="
else
  echo "== creating Ollama model '${MODEL_NAME}' =="
  TMP_MODELFILE="$(mktemp)"
  sed "s|{GGUF_PATH}|$GGUF_DIR/$GGUF_FILE|" "$HERE/Modelfile.goedel" > "$TMP_MODELFILE"
  ollama create "$MODEL_NAME" -f "$TMP_MODELFILE"
  rm -f "$TMP_MODELFILE"
fi

# --- 4. optional: serve on the LAN -------------------------------------------
if [ "${1:-}" = "--serve-lan" ]; then
  echo "== configuring Ollama to listen on 0.0.0.0 (LAN) =="
  if [ -d /etc/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
    sudo mkdir -p /etc/systemd/system/ollama.service.d
    # OLLAMA_NUM_PARALLEL must match LeanLoop's [prover.local] concurrency (default 2)
    # or concurrent samples queue instead of batching. OLLAMA_HOST opens the LAN.
    printf '[Service]\nEnvironment="OLLAMA_HOST=0.0.0.0"\nEnvironment="OLLAMA_NUM_PARALLEL=2"\n' | \
      sudo tee /etc/systemd/system/ollama.service.d/leanloop-lan.conf >/dev/null
    sudo systemctl daemon-reload && sudo systemctl restart ollama
    echo "Ollama now listens on 0.0.0.0:11434 with NUM_PARALLEL=2 (systemd override installed)."
  else
    echo "No systemd detected — start manually with: OLLAMA_HOST=0.0.0.0 ollama serve"
  fi
  echo "Remember to allow TCP 11434 from your loop machine in the firewall, e.g.:"
  echo "  sudo ufw allow from <loop-machine-ip> to any port 11434 proto tcp"
fi

# --- smoke test ---------------------------------------------------------------
echo "== smoke test =="
ollama run "$MODEL_NAME" "Complete the following Lean 4 code:

\`\`\`lean4
theorem t : 1 + 1 = 2 := by sorry\`\`\`" --verbose 2>&1 | tail -5 || true

cat <<'EOF'

------------------------------------------------------------------------------
Done. Point LeanLoop at this host:
  single-machine : base_url = "http://localhost:11434"        (configs/local.toml)
  remote         : base_url = "http://<this-box-ip>:11434"    (configs/remote-goedel.toml)
  quick override : LEANLOOP_PROVER_URL=http://<ip>:11434 leanloop run ...

TROUBLESHOOTING (AMD RX 6950 XT / RDNA2, gfx1030):
  * GPU not used (CPU-only inference)? Force the ROCm arch for the service:
      sudo mkdir -p /etc/systemd/system/ollama.service.d
      printf '[Service]\nEnvironment="HSA_OVERRIDE_GFX_VERSION=10.3.0"\n' | \
        sudo tee /etc/systemd/system/ollama.service.d/rocm-rdna2.conf
      sudo systemctl daemon-reload && sudo systemctl restart ollama
  * Verify GPU offload:  ollama ps   (should show 100% GPU for the model)
  * ROCm missing entirely: install AMD's rocm packages for your distro first.
------------------------------------------------------------------------------
EOF
