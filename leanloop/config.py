"""LeanLoop configuration.

The whole point of this module is the PLUGGABLE prover backend: the local
Goedel prover is reached over an HTTP endpoint, so "run everything on one
machine" and "run the prover on a second box (e.g. a 6950 XT)" differ ONLY by
the `base_url` in `[prover.local]`. Nothing else in the loop changes.
"""
from __future__ import annotations

import os
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # py311+
    import tomllib
except ModuleNotFoundError:  # py310
    import tomli as tomllib  # type: ignore


# --------------------------------------------------------------------------- #
# Lean project under verification
# --------------------------------------------------------------------------- #
@dataclass
class ProjectConfig:
    """The Aeneas-generated Lean project we prove goals in."""
    # Path to the Lake project root (contains lakefile.lean / lean-toolchain).
    root: str = "."
    # `lake` binary (elan-managed by default).
    lake: str = "lake"
    # Build target whose theorems must stay sorry-free (the "shipped" library).
    default_target: str = ""
    # Extra args passed to `lake build` (e.g. parallelism).
    build_args: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Prover backends
# --------------------------------------------------------------------------- #
@dataclass
class LocalProverConfig:
    """The local prover (Goedel-Prover-V2-8B by default), served over an
    OpenAI-/Ollama-compatible HTTP API.

    SINGLE-MACHINE:   base_url = http://localhost:11434   (Ollama on this box)
    REMOTE INFERENCE: base_url = http://<gpu-box-ip>:11434 (the 6950 XT box)

    The backend kind selects the wire format:
      - "ollama"  -> POST /api/generate   (native Ollama)
      - "openai"  -> POST /v1/chat/completions (llama.cpp server, vLLM, etc.)
    """
    enabled: bool = True
    backend: str = "ollama"                  # "ollama" | "openai"
    base_url: str = "http://localhost:11434"
    model: str = "goedel-prover-v2-8b"       # Ollama model tag (see docs)
    api_key: str = ""                        # for openai-compatible servers; usually unused
    # pass@N sampling budget per goal (whole-proof attempts).
    samples: int = 32
    # temperatures cycled across the N samples (decorrelates failures).
    temperatures: list[float] = field(default_factory=lambda: [0.7, 1.0, 1.2])
    top_p: float = 0.95
    max_tokens: int = 32768                  # Goedel generates plan + proof
    # context window requested from the server (Ollama default is tiny; Goedel
    # wants room for the file + plan + proof). Maps to Ollama `num_ctx`.
    num_ctx: int = 24576
    # Concurrent in-flight samples. IMPORTANT: with Ollama, the server's context
    # is SHARED across OLLAMA_NUM_PARALLEL slots, so effective per-request context
    # ≈ num_ctx / parallelism. On a 16 GB card running Goedel-8B Q8_0 (~8.7 GB),
    # keep concurrency low so each slot still has room for a long proof:
    #   concurrency=2, num_ctx=24576  -> ~12k tokens/slot, ~6 GB KV  (safe default)
    # Raise only if the prover host has more VRAM or you lower num_ctx.
    # The setup script sets OLLAMA_NUM_PARALLEL to match this.
    concurrency: int = 2
    # per-sample generation timeout (seconds); generous for partial-offload boxes.
    timeout_s: float = 1200.0
    # verifier-guided self-correction rounds (feed Lean errors back).
    self_correct_rounds: int = 2
    # crash-resilience: if the prover endpoint goes down mid-run (Ollama OOM /
    # restart / the GPU box rebooting), wait+retry up to this many seconds for
    # it to come back before giving up on the local tier for that goal. 0 = no
    # wait (skip the local tier immediately if unreachable).
    health_max_wait_s: float = 600.0


@dataclass
class FrontierProverConfig:
    """The frontier hands-off tier: spec authoring, goal decomposition,
    auxiliary-lemma generation, and error-class-routed repair. Reached via the
    `claude` CLI (subscription) by default so no separate API bill is needed.

    THREE cost modes via `backend`:
      - "queue"      : enqueue hard goals for an INTERACTIVE Claude Code session
                       to clear (subscription FLAT-RATE — cheapest). The session
                       writes proofs; `leanloop submit` re-verifies them through
                       the same gates. Not unattended, but free. (Recommended if
                       you have a Claude subscription.)
      - "claude_cli" : headless `claude -p` (METERED credit pool post 2026-06-15;
                       flat-rate before then). Unattended but costs credits.
      - "openai"     : an OpenAI-compatible frontier endpoint (pay per token).
      - "none"       : disable the frontier tier entirely (local-only).
    Keep any metered tier SPARSE (the transcript's ~5-10% of calls).
    """
    enabled: bool = True
    backend: str = "claude_cli"              # "queue" | "claude_cli" | "openai" | "none"
    command: str = "claude"                  # CLI binary
    model: str = ""                          # "" => CLI default
    # openai-compatible fallback (e.g. a rented frontier endpoint)
    base_url: str = ""
    api_key: str = ""
    max_turns: int = 30
    # escalate to frontier only after the local tier exhausts its budget this
    # many times on a goal (transcript: 2-3 before flagging for human review).
    escalate_after: int = 1


@dataclass
class ProverStackConfig:
    local: LocalProverConfig = field(default_factory=LocalProverConfig)
    # Optional ENSEMBLE members (prover diversity). Each is a full
    # LocalProverConfig — typically a different model on a different endpoint
    # (e.g. DeepSeek-Prover-V2-7B, Kimina-Prover-RL). When non-empty, the loop
    # runs `[local] + ensemble` as one EnsembleProver: their proposals are
    # pooled per goal (the union closes more goals than any one model). TOML:
    # `[[prover.ensemble]]` tables, one per extra model. Empty = single-model.
    ensemble: list[LocalProverConfig] = field(default_factory=list)
    frontier: FrontierProverConfig = field(default_factory=FrontierProverConfig)
    # hard wall-clock budget per GOAL across tiers 0+1 (seconds; 0 = unlimited).
    # Without it one pathological goal (slow lake builds x 11 tactics x pass@N)
    # can silently consume an entire overnight run while `status` reads ALIVE.
    # On expiry the goal is handed to the frontier tier (queued if backend=queue).
    goal_timeout_s: float = 3600.0
    # tier-0 non-LLM tactic battery cascade (run before any model call).
    # Founding-doc cascade (cheap/decisive first). Each becomes `:= by <tac>`.
    # `step*`/`progress` are Aeneas-idiomatic openers for monadic goals.
    tactic_battery: list[str] = field(default_factory=lambda: [
        "scalar_tac",
        "omega",
        "simp_all",
        "(progress; scalar_tac)",
        "(step* <;> scalar_tac)",
        "(step* <;> simp_all)",
        "grind",
        "bv_decide",
        "decide",
        "exact?",
        # "hammer",   # enable once LeanHammer is installed in the project
    ])


# --------------------------------------------------------------------------- #
# Audit gate (the soundness boundary)
# --------------------------------------------------------------------------- #
@dataclass
class AuditConfig:
    """A proof is only ACCEPTED if it passes every gate here. The Lean kernel
    re-checks all proofs; these gates additionally forbid the known ways a
    machine proof can type-check while proving nothing."""
    forbid_sorry: bool = True
    forbid_new_axioms: bool = True           # only the whitelisted ones may appear
    forbid_native_decide: bool = True        # larger TCB than the kernel
    # `#print axioms <thm>` must reduce to exactly these (+ the whitelist).
    allowed_kernel_axioms: list[str] = field(default_factory=lambda: [
        "propext", "Classical.choice", "Quot.sound",
    ])
    # project-reviewed hardware / external axioms permitted in the closure.
    axiom_whitelist: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Spec-assurance: KAT anchoring + mutation
# --------------------------------------------------------------------------- #
@dataclass
class KatConfig:
    """Anchor an executable Lean spec to official test vectors (`leanloop kat`).
    The project exposes its spec as an adapter `List UInt8 -> List UInt8`."""
    module: str = ""                 # Lean module exposing the spec adapter
    adapter: str = ""                # Lean term of type `List UInt8 -> List UInt8`
    vectors: str = ""               # path to the KAT file (relative to cwd or abs)
    format: str = "auto"             # "auto" | "jsonl" | "hex" | "rsp"
    input_key: str = "input"         # jsonl/rsp field names
    expected_key: str = "expected"


@dataclass
class MutateConfig:
    """Spec-strength via Lean-definition mutation (`leanloop mutate`)."""
    # Lean definition files to mutate (relative to project.root). Empty = all
    # files under project.root NOT containing theorems (best-effort).
    target_files: list[str] = field(default_factory=list)
    # module to `lake build` to detect a broken proof (default: project default_target)
    build_target: str = ""
    max_per_file: int = 40           # cap mutants per file
    sample: int = 0                  # 0 = all; else random-free first-N (deterministic)


# --------------------------------------------------------------------------- #
# Top-level
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    project: ProjectConfig = field(default_factory=ProjectConfig)
    prover: ProverStackConfig = field(default_factory=ProverStackConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    # sqlite run log (the expert-iteration corpus + triage trail).
    db_path: str = "leanloop_runs.sqlite"
    # where the `queue` frontier backend drops tasks for an interactive session.
    frontier_queue_dir: str = "leanloop_frontier_queue"
    kat: KatConfig = field(default_factory=KatConfig)
    mutate: MutateConfig = field(default_factory=MutateConfig)

    # ------------------------------------------------------------------ #
    @staticmethod
    def load(path: str | os.PathLike[str] | None) -> "Config":
        cfg = Config()
        if path is None:
            return cfg._with_env()
        data = tomllib.loads(Path(path).read_text())
        cfg = _from_dict(Config, data)
        return cfg._with_env()

    def _with_env(self) -> "Config":
        """Env overrides for the deployment-specific knobs (CI / secrets)."""
        url = os.environ.get("LEANLOOP_PROVER_URL")
        if url:
            self.prover.local.base_url = url
        model = os.environ.get("LEANLOOP_PROVER_MODEL")
        if model:
            self.prover.local.model = model
        if os.environ.get("LEANLOOP_FRONTIER_DISABLE") == "1":
            self.prover.frontier.enabled = False
        return self


# --------------------------------------------------------------------------- #
# minimal nested-dataclass loader (no external deps)
# --------------------------------------------------------------------------- #
def _from_dict(cls: type, data: dict[str, Any]):
    if not hasattr(cls, "__dataclass_fields__"):
        return data
    kwargs: dict[str, Any] = {}
    # NB: `from __future__ import annotations` makes `f.type` a STRING; resolve
    # real classes via get_type_hints so nested TOML tables become dataclasses.
    hints = typing.get_type_hints(cls)
    for key, val in data.items():
        if key not in hints:
            continue
        ftype = hints[key]
        if isinstance(val, dict) and hasattr(ftype, "__dataclass_fields__"):
            kwargs[key] = _from_dict(ftype, val)
        elif isinstance(val, list) and typing.get_origin(ftype) is list:
            # list[SomeDataclass] (e.g. prover.ensemble): convert each element.
            (elem_t,) = typing.get_args(ftype) or (None,)
            if elem_t is not None and hasattr(elem_t, "__dataclass_fields__"):
                kwargs[key] = [_from_dict(elem_t, v) if isinstance(v, dict) else v
                               for v in val]
            else:
                kwargs[key] = val
        else:
            kwargs[key] = val
    return cls(**kwargs)
