"""Goal discovery: find proof obligations (files containing `sorry`) in the
target Lean project, or read an explicit goal manifest.

v0.1 granularity is ONE GOAL PER FILE: a goal is a Lean module whose `sorry`(s)
the provers must close, keeping the kernel gate trivially correct (build the
module). Decomposition into per-theorem goals is the orchestrator/frontier
tier's job (it can split a file into lemma files).

Manifest format (TOML), for hand-curated goal lists / bottom-up ordering:

    [[goal]]
    module  = "Extracted.Bits"          # dotted module under the project root
    # optional:
    name    = "lor_eq_add_disjoint"     # display name (defaults to module)
    context = "Reuse SetSliceLemmas."   # extra prompt context

Order in the manifest is the proving order — list leaf modules first
(bottom-up the call graph, per the founding doc).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore

from .leantext import strip_comments
from .provers.base import Goal

_SORRY_RE = re.compile(r"\bsorry\b")
# directories never worth scanning inside a Lake project
_SKIP_DIRS = {".lake", ".git", "build", "lake-packages"}


@dataclass
class DiscoveredGoal:
    module_stem: str   # e.g. "Extracted.Bits"
    path: Path
    goal: Goal


def _module_stem(root: Path, path: Path) -> str:
    return ".".join(path.relative_to(root).with_suffix("").parts)




def scan(project_root: str | Path) -> list[DiscoveredGoal]:
    """Walk the project for .lean files whose CODE (not comments) contains
    `sorry`. Each becomes one goal."""
    root = Path(project_root).resolve()
    out: list[DiscoveredGoal] = []
    for path in sorted(root.rglob("*.lean")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not _SORRY_RE.search(strip_comments(text)):
            continue
        stem = _module_stem(root, path)
        out.append(DiscoveredGoal(
            module_stem=stem, path=path,
            goal=Goal(name=stem, file_text=text, target_module=stem),
        ))
    return out


def from_manifest(project_root: str | Path, manifest_path: str | Path) -> list[DiscoveredGoal]:
    """Read an explicit, ordered goal list (see module docstring)."""
    root = Path(project_root).resolve()
    data = tomllib.loads(Path(manifest_path).read_text())
    out: list[DiscoveredGoal] = []
    for entry in data.get("goal", []):
        stem = entry["module"]
        path = root / Path(*stem.split(".")).with_suffix(".lean")
        text = path.read_text(encoding="utf-8", errors="replace")
        out.append(DiscoveredGoal(
            module_stem=stem, path=path,
            goal=Goal(
                name=entry.get("name", stem),
                file_text=text,
                target_module=stem,
                context=entry.get("context", ""),
            ),
        ))
    return out
