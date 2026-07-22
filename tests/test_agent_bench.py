"""External-agent held-out trajectories stay isolated and proof-free."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from leanloop.agent_bench import (
    AgentTrajectoryResult,
    _require_masked_target_sorry_ax,
    _run_setup,
    agent_config_receipt,
    run_agent_trajectory,
)
from leanloop.config import AgentBenchmarkConfig, Config, ProjectConfig
from leanloop.heldout import CaseSpec, PreparedCase
from leanloop.lean_runner import VerifyResult
from leanloop.loop import Orchestrator
from leanloop.provers.base import Goal


MODULE = "Bench.Fixture"
THEOREM = "Bench.Fixture.target"
PLACEHOLDER = "by\n  set_option maxRecDepth 16384 in\n    sorry"
SCAFFOLD = f"""namespace Bench.Fixture
theorem keep : True := by trivial
theorem target : True := {PLACEHOLDER}
end Bench.Fixture
"""
GOLD = SCAFFOLD.replace(PLACEHOLDER, "by trivial")


class _DB:
    def __init__(self):
        self.logged = []

    def log(self, attempt, *, goal_hash=""):
        self.logged.append((attempt, goal_hash))


class _TrustedRunner:
    def __init__(self):
        self.calls = []

    def verify(self, candidate, theorem_fqns, *, module_stem):
        self.calls.append((candidate, theorem_fqns, module_stem))
        if "exact detached_helper" in candidate:
            return VerifyResult(build_ok=False, errors="unknown identifier detached_helper")
        return VerifyResult(
            build_ok=True,
            axioms="'Bench.Fixture.target' does not depend on any axioms",
        )


def _prepared(root: Path) -> PreparedCase:
    target = root / "Bench" / "Fixture.lean"
    start = SCAFFOLD.index(PLACEHOLDER)
    goal = Goal(
        name="fixture-target",
        file_text=SCAFFOLD,
        target_module=MODULE,
        target_fqns=[THEOREM],
        proof_hole=(start, start + len(PLACEHOLDER)),
        axiom_whitelist=[],
    )
    spec = CaseSpec(
        id="fixture-target",
        module=MODULE,
        theorem=THEOREM,
        source_sha256="1" * 64,
        proof_start=0,
        proof_end=1,
        proof_sha256="2" * 64,
        category="fixture",
        difficulty="easy",
        axiom_whitelist=(),
    )
    return PreparedCase(
        case=spec,
        goal=goal,
        gold_source=GOLD,
        scaffold=SCAFFOLD,
        prompt_prefix=SCAFFOLD[:start + len(PLACEHOLDER)],
        module_stem=MODULE,
        path=target,
        target_signature="theorem target : True",
    )


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _project(tmp_path: Path) -> tuple[Path, ProjectConfig, PreparedCase]:
    root = tmp_path / "trusted"
    target = root / "Bench" / "Fixture.lean"
    target.parent.mkdir(parents=True)
    target.write_text(GOLD, encoding="utf-8")
    (root / "Support.lean").write_text("def support : True := True.intro\n")
    (root / ".git").mkdir()
    (root / "nested" / ".git").mkdir(parents=True)

    artifact = root / ".lake" / "build" / "lib" / "lean" / "Bench" / "Fixture"
    artifact.parent.mkdir(parents=True)
    for suffix in (".olean", ".ilean", ".hash", ".trace"):
        Path(str(artifact) + suffix).write_text("STALE GOLD", encoding="utf-8")

    lake = tmp_path / "fake-lake"
    _write_executable(
        lake,
        f"""#!{sys.executable}
import pathlib
import sys

root = pathlib.Path.cwd()
target = root / "Bench" / "Fixture.lean"
base = root / ".lake" / "build" / "lib" / "lean" / "Bench" / "Fixture"
if sys.argv[1] == "build":
    stale = [str(base) + s for s in (".olean", ".ilean", ".hash", ".trace")]
    if any(pathlib.Path(p).exists() for p in stale):
        print("stale target artifact survived", file=sys.stderr)
        raise SystemExit(41)
    base.parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(str(base) + ".olean").write_text("MASKED")
    raise SystemExit(0)
if sys.argv[1:3] == ["env", "lean"]:
    print("'{THEOREM}' depends on axioms: [sorryAx]")
    raise SystemExit(0)
raise SystemExit(42)
""",
    )
    return root, ProjectConfig(root=str(root), lake=str(lake)), _prepared(root)


def _orchestrator(root: Path) -> tuple[Orchestrator, _TrustedRunner]:
    orch = Orchestrator.__new__(Orchestrator)
    orch.cfg = Config()
    orch.cfg.project.root = str(root)
    orch.db = _DB()
    runner = _TrustedRunner()
    orch.runner = runner
    orch._goal_hash = ""
    orch._goal_sigs = {}
    return orch, runner


def _config(agent: Path, *args: str, timeout_s: float = 5.0) -> AgentBenchmarkConfig:
    return AgentBenchmarkConfig(
        enabled=True,
        argv=[sys.executable, str(agent), *args],
        model_label="fixture-agent",
        trajectories=1,
        timeout_s=timeout_s,
        terminate_grace_s=0.5,
    )


def _assert_process_gone(pid: int) -> None:
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    raise AssertionError(f"process {pid} remained alive")


def test_fresh_git_free_workspace_removes_stale_artifacts_and_discards_edits(tmp_path):
    root, project, item = _project(tmp_path)
    dependency = root / "dependency-source.txt"
    dependency.write_text("trusted dependency")
    (root / "dependency-link.txt").symlink_to(dependency.name)
    agent = tmp_path / "agent.py"
    _write_executable(
        agent,
        f"""import pathlib
import sys
root = pathlib.Path.cwd()
assert not list(root.rglob('.git'))
assert not (root / 'dependency-link.txt').is_symlink()
(root / 'dependency-link.txt').write_text('detached edit')
target = root / 'Bench' / 'Fixture.lean'
assert 'sorry' in target.read_text()
(root / 'Support.lean').write_text('def detached_helper : True := True.intro\\n')
(root / 'agent-created.txt').write_text('discard me')
target.write_text(target.read_text().replace({PLACEHOLDER!r}, 'by trivial'))
print('candidate-output-secret')
assert sys.stdin.read()
""",
    )
    orch, trusted_runner = _orchestrator(root)

    result = run_agent_trajectory(
        project,
        item,
        _config(agent),
        trajectory=1,
        submit=lambda candidate, sampling: orch.submit(
            item.goal,
            candidate,
            module_stem=MODULE,
            model="fixture-agent",
            tier="agent",
            sampling=sampling,
        ),
    )

    assert result.accepted and result.status == "accepted"
    assert result.workspace_git_entries == 0
    assert result.removed_artifacts == 4
    assert result.trusted_source_unchanged
    assert root.joinpath("Bench/Fixture.lean").read_text() == GOLD
    assert "detached_helper" not in root.joinpath("Support.lean").read_text()
    assert not root.joinpath("agent-created.txt").exists()
    assert dependency.read_text() == "trusted dependency"
    assert trusted_runner.calls[0][1] == [THEOREM]
    attempt, _ = orch.db.logged[0]
    assert attempt.tier == "agent"
    assert attempt.sampling["trajectory"] == 1


def test_trajectory_rejects_trusted_source_drift_before_copy_or_agent(tmp_path):
    root, project, item = _project(tmp_path)
    root.joinpath("Bench/Fixture.lean").write_text(GOLD + "\n-- drift\n")
    marker = tmp_path / "agent-ran"
    agent = tmp_path / "agent.py"
    _write_executable(agent, f"import pathlib\npathlib.Path({str(marker)!r}).touch()\n")

    result = run_agent_trajectory(
        project,
        item,
        _config(agent),
        trajectory=1,
        submit=lambda _candidate, _sampling: pytest.fail("drifted source was submitted"),
    )

    assert not result.accepted and result.status == "setup_failed"
    assert result.copy_s == 0 and result.returncode is None
    assert not marker.exists()


@pytest.mark.parametrize("alias_kind", ["external", "git"])
def test_copy_rejects_external_and_git_alias_symlinks(tmp_path, alias_kind):
    root, project, item = _project(tmp_path)
    if alias_kind == "external":
        external = tmp_path / "external"
        external.mkdir()
        (external / "data").write_text("outside")
        (root / "dependency-alias").symlink_to(external, target_is_directory=True)
    else:
        (root / "git-alias").symlink_to(root / "nested" / ".git", target_is_directory=True)
    marker = tmp_path / "agent-ran"
    agent = tmp_path / "agent.py"
    _write_executable(agent, f"import pathlib\npathlib.Path({str(marker)!r}).touch()\n")

    result = run_agent_trajectory(
        project,
        item,
        _config(agent),
        trajectory=1,
        submit=lambda _candidate, _sampling: pytest.fail("unsafe copy was submitted"),
    )

    assert not result.accepted and result.status == "setup_failed"
    assert not marker.exists()


@pytest.mark.parametrize("replacement", ["target_symlink", "parent_symlink", "fifo"])
def test_harvest_rejects_links_and_non_regular_targets(tmp_path, replacement):
    root, project, item = _project(tmp_path)
    outside = tmp_path / "outside-target"
    outside.mkdir()
    outside.joinpath("Fixture.lean").write_text(GOLD)
    agent = tmp_path / "agent.py"
    _write_executable(
        agent,
        """import os
import pathlib
import shutil
import sys
target = pathlib.Path('Bench/Fixture.lean')
outside = pathlib.Path(sys.argv[2])
if sys.argv[1] == 'target_symlink':
    target.unlink()
    target.symlink_to(outside / 'Fixture.lean')
elif sys.argv[1] == 'parent_symlink':
    shutil.rmtree('Bench')
    pathlib.Path('Bench').symlink_to(outside, target_is_directory=True)
else:
    target.unlink()
    os.mkfifo(target)
""",
    )

    result = run_agent_trajectory(
        project,
        item,
        _config(agent, replacement, str(outside)),
        trajectory=1,
        submit=lambda _candidate, _sampling: pytest.fail("unsafe target was submitted"),
    )

    assert not result.accepted and result.status == "setup_failed"
    assert result.candidate_chars == 0
    assert root.joinpath("Bench/Fixture.lean").read_text() == GOLD


def test_outside_hole_and_detached_helper_edits_are_rejected_by_trusted_gate(tmp_path):
    root, project, item = _project(tmp_path)
    agent = tmp_path / "agent.py"
    _write_executable(
        agent,
        f"""import pathlib
target = pathlib.Path('Bench/Fixture.lean')
text = target.read_text().replace('theorem keep', 'lemma keep')
target.write_text(text.replace({PLACEHOLDER!r}, 'by trivial'))
""",
    )
    orch, trusted_runner = _orchestrator(root)
    result = run_agent_trajectory(
        project,
        item,
        _config(agent),
        trajectory=1,
        submit=lambda candidate, sampling: orch.submit(
            item.goal, candidate, module_stem=MODULE, tier="agent", sampling=sampling
        ),
    )
    assert not result.accepted
    assert trusted_runner.calls == []  # rejected before the kernel runner

    helper_agent = tmp_path / "helper-agent.py"
    _write_executable(
        helper_agent,
        f"""import pathlib
pathlib.Path('Support.lean').write_text('def detached_helper : True := True.intro\\n')
target = pathlib.Path('Bench/Fixture.lean')
target.write_text(target.read_text().replace({PLACEHOLDER!r}, 'by exact detached_helper'))
""",
    )
    orch, trusted_runner = _orchestrator(root)
    result = run_agent_trajectory(
        project,
        item,
        _config(helper_agent),
        trajectory=1,
        submit=lambda candidate, sampling: orch.submit(
            item.goal, candidate, module_stem=MODULE, tier="agent", sampling=sampling
        ),
    )
    assert not result.accepted and not result.build_ok
    assert len(trusted_runner.calls) == 1
    assert "detached_helper" not in root.joinpath("Support.lean").read_text()


def test_timeout_terminates_process_group_and_reaps_direct_agent(tmp_path):
    root, project, item = _project(tmp_path)
    pidfile = tmp_path / "child.pid"
    agent = tmp_path / "hanging-agent.py"
    _write_executable(
        agent,
        """import pathlib
import signal
import subprocess
import sys
import time

child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
def stop(_sig, _frame):
    try:
        child.wait(timeout=2)
    finally:
        raise SystemExit(143)
signal.signal(signal.SIGTERM, stop)
time.sleep(60)
""",
    )
    orch, _ = _orchestrator(root)
    result = run_agent_trajectory(
        project,
        item,
        _config(agent, str(pidfile), timeout_s=0.2),
        trajectory=1,
        submit=lambda candidate, sampling: orch.submit(
            item.goal, candidate, module_stem=MODULE, tier="agent", sampling=sampling
        ),
    )

    assert result.timed_out and not result.accepted
    _assert_process_gone(int(pidfile.read_text()))


def test_normal_wrapper_exit_terminates_orphaned_agent_children(tmp_path):
    root, project, item = _project(tmp_path)
    pidfile = tmp_path / "orphan.pid"
    agent = tmp_path / "exiting-agent.py"
    _write_executable(
        agent,
        f"""import pathlib
import subprocess
import sys
target = pathlib.Path('Bench/Fixture.lean')
target.write_text(target.read_text().replace({PLACEHOLDER!r}, 'by trivial'))
child = subprocess.Popen(
    [sys.executable, '-c', 'import time; time.sleep(60)'],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
""",
    )
    orch, _ = _orchestrator(root)
    result = run_agent_trajectory(
        project,
        item,
        _config(agent, str(pidfile), timeout_s=5),
        trajectory=1,
        submit=lambda candidate, sampling: orch.submit(
            item.goal, candidate, module_stem=MODULE, tier="agent", sampling=sampling
        ),
    )

    assert result.accepted and not result.timed_out
    _assert_process_gone(int(pidfile.read_text()))


def test_setup_timeout_terminates_its_whole_process_group(tmp_path, monkeypatch):
    import leanloop.agent_bench as agent_bench_module

    pidfile = tmp_path / "setup-child.pid"
    setup = tmp_path / "hanging-setup.py"
    _write_executable(
        setup,
        """import pathlib
import subprocess
import sys
import time
child = subprocess.Popen(
    [sys.executable, '-c', 'import time; time.sleep(60)'],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(60)
""",
    )
    monkeypatch.setattr(agent_bench_module, "_SETUP_TIMEOUT_S", 0.2)
    monkeypatch.setattr(agent_bench_module, "_SETUP_TERMINATE_GRACE_S", 0.2)

    with pytest.raises(subprocess.TimeoutExpired):
        _run_setup([sys.executable, str(setup), str(pidfile)], cwd=tmp_path)
    _assert_process_gone(int(pidfile.read_text()))


def test_masked_axiom_check_rejects_stale_gold_and_unrelated_sorry_ax():
    _require_masked_target_sorry_ax(
        f"'{THEOREM}' depends on axioms: [propext, sorryAx]", THEOREM
    )
    with pytest.raises(RuntimeError, match="does not contain sorryAx"):
        _require_masked_target_sorry_ax(
            f"'{THEOREM}' does not depend on any axioms\n"
            "'Other.target' depends on axioms: [sorryAx]",
            THEOREM,
        )
    with pytest.raises(RuntimeError, match="exact axiom closures"):
        _require_masked_target_sorry_ax(
            "'Other.target' depends on axioms: [sorryAx]", THEOREM
        )


def test_receipts_contain_only_fingerprints_not_raw_agent_material(tmp_path):
    root, project, item = _project(tmp_path)
    secret = "RAW_PROOF_OR_OUTPUT_MUST_NOT_PERSIST"
    agent = tmp_path / f"{secret}.py"
    _write_executable(
        agent,
        f"""import pathlib
import sys
target = pathlib.Path('Bench/Fixture.lean')
target.write_text(target.read_text().replace({PLACEHOLDER!r}, 'by trivial -- {secret}'))
print('{secret}')
print('{secret}', file=sys.stderr)
""",
    )
    orch, _ = _orchestrator(root)
    cfg = _config(agent)
    result = run_agent_trajectory(
        project,
        item,
        cfg,
        trajectory=1,
        submit=lambda candidate, sampling: orch.submit(
            item.goal, candidate, module_stem=MODULE, tier="agent", sampling=sampling
        ),
    )
    encoded = json.dumps({"config": agent_config_receipt(cfg), "result": result.receipt()})
    assert secret not in encoded
    assert "argv" not in result.receipt()
    assert result.candidate_sha256 and result.stdout_sha256 and result.stderr_sha256


def test_agent_benchmark_config_is_disabled_by_default_and_loads_nested_toml(tmp_path):
    assert Config().benchmark.agent.enabled is False
    path = tmp_path / "leanloop.toml"
    path.write_text(
        """[benchmark.agent]
enabled = true
argv = ["vibe", "--prompt", "-"]
model_label = "leanstral"
trajectories = 4
timeout_s = 7200
"""
    )
    cfg = Config.load(path).benchmark.agent
    assert cfg.enabled and cfg.argv == ["vibe", "--prompt", "-"]
    assert cfg.model_label == "leanstral" and cfg.trajectories == 4
    assert cfg.timeout_s == 7200


def test_agentic_cli_writes_atomic_proof_free_checkpoint(tmp_path, monkeypatch):
    import leanloop.agent_bench as agent_bench_module
    import leanloop.cli as cli_module
    import leanloop.heldout as heldout_module
    import leanloop.lean_runner as runner_module

    root, project, item = _project(tmp_path)
    suite = tmp_path / "suite.toml"
    suite.write_text("fixture manifest bytes\n")
    report = tmp_path / "receipt.json"
    raw_secret = "RAW_ARG_PROMPT_CANDIDATE_OUTPUT"
    cfg = Config()
    cfg.project = project
    cfg.benchmark.agent = AgentBenchmarkConfig(
        enabled=True,
        argv=[str(tmp_path / raw_secret)],
        model_label="fixture",
        trajectories=1,
        timeout_s=10,
    )

    class FakeLeanRunner:
        def __init__(self, _project):
            pass

        def verify(self, _source, theorem_fqns, *, module_stem):
            assert module_stem == MODULE
            return VerifyResult(
                build_ok=True,
                axioms=(
                    f"'{THEOREM}' does not depend on any axioms"
                    if theorem_fqns else ""
                ),
            )

    class FakeOrchestrator:
        def __init__(self, _cfg, *, config_path=""):
            assert _cfg.db_path == ":memory:"

        def close(self):
            pass

    safe_result = AgentTrajectoryResult(
        trajectory=1,
        status="accepted",
        accepted=True,
        timed_out=False,
        returncode=0,
        wall_s=1.0,
        copy_s=0.1,
        setup_s=0.2,
        agent_s=0.3,
        verify_s=0.4,
        removed_artifacts=2,
        workspace_git_entries=0,
        trusted_source_unchanged=True,
        candidate_chars=len(raw_secret),
        candidate_sha256="a" * 64,
        stdout_chars=len(raw_secret),
        stdout_sha256="b" * 64,
        stderr_chars=0,
        stderr_sha256="",
        setup_error_chars=0,
        setup_error_sha256="",
        build_ok=True,
        audit_ok=True,
        verifier_error_chars=0,
        verifier_error_sha256="",
        axiom_output_chars=10,
        axiom_output_sha256="c" * 64,
    )

    monkeypatch.setattr(cli_module, "_load", lambda _args: cfg)
    monkeypatch.setattr(cli_module, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(heldout_module, "load_suite", lambda _root, _suite: [item])
    monkeypatch.setattr(runner_module, "LeanRunner", FakeLeanRunner)
    monkeypatch.setattr(
        agent_bench_module,
        "run_agent_trajectory",
        lambda *_args, **_kwargs: safe_result,
    )
    args = SimpleNamespace(
        config=None,
        suite=str(suite),
        limit=0,
        report=str(report),
        preflight_only=False,
        skip_throughput=False,
        agentic=True,
        goals=0,
        samples=1,
    )

    assert cli_module.cmd_bench(args) == 0
    encoded = report.read_text()
    receipt = json.loads(encoded)
    assert receipt["mode"] == "external-agent"
    assert receipt["summary"]["closed"] == 1
    assert "finished_at" in receipt and "checkpointed_at" not in receipt
    assert raw_secret not in encoded and "by trivial" not in encoded
    assert not list(tmp_path.glob(f".{report.name}.*.tmp"))
    assert root.joinpath("Bench/Fixture.lean").read_text() == GOLD

    cfg.benchmark.agent.trajectories = 2
    calls = 0

    def crash_after_checkpoint(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return safe_result

    monkeypatch.setattr(agent_bench_module, "run_agent_trajectory", crash_after_checkpoint)
    with pytest.raises(KeyboardInterrupt):
        cli_module.cmd_bench(args)
    checkpoint = json.loads(report.read_text())
    assert len(checkpoint["results"]) == 1
    assert "checkpointed_at" in checkpoint and "finished_at" not in checkpoint
    assert not list(tmp_path.glob(f".{report.name}.*.tmp"))
