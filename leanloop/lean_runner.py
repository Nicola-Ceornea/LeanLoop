"""The kernel gate: write a candidate proof into the Lean project, build it,
and read back its axiom closure. This is the trust boundary — a candidate is
only ACCEPTED if the Lean kernel type-checks it AND the audit gate passes.

Build + axiom-check happen in ONE write/restore window (no intermediate restore)
so the freshly-built `.olean` always matches the source the axiom checker reads.
The original file is restored from an on-disk backup in `finally`, and if the
process is killed mid-run the backup is left next to the file for recovery.

A Kimina-Lean-Server backend can replace this direct-`lake` runner later for
throughput; the interface (`verify`) stays the same.
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import ProjectConfig
from .leantext import strip_comments

# theorem/lemma signature: capture name and everything up to the `:=`.
_THEOREM_RE = re.compile(
    r"^[ \t]*(?:@\[[^\]]*\][ \t]*)*(?:theorem|lemma)[ \t]+([A-Za-z0-9_'.]+)", re.M)
_NAMESPACE_RE = re.compile(r"^\s*namespace\s+([A-Za-z0-9_'.]+)")
_END_RE = re.compile(r"^\s*end(?:\s+([A-Za-z0-9_'.]+))?\s*$")


@dataclass
class VerifyResult:
    build_ok: bool = False
    errors: str = ""
    axioms: str = ""           # raw `#print axioms` output
    wall_clock_s: float = 0.0


class LeanRunner:
    def __init__(self, project: ProjectConfig):
        self.p = project
        self.root = Path(project.root).resolve()

    # ------------------------------------------------------------------ #
    def verify(self, candidate_file_text: str, theorem_fqns: list[str],
               *, module_stem: str) -> VerifyResult:
        """Write `candidate_file_text` as `<root>/<module_stem>.lean`, build it,
        and (if it builds and `theorem_fqns` is non-empty) run `#print axioms`
        for each FQN — all within a single write/restore window."""
        res = VerifyResult()
        rel = Path(*module_stem.split(".")).with_suffix(".lean")
        target = self.root / rel
        bak = target.with_suffix(".lean.leanloop-bak")
        # unique checker module name to avoid collisions across concurrent runs.
        chk_stem = f"LeanLoopAxiomCheck_{abs(hash(module_stem)) % 10_000_000}"
        chk_path = self.root / f"{chk_stem}.lean"
        t0 = time.time()
        existed = target.exists()
        try:
            if existed:
                bak.write_text(target.read_text())
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(candidate_file_text)

            build = subprocess.run(
                [self.p.lake, "build", module_stem, *self.p.build_args],
                cwd=self.root, capture_output=True, text=True, timeout=1800,
            )
            res.build_ok = build.returncode == 0
            if not res.build_ok:
                res.errors = (build.stdout + "\n" + build.stderr).strip()
                return res

            if theorem_fqns:
                checker = f"import {module_stem}\n" + "".join(
                    f"#print axioms {fqn}\n" for fqn in theorem_fqns)
                chk_path.write_text(checker)
                axr = subprocess.run(
                    [self.p.lake, "env", "lean", str(chk_path)],
                    cwd=self.root, capture_output=True, text=True, timeout=900,
                )
                res.axioms = (axr.stdout + "\n" + axr.stderr).strip()
        except subprocess.TimeoutExpired:
            res.errors = res.errors or "lake build/axiom-check timed out"
        except Exception as e:
            res.errors = res.errors or f"lean runner error: {e}"
        finally:
            if chk_path.exists():
                chk_path.unlink()
            # restore the original (or remove the temp file we created)
            if existed:
                if bak.exists():
                    target.write_text(bak.read_text())
                    bak.unlink()
            elif target.exists():
                target.unlink()
            res.wall_clock_s = time.time() - t0
        return res


# --------------------------------------------------------------------------- #
# Static extraction (used to PIN the goal's theorems — see loop._verify)
# --------------------------------------------------------------------------- #


def _iter_decls(file_text: str):
    """Yield (fqn, signature) for each theorem/lemma, tracking namespaces.
    `signature` is the normalized text from `theorem`/`lemma` up to the `:=`
    (binders + type), comment-stripped and whitespace-collapsed — enough to
    detect a swapped/weakened statement."""
    code = strip_comments(file_text)
    ns_stack: list[str] = []
    cursor = 0  # running char offset, so repeated head-lines slice correctly
    for line in code.splitlines(keepends=True):
        line_start = cursor
        cursor += len(line)
        stripped = line.rstrip("\n")
        m = _NAMESPACE_RE.match(stripped)
        if m:
            ns_stack.append(m.group(1)); continue
        m = _END_RE.match(stripped)
        if m:
            name = m.group(1)
            if name and ns_stack and ns_stack[-1] == name:
                ns_stack.pop()
            elif name and name in ns_stack:
                while ns_stack and ns_stack[-1] != name:
                    ns_stack.pop()
                if ns_stack:
                    ns_stack.pop()
            continue
        m = _THEOREM_RE.match(stripped)
        if m:
            prefix = ".".join(ns_stack)
            fqn = f"{prefix}.{m.group(1)}" if prefix else m.group(1)
            # signature = from this decl's `theorem`/`lemma` head to the next `:=`
            assign = code.find(":=", line_start)
            sig_src = code[line_start:assign] if assign > line_start else stripped
            sig = re.sub(r"\s+", " ", sig_src).strip()
            yield fqn, sig


def theorem_names(file_text: str) -> list[str]:
    """FULLY-QUALIFIED theorem/lemma names (namespace-aware) for the axiom gate."""
    return [fqn for fqn, _ in _iter_decls(file_text)]


def theorem_signatures(file_text: str) -> dict[str, str]:
    """Map FQN -> normalized signature (binders+type up to `:=`). Used to verify
    a candidate proves the SAME statement the goal asked for (not a weaker one)."""
    return {fqn: sig for fqn, sig in _iter_decls(file_text)}
