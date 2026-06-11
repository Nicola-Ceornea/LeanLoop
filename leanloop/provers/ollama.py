"""Local prover backend (Goedel-Prover-V2-8B by default), over an
Ollama-native or OpenAI-compatible HTTP API.

This is the pluggable boundary: point `base_url` at localhost for a
single-machine run, or at the GPU box (the 6950 XT) for remote inference.
Nothing else in the loop is aware of where the model runs.
"""
from __future__ import annotations

import asyncio

import httpx

from ..config import LocalProverConfig
from .base import Goal

# Goedel-Prover-V2 prompt (verbatim from the model card / founding doc). The
# {} is filled with the COMPLETE Lean file ending in `:= by sorry`.
GOEDEL_PROMPT = """Complete the following Lean 4 code:

```lean4
{statement}```

Before producing the Lean 4 code to formally prove the given theorem, provide a detailed proof plan outlining the main proof steps and strategies.
The plan should highlight key ideas, intermediate lemmas, and proof structures that will guide the construction of the final formal proof."""

# Appended when re-attempting after a failed compile (verifier-guided self-correction).
REPAIR_SUFFIX = """

Your previous attempt did not compile. The Lean compiler reported:
```
{errors}
```
Revise the proof to fix these errors. Output the complete corrected Lean file."""


class LocalProver:
    name = "local"

    def __init__(self, cfg: LocalProverConfig):
        self.cfg = cfg

    # ------------------------------------------------------------------ #
    def health(self, *, timeout: float = 8.0) -> tuple[bool, str]:
        """Quick liveness check of the prover endpoint + that the model exists.
        Returns (ok, message). Used by `check-prover`, `doctor`, and the loop's
        crash-resilience wait."""
        url = self.cfg.base_url.rstrip("/")
        try:
            if self.cfg.backend == "ollama":
                r = httpx.get(f"{url}/api/tags", timeout=timeout)
                r.raise_for_status()
                models = [m.get("name", "") for m in r.json().get("models", [])]
                if not any(self.cfg.model in m for m in models):
                    return False, f"reachable but model '{self.cfg.model}' not loaded (have: {models})"
                return True, f"reachable; model '{self.cfg.model}' present"
            r = httpx.get(f"{url}/v1/models", timeout=timeout)
            r.raise_for_status()
            return True, "reachable (openai-compatible)"
        except Exception as e:
            return False, f"unreachable: {e}"

    def running(self, *, timeout: float = 8.0) -> list[dict]:
        """Ollama GET /api/ps — currently-loaded models with VRAM split, so the
        doctor can report whether the model is actually on the GPU."""
        if self.cfg.backend != "ollama":
            return []
        try:
            r = httpx.get(f"{self.cfg.base_url.rstrip('/')}/api/ps", timeout=timeout)
            r.raise_for_status()
            return r.json().get("models", [])
        except Exception:
            return []

    # ------------------------------------------------------------------ #
    def generate_timed(self, prompt: str, *, temperature: float = 0.7) -> dict:
        """One generation with timing metrics, for `leanloop bench`. Ollama
        returns token counts + nanosecond durations; we derive tokens/sec.
        Returns {text, gen_toks, gen_tps, prompt_toks, prompt_tps, total_s}."""
        url = self.cfg.base_url.rstrip("/")
        with httpx.Client(timeout=self.cfg.timeout_s) as client:
            if self.cfg.backend == "ollama":
                r = client.post(f"{url}/api/generate", json={
                    "model": self.cfg.model, "prompt": prompt, "stream": False,
                    "options": {"temperature": temperature, "top_p": self.cfg.top_p,
                                "num_predict": self.cfg.max_tokens, "num_ctx": self.cfg.num_ctx},
                })
                r.raise_for_status()
                d = r.json()
                ev, evd = d.get("eval_count", 0), d.get("eval_duration", 0) or 1
                pv, pvd = d.get("prompt_eval_count", 0), d.get("prompt_eval_duration", 0) or 1
                return {
                    "text": d.get("response", ""),
                    "gen_toks": ev, "gen_tps": ev / (evd / 1e9),
                    "prompt_toks": pv, "prompt_tps": pv / (pvd / 1e9),
                    "total_s": (d.get("total_duration", 0) or 0) / 1e9,
                }
            # openai-compatible: usage if present, else just wall time
            import time as _t
            t0 = _t.time()
            headers = {"Authorization": f"Bearer {self.cfg.api_key}"} if self.cfg.api_key else {}
            r = client.post(f"{url}/v1/chat/completions", headers=headers, json={
                "model": self.cfg.model, "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature, "max_tokens": self.cfg.max_tokens})
            r.raise_for_status()
            d = r.json()
            dt = max(_t.time() - t0, 1e-6)
            ct = d.get("usage", {}).get("completion_tokens", 0)
            return {"text": d["choices"][0]["message"]["content"], "gen_toks": ct,
                    "gen_tps": ct / dt if ct else 0.0, "prompt_toks":
                    d.get("usage", {}).get("prompt_tokens", 0), "prompt_tps": 0.0, "total_s": dt}

    # ------------------------------------------------------------------ #
    def propose(self, goal: Goal, *, feedback: str = "") -> list[str]:
        """pass@N: draw `samples` candidate files, cycling temperatures."""
        return asyncio.run(self._propose_async(goal, feedback))

    async def _propose_async(self, goal: Goal, feedback: str) -> list[str]:
        prompt = self._build_prompt(goal, feedback)
        sem = asyncio.Semaphore(self.cfg.concurrency)
        temps = self.cfg.temperatures or [0.7]

        async with httpx.AsyncClient(timeout=self.cfg.timeout_s) as client:
            async def one(i: int) -> str | None:
                async with sem:
                    temp = temps[i % len(temps)]
                    try:
                        return await self._generate(client, prompt, temp)
                    except Exception as e:  # network/timeout: skip this sample
                        return f"-- LEANLOOP sample error: {e}\n"
            results = await asyncio.gather(*(one(i) for i in range(self.cfg.samples)))
        return [self._extract_lean(r) for r in results if r]

    # ------------------------------------------------------------------ #
    def _build_prompt(self, goal: Goal, feedback: str) -> str:
        statement = goal.file_text
        if goal.context:
            statement = f"-- Available context / proof plan:\n{goal.context}\n\n{statement}"
        prompt = GOEDEL_PROMPT.format(statement=statement)
        if feedback:
            prompt += REPAIR_SUFFIX.format(errors=feedback[:4000])
        return prompt

    async def _generate(self, client: httpx.AsyncClient, prompt: str, temp: float) -> str:
        if self.cfg.backend == "ollama":
            r = await client.post(
                f"{self.cfg.base_url.rstrip('/')}/api/generate",
                json={
                    "model": self.cfg.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": temp,
                        "top_p": self.cfg.top_p,
                        "num_predict": self.cfg.max_tokens,
                        "num_ctx": self.cfg.num_ctx,
                    },
                },
            )
            r.raise_for_status()
            return r.json().get("response", "")
        # openai-compatible (llama.cpp server, vLLM, ...)
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"} if self.cfg.api_key else {}
        r = await client.post(
            f"{self.cfg.base_url.rstrip('/')}/v1/chat/completions",
            headers=headers,
            json={
                "model": self.cfg.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temp,
                "top_p": self.cfg.top_p,
                "max_tokens": self.cfg.max_tokens,
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_lean(text: str) -> str:
        """Pull the Lean file out of the model output. Provers wrap proofs in a
        ```lean4 ... ``` fence; fall back to the raw text."""
        for fence in ("```lean4", "```lean", "```"):
            if fence in text:
                after = text.split(fence, 1)[1]
                body = after.split("```", 1)[0]
                if "theorem" in body or "lemma" in body or ":= by" in body:
                    return body.strip() + "\n"
        return text.strip() + "\n"
