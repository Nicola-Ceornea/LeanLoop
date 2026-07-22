"""Isolated external-agent trajectories for held-out proof replay.

The external command is untrusted.  It works in a fresh project copy with no
Git metadata and with the selected proof replaced by the held-out scaffold.
Only the target module is harvested, and that complete file still has to pass
the ordinary Orchestrator statement/source/kernel/axiom gates in the trusted
project.  This copy boundary isolates ordinary cwd-relative edits; it is not a
host sandbox.  Put bubblewrap/OCI (or an equivalent wrapper) in argv when the
agent must be prevented from reaching paths outside the detached copy.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import AgentBenchmarkConfig, ProjectConfig
from .heldout import PreparedCase
from .provers.base import ProofAttempt


_SETUP_TIMEOUT_S = 1800.0
_SETUP_TERMINATE_GRACE_S = 2.0
_MAX_CANDIDATE_BYTES = 16 * 1024 * 1024
_ARTIFACT_SUFFIXES = (
    ".olean",
    ".ilean",
    ".trace",
    ".hash",
    ".olean.hash",
    ".ilean.hash",
    ".trace.hash",
)


def _fingerprint(text: str) -> dict[str, int | str]:
    raw = text.encode("utf-8")
    return {
        "chars": len(text),
        "sha256": hashlib.sha256(raw).hexdigest() if raw else "",
    }


@dataclass(frozen=True)
class AgentTrajectoryResult:
    """Proof-free outcome of one external-agent trajectory."""

    trajectory: int
    status: str
    accepted: bool
    timed_out: bool
    returncode: int | None
    wall_s: float
    copy_s: float
    setup_s: float
    agent_s: float
    verify_s: float
    removed_artifacts: int
    workspace_git_entries: int
    trusted_source_unchanged: bool
    candidate_chars: int
    candidate_sha256: str
    stdout_chars: int
    stdout_sha256: str
    stderr_chars: int
    stderr_sha256: str
    setup_error_chars: int
    setup_error_sha256: str
    build_ok: bool
    audit_ok: bool
    verifier_error_chars: int
    verifier_error_sha256: str
    axiom_output_chars: int
    axiom_output_sha256: str

    def receipt(self) -> dict:
        """Return only hashes, counts, timings, and verdicts.

        In particular this object never retains the prompt, argv, candidate,
        process output, or verifier diagnostics themselves.
        """
        return {
            "trajectory": self.trajectory,
            "status": self.status,
            "accepted": self.accepted,
            "timed_out": self.timed_out,
            "returncode": self.returncode,
            "timing_s": {
                "wall": self.wall_s,
                "copy": self.copy_s,
                "setup": self.setup_s,
                "agent": self.agent_s,
                "verify": self.verify_s,
            },
            "workspace": {
                "removed_stale_artifacts": self.removed_artifacts,
                "git_entries": self.workspace_git_entries,
                "trusted_source_unchanged": self.trusted_source_unchanged,
            },
            "candidate_fingerprint": {
                "chars": self.candidate_chars,
                "sha256": self.candidate_sha256,
            },
            "process_output_fingerprints": {
                "stdout_chars": self.stdout_chars,
                "stdout_sha256": self.stdout_sha256,
                "stderr_chars": self.stderr_chars,
                "stderr_sha256": self.stderr_sha256,
            },
            "setup_error_fingerprint": {
                "chars": self.setup_error_chars,
                "sha256": self.setup_error_sha256,
            },
            "verification": {
                "build_ok": self.build_ok,
                "audit_ok": self.audit_ok,
                "error_chars": self.verifier_error_chars,
                "error_sha256": self.verifier_error_sha256,
                "axiom_output_chars": self.axiom_output_chars,
                "axiom_output_sha256": self.axiom_output_sha256,
            },
        }


def agent_config_receipt(cfg: AgentBenchmarkConfig) -> dict:
    """Proof-free, credential-safe public description of an agent config."""
    encoded_argv = json.dumps(cfg.argv, ensure_ascii=False, separators=(",", ":")).encode()
    return {
        "model_label": cfg.model_label,
        "trajectories": cfg.trajectories,
        "timeout_s": cfg.timeout_s,
        "terminate_grace_s": cfg.terminate_grace_s,
        "argv_items": len(cfg.argv),
        "argv_sha256": hashlib.sha256(encoded_argv).hexdigest() if cfg.argv else "",
        "prompt_transport": "stdin",
        "workspace_isolation": "detached-copy; use an argv sandbox wrapper for host isolation",
    }


def _ignore_git(_directory: str, names: list[str]) -> set[str]:
    return {".git"} if ".git" in names else set()


def _validate_project_symlinks(source: Path) -> None:
    """Reject links that would escape the copy or smuggle Git metadata into it."""
    root = source.resolve()
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        # Real Git directories/files are excluded by copytree.  A differently
        # named symlink into one is inspected below and rejected.
        dirnames[:] = [name for name in dirnames if name != ".git"]
        names = [*dirnames, *(name for name in filenames if name != ".git")]
        for name in names:
            link = Path(directory) / name
            if not link.is_symlink():
                continue
            try:
                target = link.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise RuntimeError(f"detached copy rejects unresolved symlink {link}") from exc
            try:
                relative_target = target.relative_to(root)
            except ValueError as exc:
                raise RuntimeError(
                    f"detached copy rejects symlink escaping project: {link} -> {target}"
                ) from exc
            if ".git" in relative_target.parts:
                raise RuntimeError(
                    f"detached copy rejects symlink alias into Git metadata: {link}"
                )
            # Following a link from inside its own target recursively expands
            # forever under copytree(symlinks=False).
            if target.is_dir():
                try:
                    link.relative_to(target)
                except ValueError:
                    pass
                else:
                    raise RuntimeError(f"detached copy rejects recursive symlink: {link}")


def _copy_project(source: Path, destination: Path) -> int:
    # Follow validated internal symlinks into independent files/directories.
    # Preserving links could let the agent write through to the trusted tree.
    _validate_project_symlinks(source)
    shutil.copytree(source, destination, symlinks=False, ignore=_ignore_git)
    git_entries = sum(1 for path in destination.rglob(".git") if path.name == ".git")
    if git_entries:
        raise RuntimeError("detached workspace still contains Git metadata")
    return git_entries


def _target_path(root: Path, module_stem: str) -> Path:
    target = root / Path(*module_stem.split(".")).with_suffix(".lean")
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"target module resolves outside detached project: {module_stem}") from exc
    return target


def _harvest_target(root: Path, module_stem: str) -> bytes:
    """Read a bounded regular target without following any agent-created link."""
    rel = Path(*module_stem.split(".")).with_suffix(".lean")
    root_stat = os.lstat(root)
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise RuntimeError("detached project root is no longer a real directory")

    current = root
    for part in rel.parts[:-1]:
        current /= part
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f"agent target parent is not a real directory: {current}")

    target = current / rel.name
    before = os.lstat(target)
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        raise RuntimeError("agent target is not a regular non-symlink file")
    if before.st_size > _MAX_CANDIDATE_BYTES:
        raise RuntimeError(f"agent target exceeds {_MAX_CANDIDATE_BYTES} byte safety limit")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags)
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_nlink != 1
            or opened.st_size > _MAX_CANDIDATE_BYTES
        ):
            raise RuntimeError("agent target changed while being opened")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, _MAX_CANDIDATE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_CANDIDATE_BYTES:
                raise RuntimeError(
                    f"agent target exceeds {_MAX_CANDIDATE_BYTES} byte safety limit"
                )
        return b"".join(chunks)
    finally:
        os.close(fd)


def _remove_stale_target_artifacts(root: Path, module_stem: str) -> int:
    rel = Path(*module_stem.split("."))
    base = root / ".lake" / "build" / "lib" / "lean" / rel
    removed = 0
    for suffix in _ARTIFACT_SUFFIXES:
        path = Path(str(base) + suffix)
        if path.is_file() or path.is_symlink():
            path.unlink()
            removed += 1
    return removed


def _run_setup(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
        close_fds=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=_SETUP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        stdout, stderr = _stop_and_reap(proc, _SETUP_TERMINATE_GRACE_S)
        raise subprocess.TimeoutExpired(
            argv, _SETUP_TIMEOUT_S, output=stdout, stderr=stderr
        )
    except BaseException:
        _stop_and_reap(proc, _SETUP_TERMINATE_GRACE_S)
        raise
    else:
        returncode = proc.returncode
        _terminate_remaining_group(proc.pid, _SETUP_TERMINATE_GRACE_S)
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _require_masked_target_sorry_ax(output: str, theorem: str) -> None:
    """Require one exact ``#print axioms theorem`` result containing sorryAx."""
    quoted = re.escape(theorem)
    depends = re.findall(
        rf"(?m)^[ \t]*'{quoted}' depends on axioms:\s*\[([^\]\r\n]*)\][ \t]*$",
        output,
    )
    clean = re.findall(
        rf"(?m)^[ \t]*'{quoted}' does not depend on any axioms[ \t]*$",
        output,
    )
    if len(depends) + len(clean) != 1:
        raise RuntimeError(
            f"masked target produced {len(depends) + len(clean)} exact axiom closures "
            f"for {theorem!r}; expected one"
        )
    axioms = {
        name.strip().strip("'\"")
        for group in depends
        for name in group.split(",")
        if name.strip()
    }
    if "sorryAx" not in axioms:
        raise RuntimeError(
            "masked target's exact axiom closure does not contain sorryAx; refusing a "
            "possibly stale gold build"
        )


def _assert_masked_build(
    root: Path,
    project: ProjectConfig,
    item: PreparedCase,
) -> tuple[int, str]:
    """Force a masked rebuild and prove the imported target contains sorryAx."""
    removed = _remove_stale_target_artifacts(root, item.module_stem)
    build = _run_setup(
        [project.lake, "build", item.module_stem, *project.build_args], cwd=root
    )
    if build.returncode != 0:
        raise RuntimeError("masked lake build failed\n" + build.stdout + "\n" + build.stderr)

    checker = root / f"LeanLoopAgentMaskCheck_{uuid.uuid4().hex}.lean"
    try:
        checker.write_text(
            f"import {item.module_stem}\n#print axioms {item.case.theorem}\n",
            encoding="utf-8",
        )
        closure = _run_setup(
            [project.lake, "env", "lean", str(checker)], cwd=root
        )
        output = (closure.stdout + "\n" + closure.stderr).strip()
        if closure.returncode != 0:
            raise RuntimeError("masked axiom check failed\n" + output)
        _require_masked_target_sorry_ax(output, item.case.theorem)
        return removed, output
    finally:
        checker.unlink(missing_ok=True)


def _prompt(project: ProjectConfig, item: PreparedCase) -> str:
    rel = Path(*item.module_stem.split(".")).with_suffix(".lean")
    build_command = shlex.join(
        [project.lake, "build", item.module_stem, *project.build_args]
    )
    return (
        "You are running a headless held-out benchmark in a disposable detached "
        "Lean 4 Lake project. Proceed immediately without asking for confirmation; "
        "inspection, editing the target proof, and Lean/Lake commands are explicitly "
        "authorized.\n"
        f"Prove exactly `{item.case.theorem}` in `{rel}`.\n"
        "Read the target file before editing. Replace its existing `by ... sorry` proof "
        "placeholder with a complete proof. "
        "Do not change its statement, imports, surrounding declarations, or any other file.\n"
        "Use bash and Lean LSP feedback as needed to repair the proof, then verify it with "
        f"`{build_command}`. Do not use `sorry`, `admit`, new axioms, `native_decide`, "
        "or the theorem currently being proved. Do not commit. Finish only after the "
        "target builds without proof placeholders.\n"
        "Only the target module will be harvested; every other edit is discarded, and the "
        "result will be checked independently through statement, source, kernel, and exact "
        "axiom-closure gates.\n"
        f"Pinned signature: {item.target_signature}\n"
    )


def _signal_process_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_process_group_gone(pgid: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_s)
    while _process_group_exists(pgid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
    return True


def _terminate_remaining_group(pgid: int, grace_s: float) -> None:
    """Terminate descendants left behind after their direct parent exits."""
    _signal_process_group(pgid, signal.SIGTERM)
    if _wait_process_group_gone(pgid, grace_s):
        return
    _signal_process_group(pgid, signal.SIGKILL)
    _wait_process_group_gone(pgid, grace_s)


def _stop_and_reap(
    proc: subprocess.Popen[str], grace_s: float
) -> tuple[str, str]:
    _signal_process_group(proc.pid, signal.SIGTERM)
    try:
        stdout, stderr = proc.communicate(timeout=max(0.0, grace_s))
    except subprocess.TimeoutExpired:
        _signal_process_group(proc.pid, signal.SIGKILL)
        stdout, stderr = proc.communicate()
    _terminate_remaining_group(proc.pid, grace_s)
    return stdout, stderr


def _run_agent(
    argv: list[str],
    *,
    cwd: Path,
    prompt: str,
    timeout_s: float,
    terminate_grace_s: float,
) -> tuple[int | None, bool, str, str]:
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
        close_fds=True,
    )
    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout_s)
        returncode = proc.returncode
        _terminate_remaining_group(proc.pid, terminate_grace_s)
        return returncode, False, stdout, stderr
    except subprocess.TimeoutExpired:
        stdout, stderr = _stop_and_reap(proc, terminate_grace_s)
        # communicate() has completed: the direct process is reaped, and every
        # descendant that remained in its process group received TERM/KILL.
        return proc.returncode, True, stdout, stderr
    except BaseException:
        # Ctrl-C or an unexpected communicate failure must not strand an agent
        # (or its Lean/LSP children) after the disposable workspace is removed.
        _stop_and_reap(proc, terminate_grace_s)
        raise


def run_agent_trajectory(
    project: ProjectConfig,
    item: PreparedCase,
    cfg: AgentBenchmarkConfig,
    *,
    trajectory: int,
    submit: Callable[[str, dict], ProofAttempt],
) -> AgentTrajectoryResult:
    """Run one detached trajectory and submit only its target module.

    ``submit`` is normally a thin call to ``Orchestrator.submit(...,
    tier="agent")`` whose runner points at the untouched trusted project.
    """
    started = time.monotonic()
    copy_s = setup_s = agent_s = verify_s = 0.0
    removed = git_entries = 0
    timed_out = False
    returncode: int | None = None
    candidate = stdout = stderr = setup_error = ""
    attempt: ProofAttempt | None = None
    status = "setup_failed"
    trusted_unchanged = True

    if not cfg.enabled:
        setup_error = "external-agent benchmark is disabled"
    elif not cfg.argv or not cfg.argv[0]:
        setup_error = "external-agent argv must contain an executable"
    elif cfg.trajectories < 1:
        setup_error = "external-agent trajectories must be at least one"
    elif cfg.timeout_s <= 0:
        setup_error = "external-agent timeout_s must be positive"
    elif cfg.terminate_grace_s < 0:
        setup_error = "external-agent terminate_grace_s cannot be negative"
    else:
        trusted_root = Path(project.root).resolve()
        trusted_target = _target_path(trusted_root, item.module_stem)
        gold_bytes = item.gold_source.encode("utf-8")
        try:
            trusted_stat = os.lstat(trusted_target)
            if (
                stat.S_ISLNK(trusted_stat.st_mode)
                or not stat.S_ISREG(trusted_stat.st_mode)
                or trusted_stat.st_size != len(gold_bytes)
            ):
                raise RuntimeError("trusted target is not the expected regular file")
            trusted_before = trusted_target.read_bytes()
        except OSError as exc:
            setup_error = f"cannot pin trusted target before trajectory: {exc}"
        except RuntimeError as exc:
            setup_error = str(exc)
        else:
            if trusted_before != gold_bytes:
                setup_error = "trusted target drifted after held-out suite validation"
            else:
                try:
                    with tempfile.TemporaryDirectory(prefix="leanloop-agent-") as raw_tmp:
                        workspace = Path(raw_tmp) / "project"
                        t0 = time.monotonic()
                        git_entries = _copy_project(trusted_root, workspace)
                        copy_s = time.monotonic() - t0

                        target = _target_path(workspace, item.module_stem)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(item.scaffold, encoding="utf-8")
                        target.with_suffix(".lean.leanloop-bak").unlink(missing_ok=True)

                        t0 = time.monotonic()
                        removed, _masked_axioms = _assert_masked_build(workspace, project, item)
                        setup_s = time.monotonic() - t0

                        t0 = time.monotonic()
                        returncode, timed_out, stdout, stderr = _run_agent(
                            cfg.argv,
                            cwd=workspace,
                            prompt=_prompt(project, item),
                            timeout_s=cfg.timeout_s,
                            terminate_grace_s=cfg.terminate_grace_s,
                        )
                        agent_s = time.monotonic() - t0

                        candidate = _harvest_target(workspace, item.module_stem).decode("utf-8")

                        sampling = {
                            "trajectory": trajectory,
                            "agent_returncode": returncode,
                            "agent_timed_out": timed_out,
                        }
                        t0 = time.monotonic()
                        attempt = submit(candidate, sampling)
                        verify_s = time.monotonic() - t0
                        status = "accepted" if attempt.accepted else (
                            "timeout_rejected" if timed_out else "rejected"
                        )
                except subprocess.TimeoutExpired as exc:
                    setup_error = f"detached masked-project setup timed out: {exc}"
                except (OSError, RuntimeError, UnicodeError, shutil.Error) as exc:
                    setup_error = f"detached trajectory failed: {exc}"
                finally:
                    try:
                        trusted_after_stat = os.lstat(trusted_target)
                        trusted_unchanged = (
                            stat.S_ISREG(trusted_after_stat.st_mode)
                            and not stat.S_ISLNK(trusted_after_stat.st_mode)
                            and trusted_after_stat.st_size == len(trusted_before)
                            and trusted_target.read_bytes() == trusted_before
                        )
                    except OSError:
                        trusted_unchanged = False
                    if not trusted_unchanged:
                        status = "trusted_source_changed"
                        if attempt is not None:
                            attempt.accepted = False

    candidate_fp = _fingerprint(candidate)
    stdout_fp = _fingerprint(stdout)
    stderr_fp = _fingerprint(stderr)
    setup_fp = _fingerprint(setup_error)
    verifier_error_fp = _fingerprint(attempt.lean_errors if attempt else "")
    axiom_fp = _fingerprint(attempt.axioms if attempt else "")
    return AgentTrajectoryResult(
        trajectory=trajectory,
        status=status,
        accepted=bool(attempt and attempt.accepted and trusted_unchanged),
        timed_out=timed_out,
        returncode=returncode,
        wall_s=time.monotonic() - started,
        copy_s=copy_s,
        setup_s=setup_s,
        agent_s=agent_s,
        verify_s=verify_s,
        removed_artifacts=removed,
        workspace_git_entries=git_entries,
        trusted_source_unchanged=trusted_unchanged,
        candidate_chars=int(candidate_fp["chars"]),
        candidate_sha256=str(candidate_fp["sha256"]),
        stdout_chars=int(stdout_fp["chars"]),
        stdout_sha256=str(stdout_fp["sha256"]),
        stderr_chars=int(stderr_fp["chars"]),
        stderr_sha256=str(stderr_fp["sha256"]),
        setup_error_chars=int(setup_fp["chars"]),
        setup_error_sha256=str(setup_fp["sha256"]),
        build_ok=bool(attempt and attempt.build_ok),
        audit_ok=bool(attempt and attempt.audit_ok),
        verifier_error_chars=int(verifier_error_fp["chars"]),
        verifier_error_sha256=str(verifier_error_fp["sha256"]),
        axiom_output_chars=int(axiom_fp["chars"]),
        axiom_output_sha256=str(axiom_fp["sha256"]),
    )
