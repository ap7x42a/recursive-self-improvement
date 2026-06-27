#!/usr/bin/env python3
"""Fail-closed legacy adapter.

recursive-self-improvement is a same-session Codex skill. It must not launch
Claude Code, Codex, or another LLM subprocess.
"""
from __future__ import annotations

import sys


def main() -> int:
    print(
        "cli_model_adapter.py is disabled: recursive-self-improvement must run in "
        "the current agent session, not through a child Claude/Codex process. "
        "Load SKILL.md and follow the same-session loop.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
