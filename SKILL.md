---
name: recursive-self-improvement
description: Use when the current agent is asked to iteratively improve code, prompts, tests, or skill behavior in the same loaded session without spawning child Claude or Codex sessions.
metadata:
  version: 4.1.0
  self_test: scripts/self_test.py
---

# Recursive Self-Improvement

This is a same-session improvement discipline. The agent that loads this skill is the controller, candidate generator, evaluator, and promoter. It must use only the tools already exposed in the current session.

## Hard Boundary

Never launch a child LLM session from this skill.

Forbidden:
- `claude`, `claude -p`, or Claude Code subprocesses
- `codex exec` or nested Codex subprocesses
- `scripts/cli_model_adapter.py`
- `scripts/recursive_self_improve.py` as an autonomous controller
- background helper workers unless the user explicitly authorizes them for the current task

If a repo copy of this skill still advertises child-session control, treat that copy as stale and repair it before use.

## Same-Session Loop

For each generation:

1. State the concrete objective and trusted checks.
2. Snapshot current state with `git status --short --branch`, relevant tests, and any metric command.
3. Produce one focused candidate change yourself in the current session.
4. Run trusted checks. Model-proposed checks are obligations only; they do not prove score gain.
5. Reject the candidate if it regresses a previously passing trusted check, mutates protected evaluator paths, or lacks a real diff.
6. Promote by keeping the edits only after trusted checks pass in the real workspace.
7. Record what changed, what proved it, and the next generation target.

Use best-of-N only as an internal reasoning exercise unless isolated worktrees or subagents were explicitly authorized for this task. Even then, promotion still happens through the current agent after real-workspace verification.

## Protected Paths

Before editing, name evaluator assets that candidates must not change: test harnesses, scoring scripts, benchmark fixtures, skill validation scripts, and runtime gates. If a candidate needs to touch those, stop and justify that as a separate repair.

## Verification

Completion requires fresh evidence:
- baseline command output or known failing symptom
- final trusted command output
- diff summary for promoted edits
- rollback or rejection note for failed candidates

Do not report “recursive improvement completed” from process state, receipt files, or a child agent transcript. Only current-session edits plus current-session verification count.

## Bundled Files

The Python package contains evaluator and rollback experiments from the original standalone controller. They are reference material only unless the current task explicitly asks to repair that package. The active Codex skill is this same-session procedure.
