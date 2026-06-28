# Recursive Self-Improvement

Recursive Self-Improvement is a Codex/agent skill for improving a codebase,
prompt, test, or skill in the current loaded session. The agent that loads the
skill is the controller, candidate generator, evaluator, and promoter.

It must not launch a fresh Claude Code or Codex session.

## Hard Boundary

Forbidden active paths:

- `claude`, `claude -p`, or Claude Code subprocesses
- `codex exec` or nested Codex subprocesses
- autonomous use of `scripts/cli_model_adapter.py`
- autonomous use of `scripts/recursive_self_improve.py`
- `scripts/supervisor.py` as a cross-generation child-process controller

Those launcher surfaces remain only as inert legacy compatibility files or
historical reference points. The active runtime contract is `SKILL.md`.

## Same-Session Loop

```text
state objective and trusted checks
-> capture baseline state
-> make one focused candidate change in the current session
-> run trusted tests or metrics
-> reject regressions, protected-path mutations, and no-diff candidates
-> keep promoted edits only after real-workspace verification
-> record evidence and the next generation target
```

Best-of-N can be done as a reasoning exercise by the current agent. Isolated
worktrees or subagents are allowed only when the user explicitly authorizes them
for the current task; they do not replace current-session promotion and
verification.

## How It Works With The Other Two Skills

These three skills are useful independently:

- `fable-method`: locks and verifies the engineering slice
- `evaluate-by-experiment`: tests a contestable claim with a falsifiable setup
- `recursive-self-improvement`: iterates one verified improvement in the
  current session without spawning child model sessions

They also compose well:

```text
Fable Method sets the target, non-goals, and proof gate.
Evaluate by Experiment tests whether the proposed improvement actually works.
Recursive Self-Improvement applies one verified improvement and repeats only
when another trusted check can measure the next step.
```

The composition is intentionally conservative: no child session can promote its
own work, no metric counts without a real check, and no iteration is successful
without a diff and verification in the current workspace.

## Install

```bash
./install.sh /path/to/skills
```

This creates:

```text
/path/to/skills/recursive-self-improvement
```

No third-party Python package is required.

## Verify

```bash
python3 scripts/self_test.py
sha256sum -c SHA256SUMS.txt
```

The suite verifies evaluator behavior, rollback behavior, adaptive selection
with an in-process deterministic model stub, and the no-child-session contract.

## Status Of The Python Package

The `rsi/` modules contain deterministic evaluator, diff, rollback, and scoring
experiments from the original standalone controller. They are reference material
unless the current task explicitly asks to repair that package. Any path that
would launch a child model session fails closed and points back to `SKILL.md`.
