"""Frontier prover tier — spec authoring, decomposition, auxiliary-lemma
generation, and error-class-routed repair. Reached via the `claude` CLI by
default (no separate API bill).

Design choice: we call the CLI in PURE TEXT-GENERATION mode (`claude -p`, no
tools). The frontier model only emits a candidate Lean file as text; LeanLoop
writes it and the Lean kernel verifies it. So the model never touches the repo
and never bypasses the audit gate — a wrong proposal just fails to compile.

Cost note: interactive `claude` uses subscription flat-rate; headless `claude -p`
moves to the metered credit pool (Anthropic, 2026-06-15). Keep this tier sparse
(escalate_after / frontier.enabled). For a frontier endpoint you pay per token,
set backend = "openai" with base_url/api_key instead.
"""
from __future__ import annotations

import subprocess

import httpx

from ..config import FrontierProverConfig
from .base import Goal

SYSTEM = (
    "You are an expert in Lean 4 and the Aeneas verification library "
    "(progress, scalar_tac, bvify/bv_tac, step*, the Result/ok/fail monad over "
    "machine integers). You close ONE proof obligation. Output ONLY a complete "
    "Lean 4 file in a ```lean4 fenced block — no prose. Do NOT use `sorry`, "
    "`admit`, new `axiom` declarations, or `native_decide`; the file must "
    "type-check under the Lean kernel with a clean `#print axioms`."
)

USER = """Close the `sorry` in this Lean 4 file. Reuse the Aeneas tactics and any
lemmas in the provided context. Prefer the project's established patterns.

{plan}{context}

```lean4
{statement}```"""

REPAIR = """

The previous attempt failed to compile. Lean reported:
```
{errors}
```
Return the corrected complete file."""


class FrontierProver:
    name = "frontier"

    def __init__(self, cfg: FrontierProverConfig):
        self.cfg = cfg

    # ------------------------------------------------------------------ #
    def propose(self, goal: Goal, *, feedback: str = "") -> list[str]:
        if not self.cfg.enabled or self.cfg.backend == "none":
            return []
        prompt = self._build(goal, feedback)
        if self.cfg.backend == "claude_cli":
            out = self._claude_cli(prompt)
        else:  # openai-compatible frontier endpoint
            out = self._openai(prompt)
        return [self._extract(out)] if out else []

    # ------------------------------------------------------------------ #
    def _build(self, goal: Goal, feedback: str) -> str:
        plan = (
            "First sketch a 2-4 step proof plan, then give the file.\n\n"
            if not feedback else ""
        )
        context = f"\nContext / available lemmas:\n{goal.context}\n" if goal.context else ""
        prompt = USER.format(plan=plan, context=context, statement=goal.file_text)
        if feedback:
            prompt += REPAIR.format(errors=feedback[:4000])
        return f"{SYSTEM}\n\n{prompt}"

    def _claude_cli(self, prompt: str) -> str:
        cmd = [self.cfg.command, "-p", prompt, "--max-turns", str(self.cfg.max_turns)]
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            return res.stdout
        except Exception as e:
            return f"-- LEANLOOP frontier error: {e}\n"

    def _openai(self, prompt: str) -> str:
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"} if self.cfg.api_key else {}
        try:
            r = httpx.post(
                f"{self.cfg.base_url.rstrip('/')}/v1/chat/completions",
                headers=headers, timeout=900,
                json={
                    "model": self.cfg.model or "default",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                },
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            return f"-- LEANLOOP frontier error: {e}\n"

    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract(text: str) -> str:
        for fence in ("```lean4", "```lean", "```"):
            if fence in text:
                body = text.split(fence, 1)[1].split("```", 1)[0]
                if "theorem" in body or "lemma" in body or ":= by" in body:
                    return body.strip() + "\n"
        return text.strip() + "\n"
