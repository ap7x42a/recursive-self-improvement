from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .agent import AgentResult
from .config import MetricSpec, RunConfig, TestSpec
from .testing import MetricResult, TestResult, run_metrics, run_tests
from .tools import FileState, changed_line_estimate, diff_manifests, file_manifest


@dataclass(slots=True)
class BaselineEvaluation:
    tests: list[TestResult]
    metrics: list[MetricResult]
    manifest: dict[str, FileState]


@dataclass(slots=True)
class CandidateEvaluation:
    eligible: bool
    changed: bool
    measurable_improvement: bool
    repaired_tests: list[str]
    regressions: list[str]
    proposed_test_failures: list[str]
    invalid_metrics: list[str]
    protected_changes: list[str]
    metric_gain: float
    trusted_passes: int
    changed_files: int
    changed_lines: int
    score: tuple[float, ...]
    trusted_tests: list[TestResult] = field(default_factory=list)
    trusted_metrics: list[MetricResult] = field(default_factory=list)
    proposed_tests: list[TestResult] = field(default_factory=list)
    diff: dict[str, list[str]] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "changed": self.changed,
            "measurable_improvement": self.measurable_improvement,
            "repaired_tests": self.repaired_tests,
            "regressions": self.regressions,
            "proposed_test_failures": self.proposed_test_failures,
            "invalid_metrics": self.invalid_metrics,
            "protected_changes": self.protected_changes,
            "metric_gain": self.metric_gain,
            "trusted_passes": self.trusted_passes,
            "changed_files": self.changed_files,
            "changed_lines": self.changed_lines,
            "score": list(self.score),
            "trusted_tests": [item.to_dict() for item in self.trusted_tests],
            "trusted_metrics": [item.to_dict() for item in self.trusted_metrics],
            "proposed_tests": [item.to_dict() for item in self.proposed_tests],
            "diff": self.diff,
            "reasons": self.reasons,
        }


def establish_baseline(workspace: Path, config: RunConfig, tests: list[TestSpec]) -> BaselineEvaluation:
    return BaselineEvaluation(
        tests=run_tests(workspace, tests, default_timeout=config.test_timeout),
        metrics=run_metrics(workspace, config.metrics, default_timeout=config.test_timeout),
        manifest=file_manifest(workspace, config.snapshot.excludes),
    )


def _key(result: TestResult) -> tuple[str, str]:
    return result.name, result.command


def _metric_key(result: MetricResult) -> tuple[str, str]:
    return result.name, result.command


def _protected_changes(diff: dict[str, list[str]], protected: list[str]) -> list[str]:
    normalized = [Path(item).as_posix().strip("/") for item in protected]
    changed = diff["created"] + diff["modified"] + diff["deleted"]
    result: list[str] = []
    for path in changed:
        clean = path.rstrip("/")
        for prefix in normalized:
            if clean == prefix or clean.startswith(prefix.rstrip("/") + "/"):
                result.append(path)
                break
    return sorted(set(result))


def evaluate_candidate(
    *,
    baseline_root: Path,
    candidate_root: Path,
    baseline: BaselineEvaluation,
    agent_result: AgentResult,
    trusted_specs: list[TestSpec],
    config: RunConfig,
    candidate_index: int,
) -> CandidateEvaluation:
    trusted = run_tests(candidate_root, trusted_specs, default_timeout=config.test_timeout)
    metrics = run_metrics(candidate_root, config.metrics, default_timeout=config.test_timeout)
    proposed = run_tests(candidate_root, agent_result.proposed_tests, default_timeout=config.test_timeout)
    candidate_manifest = file_manifest(candidate_root, config.snapshot.excludes)
    diff = diff_manifests(baseline.manifest, candidate_manifest)
    changed = any(diff.values())

    baseline_tests = {_key(item): item for item in baseline.tests}
    candidate_tests = {_key(item): item for item in trusted}
    repaired: list[str] = []
    regressions: list[str] = []
    for key, prior in baseline_tests.items():
        current = candidate_tests.get(key)
        label = prior.name
        if prior.passed and (current is None or not current.passed):
            regressions.append(label)
        elif not prior.passed and current is not None and current.passed:
            repaired.append(label)

    proposed_failures = [item.name for item in proposed if not item.passed]
    baseline_metrics = {_metric_key(item): item for item in baseline.metrics}
    candidate_metrics = {_metric_key(item): item for item in metrics}
    metric_gain = 0.0
    invalid_metrics: list[str] = []
    for key, prior in baseline_metrics.items():
        current = candidate_metrics.get(key)
        if prior.valid:
            if current is None or not current.valid or current.value is None or prior.value is None:
                invalid_metrics.append(prior.name)
                continue
            direction = 1.0 if prior.direction == "higher" else -1.0
            metric_gain += direction * (current.value - prior.value) * prior.weight
        elif current is not None and current.valid:
            # A previously unusable trusted metric becoming usable is evidence, but
            # it does not receive an arbitrary numeric bonus.
            pass

    protected_changes = _protected_changes(diff, config.protected_paths)
    changed_files = len(diff["created"]) + len(diff["modified"]) + len(diff["deleted"])
    changed_lines = changed_line_estimate(baseline_root, candidate_root, diff)
    trusted_passes = sum(item.passed for item in trusted)
    measurable = bool(repaired) or metric_gain > 1e-12
    reasons: list[str] = []
    if not agent_result.completed:
        reasons.append(f"agent did not complete: {agent_result.error}")
    if not changed:
        reasons.append("candidate made no filesystem changes")
    if regressions:
        reasons.append("previously passing trusted tests regressed")
    if proposed_failures:
        reasons.append("model-proposed candidate obligations failed")
    if invalid_metrics:
        reasons.append("previously valid trusted metrics became invalid")
    if protected_changes:
        reasons.append("candidate changed protected evaluator paths")
    if config.search.require_measurable_improvement and not measurable:
        reasons.append("configuration requires measurable improvement")

    eligible = (
        agent_result.completed
        and changed
        and not regressions
        and not proposed_failures
        and not invalid_metrics
        and not protected_changes
        and (measurable or not config.search.require_measurable_improvement)
    )

    # Model-proposed tests are deliberately absent from this score. They are only
    # obligations. The final components prefer a focused diff when evidence ties.
    score: tuple[float, ...] = (
        float(len(repaired)),
        float(metric_gain),
        float(trusted_passes),
        float(-changed_lines),
        float(-changed_files),
        float(-candidate_index),
    )
    return CandidateEvaluation(
        eligible=eligible,
        changed=changed,
        measurable_improvement=measurable,
        repaired_tests=repaired,
        regressions=regressions,
        proposed_test_failures=proposed_failures,
        invalid_metrics=invalid_metrics,
        protected_changes=protected_changes,
        metric_gain=metric_gain,
        trusted_passes=trusted_passes,
        changed_files=changed_files,
        changed_lines=changed_lines,
        score=score,
        trusted_tests=trusted,
        trusted_metrics=metrics,
        proposed_tests=proposed,
        diff=diff,
        reasons=reasons,
    )
