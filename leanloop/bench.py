"""`leanloop bench` — measure the two Phase-0 unknowns the founding doc flags:
prover THROUGHPUT (tokens/sec on your actual GPU) and local-tier CLOSE-RATE.

Pure aggregation lives here (testable); the orchestration is in cli.cmd_bench.
"""
from __future__ import annotations

# A representative proof-completion prompt (Goedel format) for the throughput
# probe — a real-ish goal so the model emits a plan + proof of realistic length,
# giving a tokens/sec number that reflects actual proving work, not a one-liner.
THROUGHPUT_GOAL = """import Mathlib

theorem bench_sample (a b : Nat) (h : a ≤ b) : a + 1 ≤ b + 1 := by sorry"""

THROUGHPUT_PROMPT = """Complete the following Lean 4 code:

```lean4
{}```

Before producing the Lean 4 code to formally prove the given theorem, provide a detailed proof plan outlining the main proof steps and strategies.
The plan should highlight key ideas, intermediate lemmas, and proof structures that will guide the construction of the final formal proof.""".format(THROUGHPUT_GOAL)

PROOF_ONLY_THROUGHPUT_PROMPT = """Goal to close: bench_sample

Trusted Lean context and goal:
```lean
{}
```

Return only the Lean proof term replacing `by sorry`, beginning with `by`.""".format(
    THROUGHPUT_GOAL
)


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def summarize_throughput(samples: list[dict]) -> dict:
    """samples = list of generate_timed() dicts → aggregate gen/prompt tok/s."""
    ok = [s for s in samples if s and s.get("gen_toks", 0) > 0]
    gen = [s["gen_tps"] for s in ok]
    prm = [s["prompt_tps"] for s in ok if s.get("prompt_tps")]
    toks = [s["gen_toks"] for s in ok]
    return {
        "n": len(samples), "ok": len(ok),
        "gen_tps_median": round(_median(gen), 1),
        "gen_tps_mean": round(sum(gen) / len(gen), 1) if gen else 0.0,
        "gen_tps_min": round(min(gen), 1) if gen else 0.0,
        "gen_tps_max": round(max(gen), 1) if gen else 0.0,
        "prompt_tps_median": round(_median(prm), 1),
        "gen_toks_median": int(_median(toks)),
    }


def summarize_closerate(rows: list[dict]) -> dict:
    """rows = [{tier, accepted, wall_s}] from running the local stack on goals.
    Reports how many goals each tier closed + wall-clock distribution."""
    n = len(rows)
    by_tactic = sum(1 for r in rows if r["accepted"] and r["tier"] == "tactic")
    by_local = sum(1 for r in rows if r["accepted"] and r["tier"] == "local")
    closed = by_tactic + by_local
    walls = [r["wall_s"] for r in rows]
    ordered_walls = sorted(walls)
    p95 = (ordered_walls[max(0, int(len(ordered_walls) * 0.95 + 0.999999) - 1)]
           if ordered_walls else 0.0)
    return {
        "goals": n,
        "closed": closed,
        "close_rate": round(100 * closed / n, 1) if n else 0.0,
        "by_tactic": by_tactic,
        "by_local_prover": by_local,
        "open": n - closed,
        "wall_s_median": round(_median(walls), 1),
        "wall_s_p95": round(p95, 1),
        "wall_s_total": round(sum(walls), 1),
    }
