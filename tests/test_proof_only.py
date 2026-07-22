"""Trusted proof-only prompting and Ollama residency controls."""
from __future__ import annotations

import asyncio
import textwrap

import pytest

from leanloop.config import Config, LocalProverConfig
from leanloop.provers.base import Goal
from leanloop.provers.ollama import PROOF_ONLY_SYSTEM, LocalProver


def _goal() -> Goal:
    prefix = "theorem Locked : True := "
    placeholder = "by sorry"
    suffix = "\n#check Locked\n"
    scaffold = prefix + placeholder + suffix
    start = len(prefix)
    return Goal(
        name="Locked",
        file_text=scaffold,
        prompt_text=prefix + placeholder,
        target_fqns=["Bench.Locked"],
        proof_hole=(start, start + len(placeholder)),
        axiom_whitelist=[],
    )


def test_goal_materializes_only_inside_trusted_half_open_span():
    goal = _goal()
    candidate = goal.materialize_proof("\n by\n  trivial \n")
    assert candidate == "theorem Locked : True := by\n  trivial\n#check Locked\n"

    with pytest.raises(ValueError, match="requires Goal.proof_hole"):
        Goal(name="legacy", file_text="theorem legacy : True := by sorry").materialize_proof(
            "by trivial"
        )
    goal.proof_hole = (0, len(goal.file_text) + 1)
    with pytest.raises(ValueError, match="invalid proof hole"):
        goal.materialize_proof("by trivial")


def test_legacy_defaults_and_toml_profile_controls(tmp_path):
    defaults = LocalProverConfig()
    assert defaults.prompt_profile == "goedel"
    assert defaults.keep_alive == "5m"
    assert defaults.seed is None

    path = tmp_path / "leanloop.toml"
    path.write_text(textwrap.dedent("""
        [prover.local]
        model = "leanstral"
        prompt_profile = "proof_only"
        keep_alive = "-1"
        seed = 42
    """))
    cfg = Config.load(path)
    assert cfg.prover.local.model == "leanstral"
    assert cfg.prover.local.prompt_profile == "proof_only"
    assert cfg.prover.local.keep_alive == "-1"
    assert cfg.prover.local.seed == 42


def test_proof_only_prompt_is_separate_and_candidate_is_spliced(monkeypatch):
    cfg = LocalProverConfig(
        model="leanstral",
        prompt_profile="proof_only",
        samples=1,
        concurrency=1,
    )
    prover = LocalProver(cfg)
    captured = {}

    async def fake_generate(client, prompt, temp, *, system=""):
        captured.update(prompt=prompt, temperature=temp, system=system)
        return "```lean4\nby\n  trivial\n```"

    monkeypatch.setattr(prover, "_generate", fake_generate)
    (candidate,) = prover.propose(_goal(), feedback="unknown identifier")

    assert candidate == "theorem Locked : True := by\n  trivial\n#check Locked\n"
    assert captured["system"] == PROOF_ONLY_SYSTEM
    assert "Goal to close: Bench.Locked" in captured["prompt"]
    assert "by sorry" in captured["prompt"]
    assert "#check Locked" not in captured["prompt"]  # prompt_text, not the full scaffold
    assert "replacement for the final masked proof expression" in captured["prompt"]
    assert "unknown identifier" in captured["prompt"]
    assert prover.last_metadata[0]["prompt_profile"] == "proof_only"
    assert prover.last_metadata[0]["candidate_chars"] == len(candidate)
    assert prover.last_generation_metadata is prover.last_metadata


def test_proof_only_fails_closed_without_a_trusted_hole():
    prover = LocalProver(LocalProverConfig(prompt_profile="proof_only", samples=0))
    goal = Goal(name="t", file_text="theorem t : True := by sorry")
    with pytest.raises(ValueError, match="requires Goal.proof_hole"):
        prover.propose(goal)


def test_fenced_tactic_body_is_wrapped_but_unfenced_prose_is_not():
    assert LocalProver._extract_proof(
        "```lean\nhave h : True := by trivial\nexact h\n```"
    ) == "by\n  have h : True := by trivial\n  exact h"
    assert LocalProver._extract_proof("Here is the proof: exact True.intro") == (
        "Here is the proof: exact True.intro"
    )


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {"response": "by trivial", "choices": [{"message": {"content": "by trivial"}}]}


class _AsyncClient:
    def __init__(self):
        self.url = ""
        self.kwargs = {}

    async def post(self, url, **kwargs):
        self.url = url
        self.kwargs = kwargs
        return _Response()


def test_ollama_payload_carries_system_prompt_and_keep_alive():
    prover = LocalProver(LocalProverConfig(
        model="leanstral",
        prompt_profile="proof_only",
        keep_alive="-1",
        seed=42,
    ))
    client = _AsyncClient()
    result = asyncio.run(prover._generate(
        client, "Goal to close: t", 1.0, system=PROOF_ONLY_SYSTEM
    ))
    assert result == "by trivial"
    assert client.url.endswith("/api/generate")
    assert client.kwargs["json"]["system"] == PROOF_ONLY_SYSTEM
    assert client.kwargs["json"]["keep_alive"] == "-1"
    assert client.kwargs["json"]["options"]["seed"] == 42


def test_openai_payload_uses_distinct_system_and_user_messages():
    prover = LocalProver(LocalProverConfig(
        backend="openai",
        prompt_profile="proof_only",
    ))
    client = _AsyncClient()
    asyncio.run(prover._generate(
        client, "Goal to close: t", 1.0, system=PROOF_ONLY_SYSTEM
    ))
    assert [message["role"] for message in client.kwargs["json"]["messages"]] == [
        "system", "user",
    ]
    assert client.kwargs["json"]["messages"][1]["content"] == "Goal to close: t"
