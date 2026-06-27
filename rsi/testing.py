from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .config import MetricSpec, TestSpec, dedupe_tests


@dataclass(slots=True)
class TestResult:
    name: str
    command: str
    passed: bool
    returncode: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class MetricResult:
    name: str
    command: str
    direction: str
    weight: float
    valid: bool
    value: float | None
    returncode: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _purge_python_bytecode(root: Path) -> None:
    for cache in root.rglob("__pycache__"):
        if cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)


def _run_command(root: Path, command: str, timeout: float) -> tuple[int | None, str, str, float, bool]:
    _purge_python_bytecode(root)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    start = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            shell=True,
            text=True,
            capture_output=True,
            env=env,
            timeout=timeout,
            check=False,
        )
        return (
            completed.returncode,
            completed.stdout,
            completed.stderr,
            time.monotonic() - start,
            False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return None, stdout, stderr, time.monotonic() - start, True


def run_tests(root: Path, specs: Iterable[TestSpec], *, default_timeout: float) -> list[TestResult]:
    """Run trusted tests. Per-test timeout overrides the configured default."""
    results: list[TestResult] = []
    for spec in specs:
        timeout = spec.timeout if spec.timeout is not None else default_timeout
        returncode, stdout, stderr, duration, timed_out = _run_command(root, spec.command, timeout)
        results.append(
            TestResult(
                name=spec.name,
                command=spec.command,
                passed=(returncode == 0 and not timed_out),
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=duration,
                timed_out=timed_out,
            )
        )
    return results


def _parse_metric_value(output: str) -> float:
    stripped = output.strip()
    if not stripped:
        raise ValueError("metric command produced no output")
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, (int, float)) and not isinstance(decoded, bool):
        value = float(decoded)
    elif isinstance(decoded, dict) and isinstance(decoded.get("score"), (int, float)):
        value = float(decoded["score"])
    else:
        match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", stripped)
        if not match:
            raise ValueError("metric output did not contain a number")
        value = float(match.group(0))
    if not math.isfinite(value):
        raise ValueError("metric value is not finite")
    return value


def run_metrics(root: Path, specs: Iterable[MetricSpec], *, default_timeout: float) -> list[MetricResult]:
    results: list[MetricResult] = []
    for spec in specs:
        timeout = spec.timeout if spec.timeout is not None else default_timeout
        returncode, stdout, stderr, duration, timed_out = _run_command(root, spec.command, timeout)
        error = ""
        value: float | None = None
        valid = False
        if timed_out:
            error = f"timed out after {timeout}s"
        elif returncode != 0:
            error = f"metric command exited {returncode}"
        else:
            try:
                value = _parse_metric_value(stdout)
                valid = True
            except ValueError as exc:
                error = str(exc)
        results.append(
            MetricResult(
                name=spec.name,
                command=spec.command,
                direction=spec.direction,
                weight=spec.weight,
                valid=valid,
                value=value,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=duration,
                error=error,
            )
        )
    return results


def discover_tests(root: Path) -> list[TestSpec]:
    specs: list[TestSpec] = []
    root_self_test = root / "scripts" / "self_test.py"
    if root_self_test.is_file():
        specs.append(TestSpec("self_test", "python3 scripts/self_test.py"))

    try:
        children = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        children = []
    for child in children:
        candidate = child / "scripts" / "self_test.py"
        if child.is_dir() and candidate.is_file():
            quoted = repr(str(candidate.relative_to(root)))
            specs.append(TestSpec(f"{child.name}:self_test", f"python3 {quoted}"))

    if (root / "pytest.ini").is_file() or (root / "conftest.py").is_file():
        specs.append(TestSpec("pytest", "python3 -m pytest -q"))
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            text = pyproject.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if "[tool.pytest" in text or "pytest" in text:
            specs.append(TestSpec("pytest", "python3 -m pytest -q"))

    package = root / "package.json"
    if package.is_file():
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
            scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
            if isinstance(scripts, dict) and scripts.get("test"):
                specs.append(TestSpec("npm-test", "npm test"))
        except (OSError, json.JSONDecodeError):
            pass
    return dedupe_tests(specs)
