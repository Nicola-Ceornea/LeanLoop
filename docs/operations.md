# Operations: running unattended over SSH, monitoring, and recovery

LeanLoop is built for long, unattended runs on a remote GPU box. This is the
playbook for keeping one healthy over SSH and recovering when something breaks.

## Topologies

```
A) all on the GPU box        B) split (loop here, prover there)
   ssh gpu-box                  loop+Lean on your machine ── LAN ── Ollama on GPU box
   loop + Lean + Ollama         (configs/remote-goedel.toml)     (configs/local.toml)
```

Either works. The monitoring commands run wherever the **loop** runs (they read
its sqlite DB + queue); `scripts/gpu_box_status.sh` runs on the **prover** host.

## Before a run: `leanloop doctor`

One command, run it after you SSH in:

```bash
leanloop -c leanloop.toml doctor
```

It checks, with ✓/⚠/✗: the project root + lakefile, the `lake` toolchain, the
prover endpoint + that your model is loaded, **whether the model is actually on
the GPU** (via Ollama `/api/ps` — a partial-CPU load is your "why is it slow"
answer), the frontier backend, free disk, the frontier-queue depth, and the
liveness of the last run. Exit code is non-zero if anything is a hard failure,
so you can gate a launch script on it.

## Launch so it survives your SSH session

`leanloop run` is a long foreground process — don't let your SSH disconnect
kill it. Use `tmux` (simplest) or `nohup`:

```bash
tmux new -s leanloop
leanloop -c leanloop.toml run --apply --manifest goals.toml
#  detach: Ctrl-b d   ·   reattach later: tmux attach -t leanloop
```

or:

```bash
nohup leanloop -c leanloop.toml run --apply > leanloop.out 2>&1 &
```

## While it runs: `leanloop status`

```bash
leanloop -c leanloop.toml status            # one snapshot
leanloop -c leanloop.toml status --watch    # live, refreshes every 10s
```

Shows: **ALIVE / STALLED-DEAD / FINISHED**, host + pid, `done/total` + queued,
the current goal, when it started, **last activity** (advances every few seconds
because every tactic and every sample logs an attempt), per-tier yield, and the
last 8 attempts with outcome + Lean error snippet. "STALLED/DEAD" means no
attempt has been logged in 3 minutes — investigate.

`status` reads the DB in WAL mode, so it never blocks the running loop, and it's
safe to run from a second SSH session.

## Recovery playbook

**The prover (Ollama) crashed / OOM'd / the GPU box rebooted.**
The loop already tolerates this: before each goal's local tier it pings the
prover and, if it's down, **waits and retries with backoff** for up to
`health_max_wait_s` (default 600 s), then resumes automatically when Ollama is
back. To fix the prover itself, on the GPU box:

```bash
./scripts/gpu_box_status.sh            # is it down? is the model on GPU?
./scripts/gpu_box_status.sh --restart  # bounce the service
./scripts/gpu_box_status.sh --logs     # why did it die (OOM? gfx error?)
```

If it OOM'd, lower `concurrency`/`OLLAMA_NUM_PARALLEL` or `num_ctx`, or drop to
`Q6_K` (see `docs/deployment.md`).

**The loop died (machine rebooted, tmux killed, OOM on the loop box).**
Just run it again — it's **idempotent**. Solved goals are recorded in the DB and
skipped (`already solved (cached)`), so `leanloop run` resumes from where it
stopped. `status` will show the prior run as STALLED/DEAD until you relaunch.

**`status` says STALLED but the prover is fine.**
Usually a single `lake build` is grinding a hard goal (cold Mathlib builds can
take a while) — check `leanloop.out`/the tmux pane and the "working on it Xm"
age in `status`.

**One goal is eating the night.** Every goal has a hard wall-clock budget,
`[prover] goal_timeout_s` (default 3600 s): on expiry the remaining tiers are
skipped and the goal falls through to the frontier (queued if
`backend = "queue"`), so the run moves on. Lower it for broad first passes;
set `0` for unlimited.

**Goals piling up in the frontier queue (`backend = "queue"`).**
That's expected — they're waiting for you. Clear them in an interactive Claude
Code session: `/leanloop-frontier`, or `leanloop frontier --next` →
write the proof → `leanloop submit <goal>`. See the README "Cost modes".

**Disk filling up.** The sqlite log grows with attempts. It's safe to archive
`leanloop_runs.sqlite` between campaigns (you lose the resume cache + history,
not any applied proof — those are committed in the project).

## A minimal overnight wrapper

```bash
#!/usr/bin/env bash
set -e
leanloop -c leanloop.toml doctor || { echo "doctor failed"; exit 1; }
tmux new -d -s leanloop "leanloop -c leanloop.toml run --apply --manifest goals.toml"
echo "launched. monitor: leanloop -c leanloop.toml status --watch"
```
