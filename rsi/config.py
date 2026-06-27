from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


DEFAULT_EXCLUDES = [
    ".git",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".recursive-self-improvement",
]


def _default_cli_adapter() -> str:
    """No default child-session adapter exists for the same-session skill."""
    return ""


@dataclass(slots=True)
class ModelConfig:
    # Same-session only. The current agent that loaded SKILL.md is the controller.
    # The command field is retained for config compatibility but must stay empty.
    provider: str = "same-session"
    model: str = ""
    command: str = field(default_factory=_default_cli_adapter)
    timeout: float = 600.0
    temperature: float = 0.1
    max_tokens: int = 8192


@dataclass(slots=True, frozen=True)
class TestSpec:
    name: str
    command: str
    timeout: float | None = None


@dataclass(slots=True, frozen=True)
class MetricSpec:
    name: str
    command: str
    direction: str = "higher"
    weight: float = 1.0
    timeout: float | None = None

    def __post_init__(self) -> None:
        if self.direction not in {"higher", "lower"}:
            raise ValueError(f"metric direction must be 'higher' or 'lower': {self.direction}")


@dataclass(slots=True)
class LimitsConfig:
    inventory_depth: int = 3
    inventory_entries: int = 0
    read_bytes: int = 0
    tool_output_bytes: int = 0
    search_matches: int = 0
    conversation_chars: int = 0


@dataclass(slots=True)
class SearchConfig:
    strategy: str = "adaptive"
    initial_candidates: int = 1
    max_candidates: int = 8
    candidates: int = 4
    parallel_candidates: int = 4
    max_generations: int = 8
    patience: int = 3
    max_actions: int = 80
    max_invalid_responses: int = 4
    ignore_done: bool = False
    require_measurable_improvement: bool = False

    def validate(self) -> None:
        if self.strategy not in {"direct", "best-of-n", "adaptive"}:
            raise ValueError(f"unknown strategy: {self.strategy}")
        for name in (
            "initial_candidates",
            "max_candidates",
            "candidates",
            "parallel_candidates",
            "max_actions",
            "max_invalid_responses",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")
        if self.max_generations < 0:
            raise ValueError("max_generations must be 0 (unlimited) or positive")
        if self.patience < 0:
            raise ValueError("patience cannot be negative")


@dataclass(slots=True)
class SnapshotConfig:
    mode: str = "reflink"
    excludes: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    keep_candidates: bool = False

    def validate(self) -> None:
        if self.mode not in {"copy", "reflink", "none"}:
            raise ValueError(f"unknown snapshot mode: {self.mode}")


@dataclass(slots=True)
class RunConfig:
    objective: str = (
        "Improve correctness, reliability, maintainability, and measured performance. "
        "Find concrete weaknesses, make focused changes, add regression tests where useful, "
        "and preserve working behavior."
    )
    model: ModelConfig = field(default_factory=ModelConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    snapshot: SnapshotConfig = field(default_factory=SnapshotConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    tests: list[TestSpec] = field(default_factory=list)
    metrics: list[MetricSpec] = field(default_factory=list)
    protected_paths: list[str] = field(default_factory=list)
    auto_discover_tests: bool = True
    test_timeout: float = 900.0
    command_timeout: float = 900.0
    run_dir: str = ""
    receipt_out: str = ""
    verbose: bool = False

    def validate(self) -> None:
        self.search.validate()
        self.snapshot.validate()
        if self.model.provider != "same-session":
            raise ValueError(
                "model.provider must be 'same-session'. This Codex skill must not launch "
                f"child Claude/Codex sessions or external model adapters: {self.model.provider}"
            )
        if self.model.command:
            raise ValueError("model.command must be empty; child-session model adapters are forbidden")
        if self.test_timeout <= 0 or self.command_timeout <= 0 or self.model.timeout <= 0:
            raise ValueError("timeouts must be positive")
        for field_name in (
            "inventory_depth",
            "inventory_entries",
            "read_bytes",
            "tool_output_bytes",
            "search_matches",
            "conversation_chars",
        ):
            if getattr(self.limits, field_name) < 0:
                raise ValueError(f"limits.{field_name} cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _merge_dataclass(obj: Any, updates: dict[str, Any]) -> Any:
    for key, value in updates.items():
        if not hasattr(obj, key):
            raise ValueError(f"unknown configuration key: {key}")
        current = getattr(obj, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _merge_dataclass(current, value)
        elif key == "tests":
            setattr(obj, key, [parse_test_object(item) for item in value])
        elif key == "metrics":
            setattr(obj, key, [parse_metric_object(item) for item in value])
        else:
            setattr(obj, key, value)
    return obj


def load_config(path: Path | None) -> RunConfig:
    config = RunConfig()
    if path is not None:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("configuration root must be an object")
        _merge_dataclass(config, data)
    config.validate()
    return config


def parse_test_object(value: Any) -> TestSpec:
    if isinstance(value, str):
        return parse_test_cli(value)
    if not isinstance(value, dict):
        raise ValueError(f"invalid test specification: {value!r}")
    return TestSpec(
        name=str(value.get("name") or value.get("command") or "test"),
        command=str(value["command"]),
        timeout=float(value["timeout"]) if value.get("timeout") is not None else None,
    )


def parse_metric_object(value: Any) -> MetricSpec:
    if isinstance(value, str):
        return parse_metric_cli(value)
    if not isinstance(value, dict):
        raise ValueError(f"invalid metric specification: {value!r}")
    return MetricSpec(
        name=str(value.get("name") or value.get("command") or "metric"),
        command=str(value["command"]),
        direction=str(value.get("direction", "higher")),
        weight=float(value.get("weight", 1.0)),
        timeout=float(value["timeout"]) if value.get("timeout") is not None else None,
    )


def parse_test_cli(text: str) -> TestSpec:
    if "=" in text:
        name, command = text.split("=", 1)
        return TestSpec(name=name.strip() or command.strip(), command=command.strip())
    return TestSpec(name=text.strip(), command=text.strip())


def parse_metric_cli(text: str) -> MetricSpec:
    # NAME:DIRECTION:COMMAND; COMMAND may contain additional colons.
    parts = text.split(":", 2)
    if len(parts) != 3:
        raise ValueError("metric syntax is NAME:DIRECTION:COMMAND")
    name, direction, command = parts
    return MetricSpec(name=name.strip(), direction=direction.strip(), command=command.strip())


def dedupe_tests(specs: Iterable[TestSpec]) -> list[TestSpec]:
    seen: set[tuple[str, str]] = set()
    result: list[TestSpec] = []
    for spec in specs:
        key = (spec.name, spec.command)
        if key not in seen:
            seen.add(key)
            result.append(spec)
    return result
