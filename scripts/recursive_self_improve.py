#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from rsi import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Same-session recursive self-improvement skill helper.",
    )
    parser.add_argument("workspace", nargs="?", help="Mutable workspace; defaults to this skill directory")
    parser.add_argument("--config", type=Path, help="JSON configuration file")
    parser.add_argument("--objective")
    parser.add_argument("--provider", choices=["same-session"])
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def apply_overrides(config, args: argparse.Namespace) -> None:
    return None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    parser.exit(
        2,
        "error: recursive-self-improvement is a same-session Codex skill. "
        "Do not run this script as an autonomous controller or child-agent launcher; "
        "load SKILL.md and perform the loop in the current agent session.\n",
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
