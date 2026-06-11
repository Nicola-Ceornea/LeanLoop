#!/usr/bin/env bash
# ============================================================================
# Run this ON the GPU box (the RX 6950 XT / prover host), e.g. over SSH, to see
# whether Ollama is healthy and the model is actually on the GPU — and to
# recover it.
#
#   ./gpu_box_status.sh            # one-shot health report
#   ./gpu_box_status.sh --watch    # refresh every 3s
#   ./gpu_box_status.sh --restart  # restart the Ollama service, then report
#   ./gpu_box_status.sh --logs     # tail recent Ollama logs
#
# Env: MODEL=goedel-prover-v2-8b   OLLAMA=http://localhost:11434
# ============================================================================
set -uo pipefail
MODEL="${MODEL:-goedel-prover-v2-8b}"
OLLAMA="${OLLAMA:-http://localhost:11434}"

restart() {
  echo "== restarting Ollama =="
  if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl restart ollama && echo "restarted (systemd)."
  else
    pkill -f "ollama serve" 2>/dev/null
    echo "killed any 'ollama serve'; start it again with: OLLAMA_HOST=0.0.0.0 ollama serve &"
  fi
}

logs() {
  echo "== recent Ollama logs =="
  if command -v journalctl >/dev/null 2>&1; then
    journalctl -u ollama -n 40 --no-pager 2>/dev/null || echo "(no systemd journal for ollama)"
  else
    echo "(no journalctl; check wherever you redirected 'ollama serve' output)"
  fi
}

gpu() {
  echo "== GPU =="
  if command -v rocm-smi >/dev/null 2>&1; then
    rocm-smi --showuse --showmeminfo vram 2>/dev/null | grep -iE "GPU|use|vram|%" | head -12
  elif command -v radeontop >/dev/null 2>&1; then
    timeout 2 radeontop -d - -l 1 2>/dev/null | tail -1
  elif [ -r /sys/class/drm/card0/device/gpu_busy_percent ]; then
    echo "gpu_busy: $(cat /sys/class/drm/card0/device/gpu_busy_percent)%"
  else
    echo "(no rocm-smi / radeontop / sysfs gpu_busy — install rocm-smi for detail)"
  fi
}

report() {
  echo "================ LeanLoop GPU-box status ($(date +%H:%M:%S)) ================"
  # 1. service
  if command -v systemctl >/dev/null 2>&1; then
    echo "ollama service: $(systemctl is-active ollama 2>/dev/null || echo unknown)"
  fi
  # 2. reachable + model present
  if curl -fsS "$OLLAMA/api/tags" >/tmp/_tags.json 2>/dev/null; then
    if grep -q "$MODEL" /tmp/_tags.json; then
      echo "endpoint: UP · model '$MODEL' installed"
    else
      echo "endpoint: UP · model '$MODEL' NOT installed — run setup_prover_host.sh"
    fi
  else
    echo "endpoint: DOWN ($OLLAMA unreachable) — try --restart, or check it's serving 0.0.0.0"
  fi
  # 3. loaded models + GPU residency (Ollama /api/ps)
  echo "== loaded (ollama ps) =="
  curl -fsS "$OLLAMA/api/ps" 2>/dev/null | python3 -c '
import sys, json
try:
    for m in json.load(sys.stdin).get("models", []):
        v, s = m.get("size_vram",0), m.get("size",0) or 1
        pct = round(100*v/s)
        print(f"  {m.get(\"name\")}: {pct}% GPU  ({\"GPU-resident\" if pct>=99 else \"PARTIAL CPU — slow\"})")
    else:
        pass
except Exception:
    print("  (none loaded / parse error)")
' 2>/dev/null || echo "  (none)"
  gpu
}

case "${1:-}" in
  --restart) restart; sleep 2; report ;;
  --logs)    logs ;;
  --watch)   while true; do clear; report; echo; echo "Ctrl-C to stop"; sleep 3; done ;;
  *)         report ;;
esac
