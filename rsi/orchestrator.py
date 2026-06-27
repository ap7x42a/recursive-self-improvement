from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .candidates import CandidateResult, remove_candidate, run_candidate_batch
from .config import RunConfig, TestSpec, dedupe_tests
from .evaluator import BaselineEvaluation, establish_baseline
from .models import ModelClient
from .promotion import promote_candidate
from .receipts import EventLog, atomic_write_json, utc_now
from .testing import discover_tests
from .tools import file_manifest, manifest_digest
from . import __version__


def _default_run_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(tempfile.gettempdir()) / "recursive-self-improvement" / f"{stamp}-{uuid.uuid4().hex[:8]}"


def _within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _best_eligible(results: list[CandidateResult]) -> CandidateResult | None:
    eligible = [item for item in results if item.evaluation is not None and item.evaluation.eligible]
    if not eligible:
        return None
    return max(eligible, key=lambda item: item.evaluation.score if item.evaluation else ())


def _all_fixed_point(results: list[CandidateResult]) -> bool:
    completed = [item for item in results if item.agent.completed]
    return bool(completed) and all(
        item.agent.done and item.evaluation is not None and not item.evaluation.changed
        for item in completed
    )


def _candidate_summary(result: CandidateResult) -> dict[str, Any]:
    data = result.to_dict()
    # Candidate paths can be removed after evaluation; the receipt still records identity.
    return data


def _write_receipt(run_dir: Path, receipt: dict[str, Any], receipt_out: str = "") -> None:
    atomic_write_json(run_dir / "receipt.json", receipt)
    if receipt_out:
        atomic_write_json(Path(receipt_out).expanduser().resolve(), receipt)


def run_recursive_improvement(
    workspace: Path,
    config: RunConfig,
    *,
    client_factory: Callable[[], ModelClient] | None = None,
) -> dict[str, Any]:
    config.validate()
    workspace = workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError(f"workspace is not a directory: {workspace}")
    run_dir = Path(config.run_dir).expanduser().resolve() if config.run_dir else _default_run_dir()
    if _within(run_dir, workspace):
        raise ValueError("run_dir must be outside the mutable workspace")
    run_dir.mkdir(parents=True, exist_ok=True)
    log = EventLog(run_dir / "events.jsonl", verbose=config.verbose)

    trusted_specs: list[TestSpec] = list(config.tests)
    if config.auto_discover_tests:
        trusted_specs.extend(discover_tests(workspace))
    trusted_specs = dedupe_tests(trusted_specs)

    initial_manifest = file_manifest(workspace, config.snapshot.excludes)
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "engine_version": __version__,
        "started_at": utc_now(),
        "finished_at": None,
        "workspace": str(workspace),
        "run_dir": str(run_dir),
        "status": "running",
        "stop_reason": "",
        "strategy": config.search.strategy,
        "objective": config.objective,
        "config": config.to_dict(),
        "trusted_tests": [asdict(item) for item in trusted_specs],
        "score_definition": [
            "baseline_failures_repaired",
            "trusted_metric_gain",
            "trusted_tests_passing",
            "negative_changed_lines",
            "negative_changed_files",
            "stable_candidate_tiebreak",
        ],
        "model_proposed_tests_score_credit": False,
        "initial_manifest": manifest_digest(initial_manifest),
        "final_manifest": None,
        "accepted_generations": 0,
        "rolled_back_promotions": 0,
        "generations": [],
    }
    log.emit(
        "run_start",
        workspace=str(workspace),
        run_dir=str(run_dir),
        strategy=config.search.strategy,
        trusted_tests=[asdict(item) for item in trusted_specs],
        config=config.to_dict(),
    )
    _write_receipt(run_dir, receipt, config.receipt_out)

    prior_iterations: list[dict[str, Any]] = []
    stale_generations = 0
    generation = 0
    stop_reason = ""
    fatal_error = ""

    try:
        while True:
            if config.search.max_generations and generation >= config.search.max_generations:
                stop_reason = "generation_budget"
                break
            generation += 1
            baseline = establish_baseline(workspace, config, trusted_specs)
            generation_record: dict[str, Any] = {
                "generation": generation,
                "started_at": utc_now(),
                "baseline_tests": [item.to_dict() for item in baseline.tests],
                "baseline_metrics": [item.to_dict() for item in baseline.metrics],
                "candidates": [],
                "selected_candidate": None,
                "promotion": None,
                "outcome": "",
            }
            log.emit(
                "generation_start",
                generation=generation,
                baseline_tests=generation_record["baseline_tests"],
                baseline_metrics=generation_record["baseline_metrics"],
            )

            results: list[CandidateResult] = []
            if config.search.strategy == "direct":
                results.extend(
                    run_candidate_batch(
                        workspace=workspace,
                        run_dir=run_dir,
                        generation=generation,
                        start_index=0,
                        count=1,
                        baseline=baseline,
                        trusted_specs=trusted_specs,
                        prior_iterations=prior_iterations,
                        config=config,
                        log=log,
                        client_factory=client_factory,
                    )
                )
            elif config.search.strategy == "best-of-n":
                results.extend(
                    run_candidate_batch(
                        workspace=workspace,
                        run_dir=run_dir,
                        generation=generation,
                        start_index=0,
                        count=config.search.candidates,
                        baseline=baseline,
                        trusted_specs=trusted_specs,
                        prior_iterations=prior_iterations,
                        config=config,
                        log=log,
                        client_factory=client_factory,
                    )
                )
            else:
                total = 0
                target = min(config.search.initial_candidates, config.search.max_candidates)
                while total < config.search.max_candidates:
                    batch_count = min(target - total, config.search.max_candidates - total)
                    if batch_count <= 0:
                        batch_count = min(max(1, total), config.search.max_candidates - total)
                    results.extend(
                        run_candidate_batch(
                            workspace=workspace,
                            run_dir=run_dir,
                            generation=generation,
                            start_index=total,
                            count=batch_count,
                            baseline=baseline,
                            trusted_specs=trusted_specs,
                            prior_iterations=prior_iterations,
                            config=config,
                            log=log,
                            client_factory=client_factory,
                        )
                    )
                    total += batch_count
                    current_best = _best_eligible(results)
                    if current_best and current_best.evaluation and current_best.evaluation.measurable_improvement:
                        log.emit(
                            "adaptive_early_success",
                            generation=generation,
                            candidate=current_best.candidate_id,
                            evaluated_candidates=total,
                        )
                        break
                    if total >= config.search.max_candidates:
                        break
                    target = min(config.search.max_candidates, max(total * 2, total + 1))

            generation_record["candidates"] = [_candidate_summary(item) for item in results]
            winner = _best_eligible(results)
            if winner is None:
                if _all_fixed_point(results) and not config.search.ignore_done:
                    generation_record["outcome"] = "model_fixed_point_no_changes"
                    stop_reason = "model_fixed_point"
                else:
                    generation_record["outcome"] = "no_eligible_candidate"
                    stale_generations += 1
                    if config.search.patience and stale_generations >= config.search.patience:
                        stop_reason = "patience"
                generation_record["finished_at"] = utc_now()
                receipt["generations"].append(generation_record)
                _write_receipt(run_dir, receipt, config.receipt_out)
                if not config.snapshot.keep_candidates:
                    for result in results:
                        remove_candidate(result)
                if stop_reason:
                    break
                continue

            generation_record["selected_candidate"] = winner.candidate_id
            promotion = promote_candidate(
                workspace=workspace,
                candidate=winner,
                generation=generation,
                run_dir=run_dir,
                baseline=baseline,
                trusted_specs=trusted_specs,
                config=config,
                log=log,
            )
            generation_record["promotion"] = promotion.to_dict()
            if promotion.accepted:
                receipt["accepted_generations"] += 1
                generation_record["outcome"] = "accepted"
                evaluation = winner.evaluation
                measurable = bool(evaluation and evaluation.measurable_improvement)
                stale_generations = 0 if measurable else stale_generations + 1
                prior_iterations.append(
                    {
                        "generation": generation,
                        "candidate": winner.candidate_id,
                        "summary": winner.agent.summary,
                        "evaluation": evaluation.to_dict() if evaluation else None,
                    }
                )
                if winner.agent.done and not config.search.ignore_done:
                    stop_reason = "model_fixed_point"
                elif config.search.patience and stale_generations >= config.search.patience:
                    stop_reason = "patience"
            else:
                generation_record["outcome"] = "promotion_rejected"
                if promotion.rolled_back:
                    receipt["rolled_back_promotions"] += 1
                stale_generations += 1
                if config.search.patience and stale_generations >= config.search.patience:
                    stop_reason = "patience"

            generation_record["finished_at"] = utc_now()
            receipt["generations"].append(generation_record)
            _write_receipt(run_dir, receipt, config.receipt_out)
            if not config.snapshot.keep_candidates:
                for result in results:
                    remove_candidate(result)
            if stop_reason:
                break
    except KeyboardInterrupt:
        stop_reason = "interrupted"
    except Exception as exc:
        stop_reason = "error"
        fatal_error = str(exc)
        log.emit("run_error", error=str(exc))

    final_manifest = file_manifest(workspace, config.snapshot.excludes)
    receipt["finished_at"] = utc_now()
    receipt["status"] = "error" if stop_reason == "error" else "completed"
    receipt["stop_reason"] = stop_reason or "unknown"
    receipt["error"] = fatal_error
    receipt["final_manifest"] = manifest_digest(final_manifest)
    receipt["filesystem_changed"] = receipt["initial_manifest"] != receipt["final_manifest"]
    _write_receipt(run_dir, receipt, config.receipt_out)
    log.emit(
        "run_finish",
        status=receipt["status"],
        stop_reason=receipt["stop_reason"],
        final_manifest=receipt["final_manifest"],
        accepted_generations=receipt["accepted_generations"],
    )
    return receipt
