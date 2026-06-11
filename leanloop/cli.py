"""LeanLoop CLI.

    leanloop check-prover [-c cfg]            ping the prover endpoint
    leanloop scan         [-c cfg]            list goal modules (files with sorry)
    leanloop prove MODULE [-c cfg] [--apply]  run the tier cascade on one module
    leanloop run          [-c cfg] [--apply] [--manifest goals.toml]
                                              run over all goals (the overnight loop)
    leanloop stats        [-c cfg]            run-log summary
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx
from rich.console import Console
from rich.table import Table

from . import goals as goals_mod
from .config import Config
from .loop import Orchestrator

console = Console()


def _load(args) -> Config:
    path = args.config
    if path is None and Path("leanloop.toml").exists():
        path = "leanloop.toml"
    return Config.load(path)


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
    orch = Orchestrator(cfg)
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
    orch = Orchestrator(cfg)
    closed = 0
    try:
        for dg in found:
            outcome = orch.prove(dg.goal, module_stem=dg.module_stem)
            if outcome.accepted:
                closed += 1
                if args.apply and outcome.proof_text:
                    dg.path.write_text(outcome.proof_text)
                    console.print(f"[green]applied -> {dg.path}[/]")
    finally:
        console.print(f"\n[bold]{closed}/{len(found)} goals closed[/]")
        console.print(orch.db.stats())
        orch.close()
    return 0


def cmd_stats(args) -> int:
    cfg = _load(args)
    from .db import RunDB
    db = RunDB(cfg.db_path)
    console.print(db.stats())
    db.close()
    return 0


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    # `-c` is accepted both before and after the subcommand (a common footgun
    # with argparse subparsers) via a shared parent parser.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=None, help="path to leanloop.toml")

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

    args = p.parse_args(argv)
    return {
        "check-prover": cmd_check_prover,
        "scan": cmd_scan,
        "prove": cmd_prove,
        "run": cmd_run,
        "stats": cmd_stats,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
