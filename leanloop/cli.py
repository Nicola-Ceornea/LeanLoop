"""LeanLoop CLI.

    leanloop check-prover [-c cfg]            ping the prover endpoint
    leanloop scan         [-c cfg]            list goal modules (files with sorry)
    leanloop prove MODULE [-c cfg] [--apply]  run the tier cascade on one module
    leanloop run          [-c cfg] [--apply] [--manifest goals.toml]
                                              run over all goals (the overnight loop)
    leanloop stats        [-c cfg]            run-log summary
    leanloop doctor       [-c cfg]            preflight health check (prover/GPU/lean/disk)
    leanloop status       [-c cfg] [--watch]  live status + liveness of the current run
    leanloop bench        [-c cfg] [--samples N] [--goals M]
                                              measure prover tok/s + local close-rate
    leanloop vet MODULE   [-c cfg] [--explain] spec-assurance probes + English review
    leanloop kat [MODULE] [-c cfg] [--vectors F]  run spec on official test vectors (byte-eq)
    leanloop mutate [FILES] [-c cfg] [--build-target M] [--sample N]
                                              measure spec strength (mutate defs, expect proofs to break)
    leanloop frontier     [-c cfg] [--next]   list (or print) interactive-frontier tasks
    leanloop submit GOAL  [-c cfg] [--candidate F]
                                              verify+apply an interactively-written proof
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import httpx
from rich.console import Console
from rich.table import Table

from . import goals as goals_mod
from .config import Config
from .loop import Orchestrator
from .provers.ollama import LocalProver

console = Console()


def _config_path(args) -> str | None:
    path = getattr(args, "config", None)
    if path is None and Path("leanloop.toml").exists():
        path = "leanloop.toml"
    return path


def _load(args) -> Config:
    return Config.load(_config_path(args))


# --------------------------------------------------------------------------- #
def cmd_check_prover(args) -> int:
    cfg = _load(args)
    lp = cfg.prover.local
    url = lp.base_url.rstrip("/")
    console.print(f"prover backend: [bold]{lp.backend}[/] at [bold]{url}[/] (model: {lp.model})")
    try:
        if lp.backend == "ollama":
            r = httpx.get(f"{url}/api/tags", timeout=10)
            r.raise_for_status()
            models = [m["name"] for m in r.json().get("models", [])]
            console.print(f"[green]✓ reachable[/] — models: {models}")
            if not any(lp.model in m for m in models):
                console.print(f"[yellow]⚠ model '{lp.model}' not found on the server — "
                              f"see scripts/setup_prover_host.sh[/]")
                return 1
        else:
            r = httpx.get(f"{url}/v1/models", timeout=10)
            r.raise_for_status()
            console.print(f"[green]✓ reachable[/] — {r.json()}")
        return 0
    except Exception as e:
        console.print(f"[red]✗ prover endpoint unreachable: {e}[/]")
        console.print("hint: single-machine -> is Ollama running? "
                      "remote -> is OLLAMA_HOST=0.0.0.0 set on the GPU box and "
                      "the firewall open? (see README)")
        return 1


def cmd_scan(args) -> int:
    cfg = _load(args)
    found = goals_mod.scan(cfg.project.root)
    table = Table(title=f"goals (files with `sorry`) under {cfg.project.root}")
    table.add_column("module"); table.add_column("path")
    for g in found:
        table.add_row(g.module_stem, str(g.path))
    console.print(table)
    console.print(f"{len(found)} goal module(s)")
    return 0


def cmd_prove(args) -> int:
    cfg = _load(args)
    root = Path(cfg.project.root).resolve()
    path = root / Path(*args.module.split(".")).with_suffix(".lean")
    if not path.exists():
        console.print(f"[red]no such module file: {path}[/]")
        return 2
    from .provers.base import Goal
    goal = Goal(name=args.module, file_text=path.read_text(), target_module=args.module,
                context=args.context or "")
    orch = Orchestrator(cfg, config_path=_config_path(args) or "")
    try:
        outcome = orch.prove(goal, module_stem=args.module)
    finally:
        orch.close()
    if outcome.accepted and args.apply and outcome.proof_text:
        path.write_text(outcome.proof_text)
        console.print(f"[green]applied proof to {path}[/] — review + commit it "
                      f"(the PR is the human checkpoint)")
    return 0 if outcome.accepted else 1


def cmd_run(args) -> int:
    cfg = _load(args)
    if args.manifest:
        found = goals_mod.from_manifest(cfg.project.root, args.manifest)
    else:
        found = goals_mod.scan(cfg.project.root)
    if not found:
        console.print("no goals found")
        return 0
    import os
    import socket
    orch = Orchestrator(cfg, config_path=_config_path(args) or "")
    closed = 0
    orch.db.run_begin(len(found), socket.gethostname(), os.getpid())
    try:
        for dg in found:
            orch.db.run_progress(dg.goal.name, closed, _queue_pending(cfg))
            outcome = orch.prove(dg.goal, module_stem=dg.module_stem)
            if outcome.accepted:
                closed += 1
                if args.apply and outcome.proof_text and \
                        dg.path.read_text() != outcome.proof_text:
                    dg.path.write_text(outcome.proof_text)
                    console.print(f"[green]applied -> {dg.path}[/]")
        orch.db.run_end(closed, _queue_pending(cfg))
    finally:
        console.print(f"\n[bold]{closed}/{len(found)} goals closed[/]")
        pend = _queue_pending(cfg)
        if pend:
            console.print(f"[magenta]{pend} goal(s) queued for the interactive frontier — "
                          f"run `leanloop frontier` in a Claude Code session[/]")
        console.print(orch.db.stats())
        orch.close()
    return 0


def _queue_pending(cfg) -> int:
    from . import frontier_queue
    return len(frontier_queue.list_tasks(cfg.frontier_queue_dir, pending_only=True))


def cmd_stats(args) -> int:
    cfg = _load(args)
    from .db import RunDB
    db = RunDB(cfg.db_path)
    console.print(db.stats())
    db.close()
    return 0


# --------------------------------------------------------------------------- #
def _ago(ts: float | None) -> str:
    import time
    if not ts:
        return "never"
    d = max(0, int(time.time() - ts))
    if d < 60:
        return f"{d}s ago"
    if d < 3600:
        return f"{d // 60}m{d % 60:02d}s ago"
    return f"{d // 3600}h{(d % 3600) // 60:02d}m ago"


def cmd_doctor(args) -> int:
    """Preflight health check — run before/after SSHing into the box."""
    import shutil
    import subprocess
    from pathlib import Path as _P

    cfg = _load(args)
    rows: list[tuple[bool, str]] = []      # (ok, message); ok=None => warn

    def ck(ok, msg):
        rows.append((ok, msg))

    # 1. project / lake
    root = _P(cfg.project.root)
    ck(root.exists(), f"project root exists: {root}")
    lakefile = root / "lakefile.toml"
    lakefile2 = root / "lakefile.lean"
    ck(lakefile.exists() or lakefile2.exists(),
       f"lakefile present in {root}")
    lake = shutil.which(cfg.project.lake)
    if lake:
        try:
            v = subprocess.run([cfg.project.lake, "--version"], capture_output=True,
                               text=True, timeout=20).stdout.strip().splitlines()[:1]
            ck(True, f"lake: {lake} ({v[0] if v else '?'})")
        except Exception as e:
            ck(False, f"lake found but `--version` failed: {e}")
    else:
        ck(False, f"lake binary '{cfg.project.lake}' not on PATH")

    # 2. prover
    lp = cfg.prover.local
    if lp.enabled:
        prover = LocalProver(lp)
        ok, msg = prover.health()
        ck(ok, f"prover ({lp.backend} @ {lp.base_url}): {msg}")
        for m in prover.running():
            vram = m.get("size_vram", 0)
            total = m.get("size", 0) or 1
            pct = round(100 * vram / total)
            on_gpu = pct >= 99
            ck(None if not on_gpu else True,
               f"loaded '{m.get('name')}': {pct}% on GPU "
               f"({'GPU-resident' if on_gpu else 'partial CPU offload — EXPECTED for a model bigger than VRAM (e.g. 32B on 16 GB); otherwise check ROCm/VRAM'})")
    else:
        ck(None, "local prover disabled in config")

    # 3. frontier
    fb = cfg.prover.frontier
    if fb.enabled and fb.backend == "claude_cli":
        ck(bool(shutil.which(fb.command)),
           f"frontier '{fb.command}' CLI on PATH (metered — see cost modes)")
    else:
        ck(True, f"frontier backend: {fb.backend}")

    # 4. disk
    for label, p in [("project", root), ("db", _P(cfg.db_path).resolve().parent)]:
        try:
            free = shutil.disk_usage(p).free / 1e9
            ck(free > 2.0, f"disk free ({label} {p}): {free:.1f} GB")
        except Exception as e:
            ck(None, f"disk check ({label}) failed: {e}")

    # 5. queue + last run
    pend = _queue_pending(cfg)
    ck(None if pend else True, f"frontier queue: {pend} pending")
    _print_run_liveness(cfg)

    # render
    hard_fail = False
    for ok, msg in rows:
        if ok is True:
            console.print(f"[green]  ✓[/] {msg}")
        elif ok is None:
            console.print(f"[yellow]  ⚠[/] {msg}")
        else:
            console.print(f"[red]  ✗[/] {msg}"); hard_fail = True
    console.print(f"\n[bold]{'✗ problems found' if hard_fail else '✓ all good'}[/]")
    return 1 if hard_fail else 0


def _print_run_liveness(cfg) -> None:
    import time
    from .db import RunDB
    db = RunDB(cfg.db_path)
    try:
        st = db.run_state()
        if not st:
            console.print("[dim]  · no `leanloop run` recorded yet[/]")
            return
        last = db.last_activity_ts() or st["last_beat_ts"]
        age = time.time() - (last or 0)
        if st["finished"]:
            console.print(f"[green]  ✓[/] last run FINISHED ({_ago(st['finished_ts'])}); "
                          f"{st['done']}/{st['total']} closed, host {st['host']}")
        elif age < 180:
            console.print(f"[green]  ✓[/] run ALIVE on {st['host']} (pid {st['pid']}): "
                          f"goal '{st['current_goal']}', {st['done']}/{st['total']} done, "
                          f"last activity {_ago(last)}")
        else:
            console.print(f"[red]  ✗[/] run looks STALLED/DEAD on {st['host']} "
                          f"(pid {st['pid']}): last activity {_ago(last)}, "
                          f"stuck on '{st['current_goal']}' ({st['done']}/{st['total']})")
    finally:
        db.close()


def cmd_status(args) -> int:
    """Live status of the current/last run (DB-backed; safe to read mid-run)."""
    import time
    from .db import RunDB

    def render():
        db = RunDB(cfg.db_path)
        try:
            st = db.run_state()
            console.rule(f"LeanLoop status — {time.strftime('%H:%M:%S')}")
            if not st:
                console.print("no run recorded yet — start one with `leanloop run`")
            else:
                last = db.last_activity_ts() or st["last_beat_ts"]
                age = time.time() - (last or 0)
                state = ("[green]FINISHED[/]" if st["finished"]
                         else "[green]ALIVE[/]" if age < 180
                         else "[red]STALLED/DEAD[/]")
                console.print(f"state: {state}   host: {st['host']}  pid: {st['pid']}")
                cur = st['current_goal'] or '—'
                since = f" (working on it {_ago(st['last_beat_ts']).replace(' ago','')})" \
                        if (cur != '—' and not st['finished']) else ""
                console.print(f"progress: [bold]{st['done']}/{st['total']}[/] closed, "
                              f"{st['queued']} queued   current: {cur}{since}")
                console.print(f"started {_ago(st['started_ts'])}, last activity {_ago(last)}")
            console.print(db.stats())
            console.print("\n[dim]recent attempts:[/]")
            for a in db.recent_attempts(8):
                mark = "[green]✓[/]" if a["accepted"] else ("[yellow]·[/]" if a["build_ok"] else "[red]✗[/]")
                err = (a["lean_errors"] or "").splitlines()[:1]
                console.print(f"  {mark} {_ago(a['ts']):>10}  {a['tier']:<8} {a['goal_name']:<28}"
                              f" {a['wall_clock_s']:.0f}s {(err[0][:40] if err else '')}")
        finally:
            db.close()

    cfg = _load(args)
    if not args.watch:
        render()
        return 0
    try:
        while True:
            console.clear()
            render()
            console.print(f"\n[dim]watching (every {args.interval}s) — Ctrl-C to stop[/]")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_kat(args) -> int:
    """Run the project's executable Lean spec over official test vectors and
    require byte-equality — grounds the spec in external truth (the standard)."""
    import subprocess
    from . import kat
    from .lean_runner import LeanRunner

    cfg = _load(args)
    k = cfg.kat
    vectors_path = args.vectors or k.vectors
    module = args.module or k.module
    adapter = k.adapter
    if not (module and adapter and vectors_path):
        console.print("[red]kat needs [kat] module + adapter + vectors in config[/] "
                      "(or --module/--vectors). The project must expose a Lean adapter "
                      "`List UInt8 -> List UInt8` over its spec.")
        return 2
    vp = Path(vectors_path)
    if not vp.exists():
        console.print(f"[red]vectors file not found: {vp}[/]")
        return 2

    vectors = kat.parse_vectors(vp.read_text(), k.format,
                                input_key=k.input_key, expected_key=k.expected_key)
    problems = kat.validate(vectors)
    if not vectors or problems:
        console.print(f"[red]vector parse problems[/]: {problems[:5] or 'no vectors parsed'}")
        return 2
    console.rule(f"KAT — {module} over {len(vectors)} vectors")

    runner = LeanRunner(cfg.project)
    # rebuild the spec module first — `lake env lean --run` imports its .olean,
    # which would otherwise be STALE if the spec source changed since last build
    # (a stale olean silently tests the OLD spec — a real correctness trap).
    bld = subprocess.run([cfg.project.lake, "build", module], cwd=runner.root,
                         capture_output=True, text=True, timeout=1800)
    if bld.returncode != 0:
        console.print(f"[red]✗ spec module {module} does not build — fix it first[/]\n"
                      f"{(bld.stdout + bld.stderr)[-600:]}")
        return 2
    harness = kat.build_harness(module, adapter)
    hpath = runner.root / f"LeanLoopKat_{abs(hash(module)) % 10_000_000}.lean"
    tsv = runner.root / f"LeanLoopKat_{abs(hash(module)) % 10_000_000}.tsv"
    try:
        hpath.write_text(harness)
        tsv.write_text(kat.vectors_to_tsv(vectors))
        res = subprocess.run([cfg.project.lake, "env", "lean", "--run", str(hpath), str(tsv)],
                             cwd=runner.root, capture_output=True, text=True, timeout=1800)
        out = (res.stdout + "\n" + res.stderr).strip()
    finally:
        for p in (hpath, tsv):
            if p.exists():
                p.unlink()

    kr = kat.parse_harness_output(out)
    if not kr.ran:
        console.print(f"[red]✗ harness did not run — check the adapter type "
                      f"(`{adapter}` must be `List UInt8 -> List UInt8`)[/]\n{out[-800:]}")
        return 1
    if kr.failures:
        console.print(f"[red]✗ {kr.passed}/{kr.total} vectors passed — SPEC DOES NOT "
                      f"MATCH THE STANDARD:[/]")
        for name, why in kr.failures[:10]:
            console.print(f"  [red]FAIL[/] {name}: {why}")
        return 1
    console.print(f"[green]✓ {kr.passed}/{kr.total} vectors passed — the spec reproduces "
                  f"the official vectors byte-for-byte.[/]")
    console.print("[dim]grounds functional correctness on the exercised paths; does NOT "
                  "cover security properties, unexercised paths, or the abstraction "
                  "function (human review).[/]")
    return 0


def cmd_mutate(args) -> int:
    """Measure spec STRENGTH: mutate the extracted Lean definitions and check
    that the pinned proofs catch each injected bug (mCoq proof-break scoring)."""
    from . import mutate as M
    from .lean_runner import LeanRunner

    cfg = _load(args)
    runner = LeanRunner(cfg.project)
    target = args.build_target or cfg.mutate.build_target or cfg.project.default_target
    if not target:
        console.print("[red]mutate needs a build target[/] (mutate.build_target / "
                      "project.default_target / --build-target) — the module whose proofs "
                      "must catch the bug.")
        return 2

    # which files to mutate
    files = list(args.files) if args.files else cfg.mutate.target_files
    if not files:
        console.print("[red]no target files[/] — pass files or set mutate.target_files "
                      "(the extracted DEFINITION files, e.g. Extracted/Parser/Funs.lean).")
        return 2

    # 0) baseline: the target must build clean before we start
    import subprocess
    console.rule("mutate — baseline build")
    b = subprocess.run([cfg.project.lake, "build", target], cwd=runner.root,
                       capture_output=True, text=True, timeout=1800)
    if b.returncode != 0:
        console.print(f"[red]baseline build of {target} FAILS — fix that first[/]\n"
                      f"{(b.stdout + b.stderr)[-600:]}")
        return 2
    console.print(f"[green]baseline {target} builds clean[/]")

    # 1) generate mutants
    all_mutants: list[M.Mutant] = []
    for rel in files:
        fp = runner.root / rel
        if not fp.exists():
            console.print(f"[yellow]skip missing file: {rel}[/]")
            continue
        all_mutants += M.generate_mutants(rel, fp.read_text(),
                                          max_per_file=cfg.mutate.max_per_file)
    if cfg.mutate.sample and len(all_mutants) > cfg.mutate.sample:
        all_mutants = all_mutants[: cfg.mutate.sample]
    if args.sample and len(all_mutants) > args.sample:
        all_mutants = all_mutants[: args.sample]
    console.print(f"generated {len(all_mutants)} mutant(s) across {len(files)} file(s)")

    # 2) apply -> rebuild -> judge -> revert
    results: list[M.MutantResult] = []
    for i, m in enumerate(all_mutants, 1):
        fp = runner.root / m.file
        orig = fp.read_text()
        try:
            mutated = M.apply_mutant(orig, m)
            if mutated == orig:
                results.append(M.MutantResult(m, "error", "could not apply"))
                continue
            fp.write_text(mutated)
            r = subprocess.run([cfg.project.lake, "build", target], cwd=runner.root,
                               capture_output=True, text=True, timeout=1800)
            out = (r.stdout + r.stderr)
            if r.returncode != 0:
                # did the DEFINITION fail to elaborate (unviable) or a PROOF break (killed)?
                unviable = bool(re.search(r"(unsolved goals|type mismatch|unknown identifier"
                                          r"|failed to synth)", out)) and "sorry" not in out
                # heuristic: a proof break usually cites a theorem; treat any build
                # failure as killed unless the mutated file itself failed to elaborate
                outcome = "unviable" if (m.file in out and "error" in out and
                                         "linter" not in out and "_vet" not in out
                                         and _looks_unviable(out, m.file)) else "killed"
                results.append(M.MutantResult(m, outcome, out[-400:]))
            else:
                results.append(M.MutantResult(m, "lived"))
        except subprocess.TimeoutExpired:
            results.append(M.MutantResult(m, "timeout"))
        finally:
            fp.write_text(orig)
        mark = {"killed": "[green]✓killed[/]", "lived": "[red]✗LIVED[/]",
                "unviable": "[dim]unviable[/]", "timeout": "[yellow]timeout[/]",
                "error": "[dim]error[/]"}[results[-1].outcome]
        console.print(f"  [{i}/{len(all_mutants)}] {mark} {m.name}")

    # 3) score + report survivors
    sc = M.score(results)
    console.rule("mutate — spec strength")
    score_str = f"{sc['mutation_score']}%" if sc["mutation_score"] is not None else "n/a"
    console.print(f"[bold]mutation score: {score_str}[/]  "
                  f"(killed {sc['killed']}, [red]lived {sc['lived']}[/], "
                  f"unviable {sc['unviable']}, timeout {sc['timeout']})")
    if sc["survivors"]:
        console.print("\n[red]SURVIVORS — bugs no proof caught (spec gaps):[/]")
        for s in sc["survivors"]:
            console.print(s)
        console.print("\n[dim]~85% of survivors are real spec gaps (mCoq); the rest are "
                      "equivalent mutants. Each is a place your spec under-constrains.[/]")
    return 0 if sc["lived"] == 0 else 1


def _looks_unviable(build_out: str, mutated_file: str) -> bool:
    """The build failed inside the MUTATED definition file itself (mutant won't
    elaborate) rather than in a downstream proof — exclude from the score."""
    # errors are reported as `path:line:col: error`; if the only errored file is
    # the mutated one, the mutant is unviable, not killed.
    errored = set(re.findall(r"([\w./-]+\.lean):\d+:\d+: error", build_out))
    return errored == {mutated_file} or (mutated_file in errored and len(errored) == 1)


def cmd_necessity(args) -> int:
    """Dead-premise check: for each PROVEN theorem in `module`, drop each leaf
    hypothesis and rebuild — a proof that still builds never used it (V4 dead
    conjunct). The non-circular sibling of `mutate` (which perturbs definitions);
    orthogonal to `vet` HYP (which checks satisfiability, not consumption)."""
    from . import premise as P
    from .lean_runner import LeanRunner
    from .leantext import strip_comments

    cfg = _load(args)
    runner = LeanRunner(cfg.project)
    rel = Path(*args.module.split(".")).with_suffix(".lean")
    target = runner.root / rel
    if not target.exists():
        console.print(f"[red]module not found:[/] {target}")
        return 2
    src = target.read_text()
    if re.search(r"\bsorry\b", strip_comments(src)):
        console.print(f"[red]{args.module} still contains `sorry`[/] — necessity needs a "
                      "PROVEN module (it tests whether the proof uses each hypothesis).")
        return 2

    # baseline: the module must build clean before we perturb it.
    console.rule("necessity — baseline build")
    base = runner.verify(src, [], module_stem=args.module)
    if not base.build_ok:
        console.print(f"[red]baseline build of {args.module} FAILS — fix that first[/]\n"
                      f"{base.errors[-600:]}")
        return 2
    console.print(f"[green]baseline {args.module} builds clean[/]")

    variants = P.build_variants(src)
    if args.sample and len(variants) > args.sample:
        variants = variants[: args.sample]
    if not variants:
        console.print("[dim]no leaf-hypothesis binders to drop — every binder is a "
                      "parameter the theorems are about (nothing to test).[/]")
        return 0
    console.print(f"checking {len(variants)} declared hypothesis/-es for actual use")

    results: list[P.NecessityResult] = []
    for i, v in enumerate(variants, 1):
        names = []
        for b in P.split_binder_groups(v.dropped.strip("()")):
            names += b.names
        names = names or [v.dropped]
        r = runner.verify(v.code, [], module_stem=args.module)
        outcome = P.classify(r.build_ok, r.errors, names)
        results.append(P.NecessityResult(v, outcome, r.errors[-300:]))
        mark = {"killed": "[green]✓used[/]", "lived": "[red]✗DEAD[/]",
                "unviable": "[dim]unviable[/]", "timeout": "[yellow]timeout[/]",
                "error": "[dim]error[/]"}[outcome]
        console.print(f"  [{i}/{len(variants)}] {mark} {v.theorem_fqn}: drop {v.dropped}")

    sc = P.score(results)
    console.rule("necessity — premise load-bearingness")
    s = f"{sc['necessity_score']}%" if sc["necessity_score"] is not None else "n/a"
    console.print(f"[bold]necessity score: {s}[/]  (used {sc['load_bearing']}, "
                  f"[red]dead {sc['dead_premises']}[/], unviable {sc['unviable']}, "
                  f"timeout {sc['timeout']})")
    if sc["dead"]:
        console.print("\n[red]DEAD PREMISES — hypotheses no proof consumed:[/]")
        for d in sc["dead"]:
            console.print(f"  {d}")
        console.print("\n[dim]A dead premise means the theorem holds WITHOUT that "
                      "assumption (it is stronger than its signature implies), or the "
                      "assumption is decoration that misleads a reader. Remove it or use it.[/]")
    return 0 if sc["dead_premises"] == 0 else 1


def cmd_bench(args) -> int:
    """Phase-0 benchmark: prover tokens/sec on this GPU + local-tier close-rate."""
    import time
    from . import bench as B

    cfg = _load(args)
    lp = cfg.prover.local
    if not lp.enabled:
        console.print("[red]local prover disabled in config — nothing to benchmark[/]")
        return 2
    prover = LocalProver(lp)
    ok, msg = prover.health()
    if not ok:
        console.print(f"[red]prover not ready: {msg}[/] (run `leanloop doctor`)")
        return 1

    # ---- Phase A: raw throughput (no project needed) ----
    console.rule("throughput probe")
    console.print(f"model [bold]{lp.model}[/] @ {lp.base_url} — {args.samples} samples "
                  f"(warming up first)…")
    try:
        prover.generate_timed(B.THROUGHPUT_PROMPT, temperature=0.7)  # warm-up (load model)
    except Exception as e:
        console.print(f"[red]generation failed: {e}[/]")
        return 1
    samples = []
    for i in range(args.samples):
        s = prover.generate_timed(B.THROUGHPUT_PROMPT, temperature=lp.temperatures[0])
        samples.append(s)
        console.print(f"  sample {i+1}: {s['gen_tps']:.1f} tok/s gen "
                      f"({s['gen_toks']} toks, {s['total_s']:.1f}s)")
    tp = B.summarize_throughput(samples)
    console.print(f"\n[bold]generation: {tp['gen_tps_median']} tok/s median[/] "
                  f"(mean {tp['gen_tps_mean']}, range {tp['gen_tps_min']}–{tp['gen_tps_max']}); "
                  f"prompt {tp['prompt_tps_median']} tok/s; ~{tp['gen_toks_median']} toks/proof")
    for m in prover.running():
        vram, total = m.get("size_vram", 0), m.get("size", 0) or 1
        pct = round(100 * vram / total)
        tag = "GPU-resident" if pct >= 99 else "[red]PARTIAL CPU — speed is capped[/]"
        console.print(f"residency: '{m.get('name')}' {pct}% on GPU ({tag})")
    if tp["gen_tps_median"] > 0:
        per = tp["gen_toks_median"] / tp["gen_tps_median"]
        console.print(f"[dim]≈ {per:.0f}s per sample → pass@{lp.samples} ≈ "
                      f"{per * lp.samples / max(lp.concurrency, 1) / 60:.0f} min/goal "
                      f"at concurrency {lp.concurrency}[/]")

    # ---- Phase B: close-rate over real goals (needs a project with sorries) ----
    if args.goals and args.goals > 0:
        found = goals_mod.scan(cfg.project.root)[: args.goals]
        if not found:
            console.print("\n[yellow]no goals (sorries) in the project — skipping close-rate. "
                          "Point project.root at an Aeneas Lean project to measure it.[/]")
        else:
            console.rule(f"close-rate (local tier only, {len(found)} goals)")
            cfg.prover.frontier.enabled = False   # measure the LOCAL stack's yield
            import tempfile
            cfg.db_path = str(Path(tempfile.mkdtemp(prefix="leanloop-bench-")) / "bench.sqlite")
            console.print(f"[dim]bench uses an ephemeral DB ({cfg.db_path}) — "
                          f"probe results never pollute the production solved-cache[/]")
            orch = Orchestrator(cfg, config_path=_config_path(args) or "")
            rows = []
            try:
                for dg in found:
                    t0 = time.time()
                    out = orch.prove(dg.goal, module_stem=dg.module_stem)
                    rows.append({"tier": out.tier, "accepted": out.accepted,
                                 "wall_s": time.time() - t0})
                    mark = "✓" if out.accepted else "✗"
                    console.print(f"  {mark} {dg.module_stem} [{out.tier or 'open'}] "
                                  f"{rows[-1]['wall_s']:.0f}s")
            finally:
                orch.close()
            cr = B.summarize_closerate(rows)
            console.print(f"\n[bold]close-rate: {cr['close_rate']}%[/] "
                          f"({cr['closed']}/{cr['goals']}) — "
                          f"{cr['by_tactic']} by tactic battery, "
                          f"{cr['by_local_prover']} by the prover, {cr['open']} open; "
                          f"median {cr['wall_s_median']}s/goal, {cr['wall_s_total']}s total")
    else:
        console.print("\n[dim]close-rate skipped (pass --goals N to measure it on the "
                      "project's sorries).[/]")
    return 0


def cmd_vet(args) -> int:
    """Spec-assurance probes: mechanical signals about whether a spec says
    anything (counterexample search, hypothesis satisfiability, negation
    proving) + an LLM explain handoff for the plain-English review."""
    from . import spec_vet
    from .lean_runner import LeanRunner, theorem_signatures

    cfg = _load(args)
    root = Path(cfg.project.root).resolve()
    path = root / Path(*args.module.split(".")).with_suffix(".lean")
    if not path.exists():
        console.print(f"[red]no such module file: {path}[/]")
        return 2
    goal_text = path.read_text()
    sigs = theorem_signatures(goal_text)
    if not sigs:
        console.print("[yellow]no theorem/lemma declarations found — nothing to vet[/]")
        return 1

    runner = LeanRunner(cfg.project)
    console.rule(f"spec vet — {args.module} ({len(sigs)} theorem(s))")
    probes = spec_vet.build_probes(goal_text, sigs)
    verdicts = []
    worst = "green"
    rank = {"green": 0, "skip": 1, "yellow": 2, "red": 3}
    for p in probes:
        console.print(f"[dim]probe {p.kind}:{p.theorem_fqn}…[/]")
        out = runner.run_scratch_file(p.file_text, tag=f"Vet{p.kind.title()}")
        v = spec_vet.judge(p, out)
        verdicts.append(v)
        color = {"red": "red", "yellow": "yellow", "green": "green", "skip": "dim"}[v.level]
        console.print(f"  [{color}]{v.level.upper():6}[/] [{v.kind}] {v.message}")
        if v.detail and v.level in ("red", "yellow"):
            console.print(f"  [dim]{v.detail[:300]}[/]")
        if rank[v.level] > rank[worst]:
            worst = v.level

    console.print(f"\n[bold]overall: {worst.upper()}[/]"
                  + ("  — investigate before trusting this spec" if worst != "green" else
                     "  — mechanical probes raise no flags"))

    if args.explain:
        probes_summary = "\n".join(
            f"- {v.theorem_fqn} [{v.kind}]: {v.level.upper()} — {v.message}" for v in verdicts)
        prompt = spec_vet.EXPLAIN_PROMPT.format(probes=probes_summary, goal=goal_text)
        fb = cfg.prover.frontier
        if fb.enabled and fb.backend == "claude_cli":
            import subprocess as sp
            console.rule("LLM spec review (claude CLI — metered)")
            res = sp.run([fb.command, "-p", prompt], capture_output=True, text=True,
                         timeout=600)
            console.print(res.stdout or res.stderr)
        else:
            out_path = Path(cfg.frontier_queue_dir)
            out_path.mkdir(parents=True, exist_ok=True)
            f = out_path / f"{args.module.replace('.', '_')}.explain.md"
            header = (f"# Spec review request — `{args.module}`\n\n"
                      "Paste the following into an interactive Claude Code session "
                      "(subscription flat-rate) for the plain-English adversarial "
                      "spec review:\n\n---\n\n")
            f.write_text(header + prompt + "\n")
            console.print(f"\n[magenta]explain prompt written to {f}[/] — open it in "
                          f"an interactive Claude Code session (flat-rate) for the "
                          f"plain-English adversarial review.")
    return 0 if worst in ("green", "skip") else 1


def cmd_frontier(args) -> int:
    """List the interactive-frontier queue, or print the next task's prompt."""
    from . import frontier_queue
    cfg = _load(args)
    pending = frontier_queue.list_tasks(cfg.frontier_queue_dir, pending_only=True)
    if args.next:
        if not pending:
            console.print("[green]frontier queue empty — nothing to do[/]")
            return 0
        # print the raw markdown so an interactive Claude Code session can act on it
        console.print(pending[0].md_path.read_text())
        return 0
    if not pending:
        console.print("[green]frontier queue empty[/]")
        return 0
    table = Table(title=f"pending frontier tasks ({cfg.frontier_queue_dir})")
    table.add_column("goal"); table.add_column("module"); table.add_column("task file")
    for t in pending:
        table.add_row(t.goal_name, t.module_stem, str(t.md_path))
    console.print(table)
    console.print(f"\n{len(pending)} pending. In an interactive Claude Code session run "
                  f"`/leanloop-frontier`, or work them by hand:\n"
                  f"  leanloop frontier --next        # print the next task prompt\n"
                  f"  # write the proof to the task's .candidate.lean, then:\n"
                  f"  leanloop submit <goal>")
    return 0


def cmd_submit(args) -> int:
    """Verify an interactively-produced candidate through the gates; apply if it passes."""
    from . import frontier_queue
    cfg = _load(args)
    task = frontier_queue.get_task(cfg.frontier_queue_dir, args.goal)
    if task is None:
        console.print(f"[red]no queued task named '{args.goal}'[/] "
                      f"(see `leanloop frontier`)")
        return 2
    cand_path = Path(args.candidate) if args.candidate else task.candidate_path
    if not cand_path.exists():
        console.print(f"[red]candidate file not found: {cand_path}[/]\n"
                      f"write your proof there first (see `leanloop frontier --next`).")
        return 2
    candidate = cand_path.read_text()
    goal = frontier_queue.task_goal(task)
    orch = Orchestrator(cfg, config_path=_config_path(args) or "")
    try:
        att = orch.submit(goal, candidate, module_stem=task.module_stem)
        if att.accepted:
            target = Path(task.project_root) / Path(*task.module_stem.split(".")).with_suffix(".lean")
            target.write_text(candidate)
            frontier_queue.mark_solved(task)
            console.print(f"[green]✓ accepted[/] — applied to {target} and marked solved.\n"
                          f"  axioms: {att.axioms.splitlines()[0] if att.axioms else '(n/a)'}\n"
                          f"  review + commit the diff (the PR is the human checkpoint).")
            return 0
        console.print(f"[red]✗ rejected by the gate:[/]\n{att.lean_errors[:1500]}\n\n"
                      f"revise {cand_path} and `leanloop submit {args.goal}` again.")
        return 1
    finally:
        orch.close()


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    # `-c` is accepted both before and after the subcommand (a common footgun
    # with argparse subparsers) via a shared parent parser.
    # NB: -c is added to BOTH the top parser and each subparser so it works on
    # either side of the subcommand. default=SUPPRESS is ESSENTIAL: without it,
    # the subparser's `-c` default (None) would clobber a `-c` given BEFORE the
    # subcommand. With SUPPRESS, an absent -c simply doesn't set the attribute.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS,
                        help="path to leanloop.toml")

    p = argparse.ArgumentParser(prog="leanloop", description=__doc__,
                                parents=[common],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check-prover", parents=[common], help="ping the local-prover endpoint")
    sub.add_parser("scan", parents=[common], help="list goal modules (files containing sorry)")
    sp = sub.add_parser("prove", parents=[common], help="prove one module")
    sp.add_argument("module", help="dotted module, e.g. Extracted.Bits")
    sp.add_argument("--apply", action="store_true",
                    help="write the accepted proof back into the project")
    sp.add_argument("--context", default="", help="extra prompt context")
    sr = sub.add_parser("run", parents=[common], help="run the loop over all goals")
    sr.add_argument("--apply", action="store_true")
    sr.add_argument("--manifest", default=None, help="ordered goal manifest (TOML)")
    sub.add_parser("stats", parents=[common], help="run-log summary")
    sub.add_parser("doctor", parents=[common],
                   help="preflight health check (prover, GPU, lean, disk, run liveness)")
    st = sub.add_parser("status", parents=[common],
                        help="live status of the current/last run")
    st.add_argument("--watch", action="store_true", help="refresh continuously")
    st.add_argument("--interval", type=float, default=10.0, help="watch refresh seconds")
    sb = sub.add_parser("bench", parents=[common],
                        help="Phase-0 benchmark: prover tok/s + local close-rate")
    sb.add_argument("--samples", type=int, default=5, help="throughput probe samples")
    sb.add_argument("--goals", type=int, default=0,
                    help="also measure local close-rate over N project goals")
    sv = sub.add_parser("vet", parents=[common],
                        help="spec-assurance probes (counterexample/vacuity/negation) + LLM explain")
    sv.add_argument("module", help="dotted module whose theorems to vet")
    sv.add_argument("--explain", action="store_true",
                    help="also produce the plain-English adversarial spec review")
    sk = sub.add_parser("kat", parents=[common],
                        help="run the executable Lean spec over official test vectors (byte-equality)")
    sk.add_argument("module", nargs="?", default="", help="spec module (or [kat] module)")
    sk.add_argument("--vectors", default="", help="KAT vector file (or [kat] vectors)")
    sm = sub.add_parser("mutate", parents=[common],
                        help="measure spec STRENGTH by mutating Lean definitions (proof-break scoring)")
    sm.add_argument("files", nargs="*", help="Lean definition files to mutate (or mutate.target_files)")
    sm.add_argument("--build-target", default="", help="module to rebuild to detect a broken proof")
    sm.add_argument("--sample", type=int, default=0, help="cap total mutants (0 = all)")
    sn = sub.add_parser("necessity", parents=[common],
                        help="dead-premise check: drop each PROVEN theorem's leaf hypotheses, "
                             "expect the proof to break (mutate.py's non-circular sibling)")
    sn.add_argument("module", help="dotted module of PROVEN theorems to check (no sorry)")
    sn.add_argument("--sample", type=int, default=0, help="cap total dropped-hyp variants (0 = all)")
    sf = sub.add_parser("frontier", parents=[common],
                        help="list the interactive-frontier queue (backend=queue)")
    sf.add_argument("--next", action="store_true",
                    help="print the next pending task's prompt (for a Claude Code session)")
    ss = sub.add_parser("submit", parents=[common],
                        help="verify+apply an interactively-produced candidate proof")
    ss.add_argument("goal", help="queued goal name (see `leanloop frontier`)")
    ss.add_argument("--candidate", default=None,
                    help="path to the candidate .lean (default: the task's .candidate.lean)")

    args = p.parse_args(argv)
    return {
        "check-prover": cmd_check_prover,
        "scan": cmd_scan,
        "prove": cmd_prove,
        "run": cmd_run,
        "stats": cmd_stats,
        "doctor": cmd_doctor,
        "status": cmd_status,
        "bench": cmd_bench,
        "vet": cmd_vet,
        "kat": cmd_kat,
        "mutate": cmd_mutate,
        "necessity": cmd_necessity,
        "frontier": cmd_frontier,
        "submit": cmd_submit,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
