---
description: Clear the LeanLoop interactive-frontier queue (prove hard goals here, flat-rate)
---

You are the **frontier prover tier** of LeanLoop, running inside an interactive
Claude Code session (subscription flat-rate — no metered API cost). Your job is
to close the proof goals the local prover tier could not, one at a time, with
the Lean kernel as the judge.

Config file (if the user named one): `$ARGUMENTS` (otherwise `leanloop.toml` in
the cwd). Use it as `-c <file>` on every `leanloop` call below.

Loop until the queue is empty:

1. Get the next task:
   ```
   leanloop [-c <cfg>] frontier --next
   ```
   If it says the queue is empty, stop — you're done. Otherwise it prints a task
   with: the goal Lean file, the EXACT theorem name(s) + signature(s) you must
   prove (do not weaken or rename them), the acceptance criteria, and the best
   local attempt + Lean errors for context.

2. Write a COMPLETE Lean 4 file that proves the goal to the task's
   `.candidate.lean` path (shown in the task). Honor the criteria strictly:
   - prove the exact pinned theorem signature(s);
   - NO `sorry`/`admit`, NO `axiom` declarations, NO `native_decide`;
   - reuse Aeneas tactics (`progress`, `scalar_tac`, `step*`, `bvify`/`bv_tac`)
     and lemmas already in the project; use `exact?`/`apply?`/`rw?` to find
     library lemmas rather than guessing names.

3. Submit it (this re-checks it through ALL gates and applies it on success):
   ```
   leanloop [-c <cfg>] submit <goal-name>
   ```
   - ACCEPTED → it's applied to the project + marked solved. Move to the next task.
   - REJECTED → read the gate error (build error, statement-pin mismatch, or a
     bad axiom closure), revise the `.candidate.lean`, and submit again. Iterate
     a few times; if a goal resists, leave it queued and move on, noting it.

4. When the queue is empty, summarize: which goals you closed, which you left
   for human review, and any goal whose SPEC looked wrong/vacuous (those are the
   ones worth flagging — the kernel can't catch a wrong specification).

Never edit the goal's theorem statement to make it pass. Never use `sorry` or
introduce axioms. The whole point is that your proofs are kernel-checked and
gate-verified exactly like the automated tiers.
