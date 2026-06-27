#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from rsi.agent import AgentResult, run_agent_iteration
from rsi.candidates import CandidateResult
from rsi.config import LimitsConfig, RunConfig, TestSpec
from rsi.evaluator import establish_baseline, evaluate_candidate
from rsi.models import ModelClient
from rsi.orchestrator import run_recursive_improvement
from rsi.promotion import promote_candidate
from rsi.receipts import EventLog
from rsi.testing import run_tests
from rsi.tools import WorkspaceTools


class Failure(RuntimeError):
    pass


def require(condition: object, message: str) -> None:
    if not condition:
        raise Failure(message)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def base_config(run_dir: Path) -> RunConfig:
    config = RunConfig()
    config.auto_discover_tests = False
    config.snapshot.mode = "copy"
    config.run_dir = str(run_dir)
    config.search.parallel_candidates = 1
    config.search.max_actions = 8
    config.search.max_invalid_responses = 2
    config.search.patience = 1
    config.test_timeout = 10
    config.command_timeout = 10
    return config


def test_run_tests_timeout() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        specs = [
            TestSpec("slow", f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(0.2)'", timeout=0.03),
            TestSpec("fast", f"{shlex.quote(sys.executable)} -c 'print(1)'"),
        ]
        results = run_tests(root, specs, default_timeout=2.0)
        require(results[0].timed_out and not results[0].passed, "per-test timeout was not enforced")
        require(results[1].passed, "default test timeout path failed")


def _bug_project(root: Path) -> TestSpec:
    write(root / "app.py", "def add(a, b):\n    return a - b\n")
    write(
        root / "test_app.py",
        "from app import add\nraise SystemExit(0 if add(2, 3) == 5 else 1)\n",
    )
    return TestSpec("addition", f"{shlex.quote(sys.executable)} test_app.py")


def test_scoring_ignores_model_tests() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary) / "base"
        base.mkdir()
        trusted = _bug_project(base)
        config = base_config(Path(temporary) / "run")
        config.tests = [trusted]
        baseline = establish_baseline(base, config, [trusted])
        require(not baseline.tests[0].passed, "fixture baseline unexpectedly passed")

        genuine = Path(temporary) / "genuine"
        cheat_a = Path(temporary) / "cheat-a"
        cheat_b = Path(temporary) / "cheat-b"
        shutil.copytree(base, genuine)
        shutil.copytree(base, cheat_a)
        shutil.copytree(base, cheat_b)
        write(genuine / "app.py", "def add(a, b):\n    return a + b\n")
        write(cheat_a / "notes.txt", "cosmetic\n")
        write(cheat_b / "notes.txt", "cosmetic\n")

        genuine_agent = AgentResult(True, "fixed", False, [TestSpec("model-true", "true")])
        cheat_one = AgentResult(True, "cheat", False, [TestSpec("one", "true")])
        cheat_many = AgentResult(
            True,
            "cheat",
            False,
            [TestSpec("one", "true"), TestSpec("two", "true"), TestSpec("three", "true")],
        )
        genuine_eval = evaluate_candidate(
            baseline_root=base,
            candidate_root=genuine,
            baseline=baseline,
            agent_result=genuine_agent,
            trusted_specs=[trusted],
            config=config,
            candidate_index=0,
        )
        cheat_eval_a = evaluate_candidate(
            baseline_root=base,
            candidate_root=cheat_a,
            baseline=baseline,
            agent_result=cheat_one,
            trusted_specs=[trusted],
            config=config,
            candidate_index=0,
        )
        cheat_eval_b = evaluate_candidate(
            baseline_root=base,
            candidate_root=cheat_b,
            baseline=baseline,
            agent_result=cheat_many,
            trusted_specs=[trusted],
            config=config,
            candidate_index=0,
        )
        require(genuine_eval.eligible, "genuine repair was rejected")
        require(genuine_eval.repaired_tests == ["addition"], "genuine repair received no repair credit")
        require(genuine_eval.score > cheat_eval_a.score, "trivial model tests outscored a real repair")
        require(cheat_eval_a.score == cheat_eval_b.score, "number of model-proposed tests changed score")


def test_regression_and_protected_gates() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "base"
        root.mkdir()
        write(root / "app.py", "VALUE = 1\n")
        write(root / "check.py", "import app\nraise SystemExit(0 if app.VALUE == 1 else 1)\n")
        trusted = TestSpec("value", f"{shlex.quote(sys.executable)} check.py")
        config = base_config(Path(temporary) / "run")
        config.protected_paths = ["check.py"]
        baseline = establish_baseline(root, config, [trusted])
        require(baseline.tests[0].passed, "passing fixture failed")

        broken = Path(temporary) / "broken"
        shutil.copytree(root, broken)
        write(broken / "app.py", "VALUE = 2\n")
        evaluation = evaluate_candidate(
            baseline_root=root,
            candidate_root=broken,
            baseline=baseline,
            agent_result=AgentResult(True, "break", False),
            trusted_specs=[trusted],
            config=config,
            candidate_index=0,
        )
        require(not evaluation.eligible and evaluation.regressions == ["value"], "regression gate failed")

        tampered = Path(temporary) / "tampered"
        shutil.copytree(root, tampered)
        write(tampered / "check.py", "raise SystemExit(0)\n")
        write(tampered / "notes.txt", "changed\n")
        evaluation = evaluate_candidate(
            baseline_root=root,
            candidate_root=tampered,
            baseline=baseline,
            agent_result=AgentResult(True, "tamper", False),
            trusted_specs=[trusted],
            config=config,
            candidate_index=0,
        )
        require(not evaluation.eligible and "check.py" in evaluation.protected_changes, "protected-path gate failed")


def test_promotion_rolls_back_real_workspace_failure() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "real-project"
        root.mkdir()
        write(root / "app.txt", "old\n")
        write(
            root / "gate.py",
            "from pathlib import Path\n"
            f"real = Path({str(root)!r}).resolve()\n"
            "bad = Path.cwd().resolve() == real and Path('app.txt').read_text() == 'new\\n'\n"
            "raise SystemExit(1 if bad else 0)\n",
        )
        trusted = TestSpec("environment-gate", f"{shlex.quote(sys.executable)} gate.py")
        config = base_config(Path(temporary) / "run")
        baseline = establish_baseline(root, config, [trusted])
        candidate_root = Path(temporary) / "candidate"
        shutil.copytree(root, candidate_root)
        write(candidate_root / "app.txt", "new\n")
        agent = AgentResult(True, "candidate passes in isolation", False)
        evaluation = evaluate_candidate(
            baseline_root=root,
            candidate_root=candidate_root,
            baseline=baseline,
            agent_result=agent,
            trusted_specs=[trusted],
            config=config,
            candidate_index=0,
        )
        require(evaluation.eligible, "isolated candidate fixture was not eligible")
        candidate = CandidateResult("candidate", 0, candidate_root, agent, evaluation)
        log = EventLog(Path(temporary) / "events.jsonl")
        result = promote_candidate(
            workspace=root,
            candidate=candidate,
            generation=1,
            run_dir=Path(temporary) / "run",
            baseline=baseline,
            trusted_specs=[trusted],
            config=config,
            log=log,
        )
        require(not result.accepted and result.rolled_back, "failed real-workspace validation did not roll back")
        require((root / "app.txt").read_text(encoding="utf-8") == "old\n", "rollback did not restore workspace")


class ToolLoopModel(ModelClient):
    def complete(self, messages: list[dict[str, str]]) -> str:
        tool_messages = [item for item in messages if item["role"] == "user" and '"tool"' in item["content"]]
        context = messages[1]["content"]
        candidate = "c0000"
        for token in ("c0000", "c0001", "c0002", "c0003"):
            if token in context:
                candidate = token
                break
        if not tool_messages:
            if candidate == "c0000":
                return json.dumps({
                    "action": "write",
                    "args": {"path": "notes.txt", "content": "cosmetic candidate\n"},
                    "reason": "First adaptive probe",
                })
            return json.dumps({
                "action": "replace",
                "args": {"path": "app.py", "old": "return a - b", "new": "return a + b"},
                "reason": "Repair addition",
            })
        return json.dumps({
            "action": "finish",
            "args": {"summary": "candidate complete", "done": False, "tests": [{"name": "sanity", "command": "true"}]},
            "reason": "Ready for trusted evaluation",
        })


def test_agent_and_adaptive_orchestrator() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary) / "project"
        workspace.mkdir()
        trusted = _bug_project(workspace)
        config = base_config(Path(temporary) / "run")
        config.tests = [trusted]
        config.search.strategy = "adaptive"
        config.search.initial_candidates = 1
        config.search.max_candidates = 4
        config.search.max_generations = 1
        config.search.patience = 0
        receipt = run_recursive_improvement(
            workspace,
            config,
            client_factory=lambda: ToolLoopModel(),
        )
        require(receipt["status"] == "completed", "adaptive run returned error")
        require(receipt["accepted_generations"] == 1, "adaptive run promoted no candidate")
        require("return a + b" in (workspace / "app.py").read_text(encoding="utf-8"), "adaptive winner was not promoted")
        generation = receipt["generations"][0]
        require(len(generation["candidates"]) == 2, "adaptive search did not expand from one candidate")
        require(str(generation["selected_candidate"]).endswith("c0001"), "adaptive search selected the cosmetic candidate")


def test_unlimited_read_default() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        text = ("x" * 1024 + "\n") * 2048
        write(root / "large.txt", text)
        limits = LimitsConfig(read_bytes=0, tool_output_bytes=0)
        tools = WorkspaceTools(root, excludes=[], protected_paths=[], limits=limits, command_timeout=2)
        result = tools.execute("read", {"path": "large.txt"})
        output = str(result.get("output", ""))
        require("read limit reached" not in output, "unlimited read default imposed a hidden cap")
        require(len(output) > len(text), "large file was not fully returned with unlimited limits")


def test_child_session_launchers_refuse() -> None:
    controller = subprocess.run(
        [sys.executable, str(PACKAGE_ROOT / "scripts" / "recursive_self_improve.py")],
        text=True,
        capture_output=True,
        check=False,
    )
    require(controller.returncode == 2, "controller CLI did not fail closed")
    require("same-session" in controller.stderr, "controller refusal did not name same-session contract")

    adapter = subprocess.run(
        [sys.executable, str(PACKAGE_ROOT / "scripts" / "cli_model_adapter.py")],
        input="{}",
        text=True,
        capture_output=True,
        check=False,
    )
    require(adapter.returncode == 2, "CLI model adapter did not fail closed")
    require("current agent session" in adapter.stderr, "adapter refusal did not block child sessions")

    supervisor = subprocess.run(
        [sys.executable, str(PACKAGE_ROOT / "scripts" / "supervisor.py")],
        text=True,
        capture_output=True,
        check=False,
    )
    require(supervisor.returncode == 2, "supervisor did not fail closed")
    require("same-session" in supervisor.stderr, "supervisor refusal did not name same-session contract")

    version = subprocess.run(
        [sys.executable, str(PACKAGE_ROOT / "scripts" / "recursive_self_improve.py"), "--version"],
        text=True,
        capture_output=True,
        check=False,
    )
    require(version.returncode == 0 and version.stdout.strip() == "4.1.0", "CLI version is not 4.1.0")


def main() -> int:
    tests: list[tuple[str, Callable[[], None]]] = [
        ("run_tests_timeout", test_run_tests_timeout),
        ("trusted_scoring", test_scoring_ignores_model_tests),
        ("regression_and_protected_gates", test_regression_and_protected_gates),
        ("promotion_rollback", test_promotion_rolls_back_real_workspace_failure),
        ("adaptive_orchestrator", test_agent_and_adaptive_orchestrator),
        ("unlimited_read", test_unlimited_read_default),
        ("child_session_refusal", test_child_session_launchers_refuse),
    ]
    failures: list[str] = []
    for name, function in tests:
        try:
            function()
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {name}")
    if failures:
        print("\nFAILURES")
        for failure in failures:
            print("- " + failure)
        return 1
    print(f"ALL {len(tests)} TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
