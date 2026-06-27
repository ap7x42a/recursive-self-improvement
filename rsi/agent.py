from __future__ import annotations

import json
import textwrap
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import RunConfig, TestSpec, parse_test_object
from .models import ModelClient, ModelError
from .receipts import EventLog
from .testing import MetricResult, TestResult
from .tools import ToolError, WorkspaceTools, inventory


PROTOCOL = r"""
Return exactly one JSON object per turn. Do not wrap it in prose.

Normal action:
{"action":"read","args":{"path":"src/main.py","start_line":1,"end_line":240},"reason":"Inspect implementation"}

Finish the candidate iteration:
{"action":"finish","args":{"summary":"What changed and why","done":false,"tests":[{"name":"regression","command":"python3 -m pytest -q"}]},"reason":"Candidate is ready for evaluation"}

Actions:
- list(path=".", recursive=false, depth=1)
- read(path, start_line=1, end_line=0)
- glob(pattern)
- search(query, path=".", glob="*", regex=false, case_sensitive=true)
- write(path, content, append=false)
- replace(path, old, new, count=0)
- delete(path)
- move(source, destination)
- mkdir(path, parents=true)
- chmod(path, mode)
- run(command, timeout optional): unrestricted shell in the workspace
- status()
- finish(summary, done=false, tests=[])

Use tools to inspect before editing. Make focused, coherent changes. Run relevant checks before finish.
The controller independently runs trusted tests and metrics. Tests you propose are executed as candidate obligations but never create repair credit, so do not add trivial commands. `done=true` means the overall objective appears to have reached a fixed point; `done=false` means this is a useful incremental improvement.
""".strip()


@dataclass(slots=True)
class AgentResult:
    completed: bool
    summary: str
    done: bool
    proposed_tests: list[TestSpec] = field(default_factory=list)
    actions: int = 0
    invalid_responses: int = 0
    error: str = ""
    model_quality_score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["proposed_tests"] = [asdict(item) for item in self.proposed_tests]
        return value


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    decoder = json.JSONDecoder()
    starts = [index for index, character in enumerate(stripped) if character == "{"]
    for start in starts:
        try:
            value, _ = decoder.raw_decode(stripped[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("response did not contain a JSON object")


def _compact_results(results: list[TestResult] | list[MetricResult]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for result in results:
        data = result.to_dict()
        for key in ("stdout", "stderr"):
            if key in data and isinstance(data[key], str) and len(data[key]) > 4000:
                data[key] = data[key][:4000] + "...[truncated in prompt]"
        compact.append(data)
    return compact


def _trim_messages(messages: list[dict[str, str]], limit: int) -> list[dict[str, str]]:
    if not limit:
        return messages
    total = sum(len(item.get("content", "")) for item in messages)
    if total <= limit or len(messages) <= 2:
        return messages
    fixed = messages[:2]
    budget = max(0, limit - sum(len(item.get("content", "")) for item in fixed))
    retained: list[dict[str, str]] = []
    used = 0
    for item in reversed(messages[2:]):
        size = len(item.get("content", ""))
        if retained and used + size > budget:
            break
        retained.append(item)
        used += size
    return fixed + list(reversed(retained))


def run_agent_iteration(
    *,
    workspace: Path,
    client: ModelClient,
    config: RunConfig,
    candidate_id: str,
    generation: int,
    trusted_tests: list[TestResult],
    trusted_metrics: list[MetricResult],
    prior_iterations: list[dict[str, Any]],
    log: EventLog,
) -> AgentResult:
    tools = WorkspaceTools(
        workspace,
        excludes=config.snapshot.excludes,
        protected_paths=config.protected_paths,
        limits=config.limits,
        command_timeout=config.command_timeout,
    )
    tree = inventory(
        workspace,
        depth=config.limits.inventory_depth,
        max_entries=config.limits.inventory_entries,
        excludes=config.snapshot.excludes,
    )
    system = (
        "You are the autonomous mutation worker in a recursive self-improvement system. "
        "You operate on an isolated candidate copy. There is no human approval step. "
        "The candidate will be independently evaluated and either promoted or discarded.\n\n"
        + PROTOCOL
    )
    context = {
        "objective": config.objective,
        "candidate_id": candidate_id,
        "generation": generation,
        "workspace_inventory": tree,
        "trusted_test_baseline": _compact_results(trusted_tests),
        "trusted_metric_baseline": _compact_results(trusted_metrics),
        "prior_accepted_iterations": prior_iterations,
    }
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": "Improve this workspace now. Context:\n" + json.dumps(context, ensure_ascii=False, indent=2),
        },
    ]
    consecutive_invalid = 0
    invalid_total = 0

    for action_number in range(1, config.search.max_actions + 1):
        outbound = _trim_messages(messages, config.limits.conversation_chars)
        try:
            raw = client.complete(outbound)
        except ModelError as exc:
            log.emit(
                "model_error",
                generation=generation,
                candidate=candidate_id,
                action_number=action_number,
                error=str(exc),
            )
            return AgentResult(False, "", False, actions=action_number - 1, invalid_responses=invalid_total, error=str(exc))

        log.emit(
            "model_response",
            generation=generation,
            candidate=candidate_id,
            action_number=action_number,
            response=raw,
        )
        try:
            envelope = _extract_json_object(raw)
            action = envelope.get("action")
            args = envelope.get("args", {})
            reason = envelope.get("reason", "")
            if not isinstance(action, str) or not action:
                raise ValueError("action must be a nonempty string")
            if not isinstance(args, dict):
                raise ValueError("args must be an object")
        except (ValueError, TypeError) as exc:
            consecutive_invalid += 1
            invalid_total += 1
            feedback = {"ok": False, "error": f"invalid model response: {exc}", "required": "one JSON action object"}
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": json.dumps(feedback)})
            log.emit(
                "invalid_model_response",
                generation=generation,
                candidate=candidate_id,
                action_number=action_number,
                error=str(exc),
            )
            if consecutive_invalid >= config.search.max_invalid_responses:
                return AgentResult(
                    False,
                    "",
                    False,
                    actions=action_number,
                    invalid_responses=invalid_total,
                    error=f"model exceeded {config.search.max_invalid_responses} consecutive invalid responses",
                )
            continue

        consecutive_invalid = 0
        messages.append({"role": "assistant", "content": raw})
        if action == "finish":
            summary = args.get("summary", "")
            done = bool(args.get("done", False))
            tests_value = args.get("tests", [])
            if not isinstance(summary, str):
                summary = str(summary)
            if not isinstance(tests_value, list):
                messages.append({"role": "user", "content": json.dumps({"ok": False, "error": "finish.tests must be a list"})})
                invalid_total += 1
                consecutive_invalid += 1
                continue
            try:
                proposed_tests = [parse_test_object(item) for item in tests_value]
            except (ValueError, KeyError, TypeError) as exc:
                messages.append({"role": "user", "content": json.dumps({"ok": False, "error": f"invalid finish.tests: {exc}"})})
                invalid_total += 1
                consecutive_invalid += 1
                continue
            quality = args.get("quality_score")
            quality_score: float | None = None
            if isinstance(quality, (int, float)) and not isinstance(quality, bool):
                quality_score = float(quality)
            log.emit(
                "agent_finish",
                generation=generation,
                candidate=candidate_id,
                action_number=action_number,
                summary=summary,
                done=done,
                proposed_tests=[asdict(item) for item in proposed_tests],
            )
            return AgentResult(
                True,
                summary,
                done,
                proposed_tests=proposed_tests,
                actions=action_number,
                invalid_responses=invalid_total,
                model_quality_score=quality_score,
            )

        try:
            tool_result = tools.execute(action, args)
        except (ToolError, TypeError, ValueError, OSError) as exc:
            tool_result = {"ok": False, "error": str(exc), "action": action}
            log.emit(
                "tool_error",
                generation=generation,
                candidate=candidate_id,
                action_number=action_number,
                action=action,
                args=args,
                reason=reason,
                error=str(exc),
            )
        else:
            log.emit(
                "tool_action",
                generation=generation,
                candidate=candidate_id,
                action_number=action_number,
                action=action,
                args=args,
                reason=reason,
                result=tool_result,
            )
        messages.append({"role": "user", "content": json.dumps({"tool": action, **tool_result}, ensure_ascii=False)})

    return AgentResult(
        False,
        "",
        False,
        actions=config.search.max_actions,
        invalid_responses=invalid_total,
        error=f"action budget exhausted ({config.search.max_actions}) without finish",
    )
