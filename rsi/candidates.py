from __future__ import annotations

import shutil
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .agent import AgentResult, run_agent_iteration
from .config import RunConfig, TestSpec
from .evaluator import BaselineEvaluation, CandidateEvaluation, evaluate_candidate
from .models import ModelClient, create_model_client
from .receipts import EventLog
from .snapshots import clone_tree


@dataclass(slots=True)
class CandidateResult:
    candidate_id: str
    index: int
    root: Path
    agent: AgentResult
    evaluation: CandidateEvaluation | None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "index": self.index,
            "root": str(self.root),
            "agent": self.agent.to_dict(),
            "evaluation": self.evaluation.to_dict() if self.evaluation else None,
            "error": self.error,
        }


def run_candidate(
    *,
    workspace: Path,
    candidate_root: Path,
    candidate_id: str,
    index: int,
    generation: int,
    baseline: BaselineEvaluation,
    trusted_specs: list[TestSpec],
    prior_iterations: list[dict[str, Any]],
    config: RunConfig,
    log: EventLog,
    client_factory: Callable[[], ModelClient] | None = None,
) -> CandidateResult:
    try:
        clone_mode = config.snapshot.mode if config.snapshot.mode != "none" else "copy"
        clone_tree(
            workspace,
            candidate_root,
            mode=clone_mode,
            excludes=config.snapshot.excludes,
        )
        client = client_factory() if client_factory else create_model_client(config.model)
        agent = run_agent_iteration(
            workspace=candidate_root,
            client=client,
            config=config,
            candidate_id=candidate_id,
            generation=generation,
            trusted_tests=baseline.tests,
            trusted_metrics=baseline.metrics,
            prior_iterations=prior_iterations,
            log=log,
        )
        evaluation = evaluate_candidate(
            baseline_root=workspace,
            candidate_root=candidate_root,
            baseline=baseline,
            agent_result=agent,
            trusted_specs=trusted_specs,
            config=config,
            candidate_index=index,
        )
        result = CandidateResult(candidate_id, index, candidate_root, agent, evaluation)
        log.emit(
            "candidate_evaluated",
            generation=generation,
            candidate=candidate_id,
            result=result.to_dict(),
        )
        return result
    except Exception as exc:  # Candidate failures should not crash competing candidates.
        agent = AgentResult(False, "", False, error=str(exc))
        result = CandidateResult(candidate_id, index, candidate_root, agent, None, error=str(exc))
        log.emit(
            "candidate_error",
            generation=generation,
            candidate=candidate_id,
            error=str(exc),
            traceback=traceback.format_exc(),
        )
        return result


def run_candidate_batch(
    *,
    workspace: Path,
    run_dir: Path,
    generation: int,
    start_index: int,
    count: int,
    baseline: BaselineEvaluation,
    trusted_specs: list[TestSpec],
    prior_iterations: list[dict[str, Any]],
    config: RunConfig,
    log: EventLog,
    client_factory: Callable[[], ModelClient] | None = None,
) -> list[CandidateResult]:
    base = run_dir / "candidates"
    base.mkdir(parents=True, exist_ok=True)
    jobs: list[tuple[int, str, Path]] = []
    for offset in range(count):
        index = start_index + offset
        candidate_id = f"g{generation:04d}-c{index:04d}"
        candidate_root = base / candidate_id / "workspace"
        jobs.append((index, candidate_id, candidate_root))

    workers = max(1, min(config.search.parallel_candidates, count))
    results: list[CandidateResult] = []
    if workers == 1:
        for index, candidate_id, candidate_root in jobs:
            results.append(
                run_candidate(
                    workspace=workspace,
                    candidate_root=candidate_root,
                    candidate_id=candidate_id,
                    index=index,
                    generation=generation,
                    baseline=baseline,
                    trusted_specs=trusted_specs,
                    prior_iterations=prior_iterations,
                    config=config,
                    log=log,
                    client_factory=client_factory,
                )
            )
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rsi-candidate") as pool:
            futures = {
                pool.submit(
                    run_candidate,
                    workspace=workspace,
                    candidate_root=candidate_root,
                    candidate_id=candidate_id,
                    index=index,
                    generation=generation,
                    baseline=baseline,
                    trusted_specs=trusted_specs,
                    prior_iterations=prior_iterations,
                    config=config,
                    log=log,
                    client_factory=client_factory,
                ): index
                for index, candidate_id, candidate_root in jobs
            }
            for future in as_completed(futures):
                results.append(future.result())
    return sorted(results, key=lambda item: item.index)


def remove_candidate(result: CandidateResult) -> None:
    container = result.root.parent
    if container.exists():
        shutil.rmtree(container, ignore_errors=True)
