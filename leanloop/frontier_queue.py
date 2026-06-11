"""Interactive-frontier work queue.

Cost lever: headless `claude -p` is METERED (Anthropic credit pool, post
2026-06-15), but an INTERACTIVE Claude Code session is subscription flat-rate.
So instead of the loop spending metered calls on hard goals, the `queue`
frontier backend writes each unsolved goal as a self-contained TASK that you
clear inside an interactive Claude Code session. The session writes a candidate
proof; `leanloop submit` re-verifies it through the SAME gates — so the
interactive path is exactly as sound as the automated one, just free.

A task is two files in the queue dir:
  <slug>.task.md     human/Claude-readable instructions + goal + criteria
  <slug>.task.json   machine metadata (goal name, module, project root, status)
The candidate proof is written by the session to <slug>.candidate.lean.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .lean_runner import theorem_signatures
from .provers.base import Goal


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


@dataclass
class Task:
    goal_name: str
    module_stem: str
    project_root: str
    status: str               # "pending" | "solved"
    slug: str
    queue_dir: str

    @property
    def candidate_path(self) -> Path:
        return Path(self.queue_dir) / f"{self.slug}.candidate.lean"

    @property
    def md_path(self) -> Path:
        return Path(self.queue_dir) / f"{self.slug}.task.md"

    @property
    def json_path(self) -> Path:
        return Path(self.queue_dir) / f"{self.slug}.task.json"


# --------------------------------------------------------------------------- #
def enqueue(goal: Goal, *, module_stem: str, project_root: str, queue_dir: str,
            config_path: str, allowed_axioms: list[str], whitelist: list[str],
            feedback: str = "") -> Task:
    """Write a frontier task for `goal` into `queue_dir`."""
    qd = Path(queue_dir)
    qd.mkdir(parents=True, exist_ok=True)
    slug = _slug(goal.name)
    sigs = theorem_signatures(goal.file_text)

    task = Task(goal_name=goal.name, module_stem=module_stem, project_root=project_root,
                status="pending", slug=slug, queue_dir=str(qd))
    task.json_path.write_text(json.dumps({
        "goal_name": goal.name, "module_stem": module_stem,
        "project_root": project_root, "status": "pending",
        "signatures": sigs,
    }, indent=2))

    cfg_arg = f"-c {config_path} " if config_path else ""
    crit_axioms = ", ".join(allowed_axioms + whitelist)
    sig_lines = "\n".join(f"  - `{fqn}` :: `{sig}`" for fqn, sig in sigs.items()) or "  (none found!)"
    ctx = f"\n## Best local attempt + Lean errors (context)\n\n```\n{feedback[:3000]}\n```\n" if feedback else ""

    task.md_path.write_text(f"""# LeanLoop frontier task — `{goal.name}`

**Status:** pending  ·  **Module:** `{module_stem}`  ·  **Project:** `{project_root}`

The local prover tier could not close this goal. Prove it here in this
interactive Claude Code session (subscription flat-rate — no metered API cost).

## Do this
1. Write a COMPLETE Lean 4 file that proves the goal below to:
   `{task.candidate_path}`
2. Verify it through LeanLoop's gates (this APPLIES it to the project if it passes):
   ```
   leanloop {cfg_arg}submit {goal.name}
   ```
3. If rejected, read the gate error, revise `{task.candidate_path}`, and submit again.

## Acceptance criteria (your proof MUST satisfy all)
- Prove EXACTLY these theorem(s) — same name, same signature, do **not** weaken/rename:
{sig_lines}
- No `sorry` / `admit`. No `axiom` declarations (not even whitelisted names).
  No `native_decide`.
- `#print axioms` of each theorem must reduce to: `{crit_axioms}` (nothing else;
  no `sorryAx`).
- Reuse Aeneas tactics (`progress`, `scalar_tac`, `step*`, `bvify`/`bv_tac`) and
  any lemmas already available in the project.

## Goal file — `{module_stem}.lean`
```lean4
{goal.file_text}```
{ctx}""")
    return task


# --------------------------------------------------------------------------- #
def _load(json_path: Path) -> Task:
    d = json.loads(json_path.read_text())
    return Task(goal_name=d["goal_name"], module_stem=d["module_stem"],
                project_root=d["project_root"], status=d.get("status", "pending"),
                slug=json_path.name[:-len(".task.json")], queue_dir=str(json_path.parent))


def list_tasks(queue_dir: str, *, pending_only: bool = False) -> list[Task]:
    qd = Path(queue_dir)
    if not qd.exists():
        return []
    tasks = [_load(p) for p in sorted(qd.glob("*.task.json"))]
    return [t for t in tasks if t.status == "pending"] if pending_only else tasks


def get_task(queue_dir: str, goal_name: str) -> Task | None:
    p = Path(queue_dir) / f"{_slug(goal_name)}.task.json"
    return _load(p) if p.exists() else None


def mark_solved(task: Task) -> None:
    d = json.loads(task.json_path.read_text())
    d["status"] = "solved"
    task.json_path.write_text(json.dumps(d, indent=2))


def task_goal(task: Task) -> Goal:
    """Reconstruct the Goal object from the project file the task points at."""
    path = Path(task.project_root) / Path(*task.module_stem.split(".")).with_suffix(".lean")
    return Goal(name=task.goal_name, file_text=path.read_text(), target_module=task.module_stem)
