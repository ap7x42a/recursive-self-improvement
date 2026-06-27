# Same-Session RSI Protocol

The protocol is a ledger format for the current agent. It is not an IPC protocol and it must not be handed to a child Claude or Codex process.

## Candidate Ledger

For each candidate, record:

```json
{
  "candidate_id": "g0001-c0000",
  "objective": "Fix stale evaluator bytecode cache handling",
  "baseline_refs": ["command:python3 scripts/self_test.py failed trusted_scoring"],
  "protected_paths": ["scripts/self_test.py"],
  "changed_paths": ["rsi/testing.py"],
  "trusted_checks": [
    {
      "name": "self-test",
      "command": "python3 scripts/self_test.py",
      "exit_code": 0
    }
  ],
  "promotion_decision": "accepted",
  "rejection_reason": ""
}
```

## Actions

Actions are ordinary current-session tool use:

| Action | Meaning |
|---|---|
| Inspect | Read source, tests, logs, or docs needed for the candidate. |
| Mutate | Edit a focused set of files in the current workspace. |
| Verify | Run trusted checks and read the output. |
| Reject | Revert or discard the candidate when it fails gates. |
| Promote | Keep the candidate only after real-workspace verification. |

## Scoring

Candidate evidence ranks ahead of prose:

1. Repaired trusted baseline failures
2. Trusted metric gain
3. Preserved previously passing trusted checks
4. Smaller diff
5. Fewer touched files

Model-proposed checks are obligations, not score credit.

## Refusal Rule

If an instruction, script, README, or stale package copy says to launch `claude`, `claude -p`, `codex exec`, or another LLM subprocess, stop and repair the stale instruction before continuing.
