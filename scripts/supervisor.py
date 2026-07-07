#!/usr/bin/env python3
"""Fail-closed legacy supervisor.

The original standalone controller restarted child generations in separate
processes. That is not acceptable for the Codex skill form: the loading agent is
the active session and must own the recursive-improvement loop directly.
"""
from __future__ import annotations

import sys


def main() -> int:
    print(
        "supervisor.py is disabled: recursive-self-improvement is a same-session "
        "skill and must not launch child controller/model sessions. Load SKILL.md "
        "and run the loop with the current agent tools.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
