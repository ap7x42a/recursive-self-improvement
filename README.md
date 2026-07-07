# Recursive Self-Improvement

Recursive Self-Improvement is an agent skill for improving code, prompts, tests,
or skills inside the current loaded session. The same agent that loads the skill
acts as controller, candidate generator, evaluator, and promoter.

It is intentionally conservative: no child model process gets to generate,
evaluate, and promote its own work.

## Hard Boundary

Do not launch a fresh Claude Code, Codex, or other model session from this
skill. These paths are forbidden as active improvement loops:

- `claude` or `claude -p`
- `codex exec` or nested Codex subprocesses
- `scripts/cli_model_adapter.py`
- `scripts/recursive_self_improve.py` as an autonomous controller
- `scripts/supervisor.py` as a cross-generation process controller

The Python controller files remain in the package as reference and regression
fixtures. The active public contract is `SKILL.md`: improve in the current
session and verify in the real workspace.

## Use It When

- A user asks for iterative improvement rather than a one-shot edit.
- A prompt, skill, benchmark, or test needs one measurable improvement at a
  time.
- Several candidate fixes are possible, but only verified workspace changes
  should survive.
- A previous "self-improvement" loop relied on process transcripts, model
  claims, or no-diff scores instead of actual changes and checks.

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

Protected paths matter. Before editing, name the evaluator assets that candidate
changes must not mutate: scoring scripts, benchmark fixtures, gate tests,
validation scripts, or any other surface that would let the candidate grade
itself.

## What The Package Includes

- `SKILL.md` - the active same-session discipline.
- `references/protocol.md` - longer protocol notes.
- `rsi/` - deterministic evaluator, diff, rollback, and scoring experiments
  retained as reference material.
- `scripts/self_test.py` - regression suite for evaluator behavior, rollback,
  adaptive selection, and the no-child-session boundary.
- `install.sh` - copies the package into a chosen skill directory.

## Install As An Agent Skill

```bash
git clone https://github.com/ap7x42a/recursive-self-improvement.git
cd recursive-self-improvement
./install.sh ~/.codex/skills
```

The install script creates:

```text
~/.codex/skills/recursive-self-improvement
```

No third-party Python package is required.

## Verify The Package

```bash
python3 scripts/self_test.py
sha256sum -c SHA256SUMS.txt
```

## Limits

This package does not provide an autonomous research agent or background
controller. It is a discipline for one loaded agent to make one verifiable
improvement at a time, reject weak candidates, and keep only changes backed by
trusted checks.
