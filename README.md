# Recursive Self-Improvement

A Codex skill for same-session recursive improvement. The agent that loads the skill performs the improvement loop with its already exposed tools. This package must not launch a fresh Claude Code or Codex session.

## Contract

Forbidden active paths:

- `claude`, `claude -p`, or Claude Code subprocesses
- `codex exec` or nested Codex subprocesses
- autonomous use of `scripts/cli_model_adapter.py`
- autonomous use of `scripts/recursive_self_improve.py`
- `scripts/supervisor.py` as a cross-generation child-process controller

Those launcher surfaces are retained only as inert legacy compatibility files or historical reference points. The active skill is `SKILL.md`.

## Same-Session Loop

```text
state objective and trusted checks
→ capture baseline state
→ make one focused candidate change in the current session
→ run trusted tests/metrics
→ reject regressions, protected-path mutations, and no-diff candidates
→ keep promoted edits only after real-workspace verification
→ record evidence and next generation target
```

Best-of-N can be done as a reasoning exercise by the current agent. Isolated worktrees or subagents are allowed only when the user explicitly authorizes them for the current task; they do not replace current-session promotion and verification.

## Install

```bash
./install.sh /path/to/skills
```

This creates:

```text
/path/to/skills/recursive-self-improvement
```

No third-party Python package is required.

## Test

```bash
python3 scripts/self_test.py
```

The suite verifies evaluator behavior, rollback behavior, adaptive selection with an in-process deterministic model stub, and the no-child-session contract.

## Status of the Python Package

The `rsi/` modules are kept because they contain useful deterministic evaluator, diff, rollback, and scoring experiments. They are not the active Codex runtime contract. Any path that would launch a child model session now fails closed and points back to `SKILL.md`.
