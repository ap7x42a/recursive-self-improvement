from __future__ import annotations

import shutil
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .candidates import CandidateResult
from .config import RunConfig, TestSpec
from .evaluator import BaselineEvaluation
from .receipts import EventLog
from .snapshots import clone_tree, mirror_tree, restore_tree
from .testing import MetricResult, TestResult, run_metrics, run_tests
from .tools import file_manifest, manifest_digest


@dataclass(slots=True)
class PromotionResult:
    accepted: bool
    rolled_back: bool
    error: str
    tests: list[TestResult]
    metrics: list[MetricResult]
    proposed_tests: list[TestResult]
    workspace_manifest: str
    snapshot: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "rolled_back": self.rolled_back,
            "error": self.error,
            "tests": [item.to_dict() for item in self.tests],
            "metrics": [item.to_dict() for item in self.metrics],
            "proposed_tests": [item.to_dict() for item in self.proposed_tests],
            "workspace_manifest": self.workspace_manifest,
            "snapshot": self.snapshot,
        }


def _promotion_gate(
    baseline: BaselineEvaluation,
    tests: list[TestResult],
    metrics: list[MetricResult],
    proposed: list[TestResult],
) -> tuple[bool, str]:
    current_tests = {(item.name, item.command): item for item in tests}
    regressions: list[str] = []
    for prior in baseline.tests:
        current = current_tests.get((prior.name, prior.command))
        if prior.passed and (current is None or not current.passed):
            regressions.append(prior.name)
    if regressions:
        return False, "post-promotion trusted test regressions: " + ", ".join(regressions)
    failed_proposed = [item.name for item in proposed if not item.passed]
    if failed_proposed:
        return False, "post-promotion candidate obligations failed: " + ", ".join(failed_proposed)
    current_metrics = {(item.name, item.command): item for item in metrics}
    invalid: list[str] = []
    for prior in baseline.metrics:
        current = current_metrics.get((prior.name, prior.command))
        if prior.valid and (current is None or not current.valid):
            invalid.append(prior.name)
    if invalid:
        return False, "post-promotion trusted metrics became invalid: " + ", ".join(invalid)
    return True, ""


def promote_candidate(
    *,
    workspace: Path,
    candidate: CandidateResult,
    generation: int,
    run_dir: Path,
    baseline: BaselineEvaluation,
    trusted_specs: list[TestSpec],
    config: RunConfig,
    log: EventLog,
) -> PromotionResult:
    snapshot = run_dir / "promotions" / f"generation-{generation:04d}-before"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot_created = False
    tests: list[TestResult] = []
    metrics: list[MetricResult] = []
    proposed: list[TestResult] = []
    try:
        if config.snapshot.mode != "none":
            clone_tree(
                workspace,
                snapshot,
                mode=config.snapshot.mode,
                excludes=config.snapshot.excludes,
            )
            snapshot_created = True
        mirror_tree(candidate.root, workspace, excludes=config.snapshot.excludes)
        tests = run_tests(workspace, trusted_specs, default_timeout=config.test_timeout)
        metrics = run_metrics(workspace, config.metrics, default_timeout=config.test_timeout)
        proposed = run_tests(
            workspace,
            candidate.agent.proposed_tests,
            default_timeout=config.test_timeout,
        )
        accepted, error = _promotion_gate(baseline, tests, metrics, proposed)
        rolled_back = False
        if not accepted:
            if snapshot_created:
                restore_tree(snapshot, workspace, excludes=config.snapshot.excludes)
                rolled_back = True
            else:
                error += "; rollback unavailable because snapshot mode is none"
        manifest = manifest_digest(file_manifest(workspace, config.snapshot.excludes))
        result = PromotionResult(
            accepted=accepted,
            rolled_back=rolled_back,
            error=error,
            tests=tests,
            metrics=metrics,
            proposed_tests=proposed,
            workspace_manifest=manifest,
            snapshot=str(snapshot) if snapshot_created else "",
        )
        log.emit(
            "promotion_result",
            generation=generation,
            candidate=candidate.candidate_id,
            result=result.to_dict(),
        )
        return result
    except Exception as exc:
        rolled_back = False
        rollback_error = ""
        if snapshot_created:
            try:
                restore_tree(snapshot, workspace, excludes=config.snapshot.excludes)
                rolled_back = True
            except Exception as restore_exc:
                rollback_error = f"; rollback also failed: {restore_exc}"
        manifest = manifest_digest(file_manifest(workspace, config.snapshot.excludes))
        error = f"promotion error: {exc}{rollback_error}"
        log.emit(
            "promotion_error",
            generation=generation,
            candidate=candidate.candidate_id,
            error=error,
            traceback=traceback.format_exc(),
        )
        return PromotionResult(
            accepted=False,
            rolled_back=rolled_back,
            error=error,
            tests=tests,
            metrics=metrics,
            proposed_tests=proposed,
            workspace_manifest=manifest,
            snapshot=str(snapshot) if snapshot_created else "",
        )
