"""Evaluate synthetic, real Nova, or real Playwright goals with independent checks."""
from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
import math
from urllib.parse import urljoin, urlsplit
from pathlib import Path

from .agent import run
from .benchmark_provenance import manifest, package_version
from . import tool_trace
from .experience import ExperienceStore
from .model_connection import add_model_arguments, resolve_model_connection
from .planner import ModelPlanner
from .recovery import reconcile
from .session import browser_scope, open_session
from .state import ALLOWED, create
from .verification import FileVerifier
from .request_runtime import add_request_arguments, request_options


def _write_fixture(root, files):
    for filename, content in files.items():
        target = (root / filename).resolve()
        if not target.is_relative_to(root) or target == root:
            raise ValueError("Unsafe fixture output path")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


class ScriptedInterruption(KeyboardInterrupt):
    pass


class ScriptedDesktop:
    def __init__(self, root, scenario):
        self.root, self.scenario = root, scenario
        self.calls = 0

    def observe(self):
        return {"summary": "synthetic desktop", "step": self.calls,
                "tools": [{"name": "synthetic_click"}]}

    def act(self, action):
        self.calls += 1
        if self.scenario.get("fail_action"):
            return {"ok": False, "summary": "injected failure"}
        _write_fixture(self.root, self.scenario.get("outputs", {}))
        if self.calls == 1 and self.scenario.get("interrupt_after_write"):
            raise ScriptedInterruption()
        return {"ok": True, "summary": "synthetic action"}


class ScriptedPlanner:
    def __init__(self, scenario):
        self.scenario = scenario
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

    def decide(self, state, observation):
        self.usage["calls"] += 1
        self.usage["input_tokens"] += self.scenario.get("input_tokens_per_call", 100)
        self.usage["output_tokens"] += self.scenario.get("output_tokens_per_call", 20)
        if state["steps"] >= self.scenario.get("actions", 1):
            return {"type": "complete", "evidence": {c: "claimed" for c in state["criteria"]}}
        return {"type": "act", "action": {"name": "synthetic_click", "impact": "low"}}


def _record(case, index, variant, state, verifier, actions, elapsed):
    expected = case.get("expected_status", "completed")
    actual = verifier.evaluate(state)
    recorded = state.get("artifact_verification", {}).get("checks", {})
    confirmed = state["status"] == "completed" and actual["passed"]
    passed = state["status"] == expected and (confirmed if expected == "completed" else not actual["passed"])
    passed = passed and all(recorded.get(criterion, {}).get("status") == reason
                            for criterion, reason in case.get("expected_checks", {}).items())
    usage = state.get("model_usage", {})
    recovered = any(event["event"] == "reconciled" for event in state["history"])
    if case.get("interrupt_after_write"):
        passed = passed and recovered and actions == 1
    trajectory = {}
    if "expected_trajectory" in case:
        trace = state.get("tool_trace")
        expected_trajectory = case["expected_trajectory"]
        evaluation = tool_trace.evaluate(expected_trajectory, trace)
        passed = passed and evaluation["passed"]
        trajectory = {"trajectory": {**evaluation, "expected": expected_trajectory, "trace": trace}}
    return {
        "scenario": case["name"], "iteration": index + 1, "variant": variant,
        "passed": passed, "completed_and_verified": confirmed, "status": state["status"],
        "verification_level": state.get("verification_level", "unverified"),
        "expected_status": expected, "checks": {name: check["status"] for name, check in actual["checks"].items()},
        "actions": actions, "reconciled": recovered, "model_calls": usage.get("calls", 0),
        "input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0),
        "usage_missing_calls": usage.get("usage_missing_calls", 0),
        "experience_hint_calls": usage.get("experience_hint_calls", 0),
        "strategy_hint_calls": usage.get("strategy_hint_calls", 0),
        "routes": {key: value for key, value in usage.items()
                   if key.startswith("route_") and type(value) is int},
        "estimated_input_tokens": usage.get("estimated_input_tokens", 0),
        "elapsed_seconds": round(elapsed, 4),
        "cost_microusd": usage.get("cost_microusd", 0),
        "cost_unknown_calls": usage.get("cost_unknown_calls", 0),
        "provider_retries": usage.get("retries", 0),
        "retrieved_hints": [{key: hint.get(key) for key in ("kind", "id", "environment", "source_store")}
                            for hint in state.get("hint_exposures", [])],
        **trajectory,
    }


def _metrics(records, synthetic):
    positive = [item for item in records if item["expected_status"] == "completed"]
    usage_complete = all(item["usage_missing_calls"] == 0
                         and (synthetic or item["model_calls"] > 0) for item in records)
    trajectories = [item["trajectory"]["passed"] for item in records if "trajectory" in item]
    route_counts = {}
    for item in records:
        for route, count in item.get("routes", {}).items():
            route_counts[route] = route_counts.get(route, 0) + count
    successes = sum(item["passed"] for item in records)
    count = len(records)
    rate = successes / count
    z = 1.96
    denominator = 1 + z*z/count
    center = (rate + z*z/(2*count)) / denominator
    radius = z * math.sqrt(rate*(1-rate)/count + z*z/(4*count*count)) / denominator
    elapsed = sorted(item.get("elapsed_seconds", 0) for item in records)
    claimed = [item for item in records if item.get("status") == "completed"]
    return {
        "success_rate_95ci": [max(0, center-radius), min(1, center+radius)],
        "p50_seconds": statistics.median(elapsed),
        "p95_seconds": elapsed[max(0, math.ceil(count*0.95)-1)],
        "false_completion_rate": (sum(not item["completed_and_verified"] for item in claimed) / len(claimed)
                                  if claimed else None),
        "reconciled_runs": sum(bool(item.get("reconciled")) for item in records),
        "total_cost_usd": (sum(item.get("cost_microusd", 0) for item in records) / 1_000_000
                           if not synthetic and all(item.get("cost_unknown_calls", 1) == 0 for item in records) else None),
        "route_counts": route_counts,
        "trajectory_pass_rate": statistics.mean(trajectories) if trajectories else None,
        "runs": len(records), "success_rate": statistics.mean(item["passed"] for item in records),
        "completion_rate": statistics.mean(item["completed_and_verified"] for item in positive) if positive else None,
        "avg_model_calls": statistics.mean(item["model_calls"] for item in records),
        "avg_tokens": statistics.mean(item["input_tokens"] + item["output_tokens"] for item in records)
        if usage_complete else None,
        "usage_missing_calls": sum(item["usage_missing_calls"] for item in records),
        "experience_hint_calls": sum(item["experience_hint_calls"] for item in records),
        "strategy_hint_calls": sum(item.get("strategy_hint_calls", 0) for item in records),
    }


def _report(kind, records, provenance):
    synthetic = kind == "offline-synthetic"
    names = dict.fromkeys(item["variant"] for item in records)
    return {"kind": kind, "provenance": provenance, **_metrics(records, synthetic),
            "variants": {name: _metrics([item for item in records if item["variant"] == name], synthetic)
                         for name in names}, "records": records}


def _validate(scenarios, repeat):
    if not isinstance(scenarios, list) or not scenarios or repeat < 1:
        raise ValueError("Nonempty scenarios and positive repeat required")
    names = set()
    for case in scenarios:
        if not isinstance(case, dict) or not isinstance(case.get("name"), str) or not case["name"].strip():
            raise ValueError("Every benchmark case needs a name")
        if case["name"] in names:
            raise ValueError("Benchmark case names must be unique")
        names.add(case["name"])
        if not isinstance(case.get("rules"), dict) or not case["rules"]:
            raise ValueError(f"{case['name']}: expected at least one artifact rule")
        if case.get("expected_status", "completed") not in ALLOWED - {"planning", "running"}:
            raise ValueError(f"{case['name']}: unsupported expected status")
        if "expected_trajectory" in case:
            tool_trace.validate_expectation(case["expected_trajectory"])


def benchmark(scenarios: list[dict], *, repeat: int = 1) -> dict:
    _validate(scenarios, repeat)
    provenance = manifest(scenarios, repeat)
    records = []
    for case in scenarios:
        for index in range(repeat):
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder).resolve()
                _write_fixture(root, case.get("initial_files", {}))
                path = root / "state.json"
                create(path, case["name"], list(case["rules"]), max_steps=case.get("max_steps", 10))
                desktop = ScriptedDesktop(root, case)
                verifier = FileVerifier(case["rules"], root)
                planner = ScriptedPlanner(case)
                start = time.perf_counter()
                trace_tools = "expected_trajectory" in case
                try:
                    state = run(path, desktop, planner, verifier=verifier, trace_tools=trace_tools)
                except ScriptedInterruption:
                    run(path, desktop, planner, verifier=verifier)
                    reconcile(path, outcome="executed", note="Scripted crash after writing the fixture")
                    state = run(path, desktop, planner, verifier=verifier)
                records.append(_record(case, index, "scripted", state, verifier, desktop.calls,
                                       time.perf_counter() - start))
    return _report("offline-synthetic", records, provenance)


def _case_url(base, case):
    """Operator-owned scenarios can select paths, never another backend origin."""
    from .adapters.browser import _origin
    path = case.get("start_path", "")
    if not isinstance(path, str) or urlsplit(path).scheme or urlsplit(path).netloc:
        raise ValueError("Benchmark start_path must be relative to the configured origin")
    target = urljoin(base, path)
    if _origin(target) != _origin(base):
        raise ValueError("Benchmark scenario changes browser origin")
    return target


def live_benchmark(scenarios, args):
    _validate(scenarios, args.repeat)
    for case in scenarios:
        if not isinstance(case.get("goal"), str) or not case["goal"].strip():
            raise ValueError(f"{case['name']}: live scenarios require a goal")
        if {"outputs", "fail_action", "interrupt_after_write"} & case.keys():
            raise ValueError(f"{case['name']}: scripted outputs, actions and interruption are offline only")
        if args.browser_url:
            _case_url(args.browser_url, case)
    connection = resolve_model_connection(args)
    experience = ExperienceStore(args.experience_store) if args.experience_store else None
    from .evolution import EvolutionStore
    evolution = EvolutionStore(args.evolution_store, experience) if args.evolution_store else None
    if bool(evolution) != bool(args.evolution_environment):
        raise ValueError("--evolution-store and --evolution-environment must be used together")
    if args.strategy_candidate_id and not evolution:
        raise ValueError("--strategy-candidate-id requires --evolution-store")
    provenance = manifest(scenarios, args.repeat)
    provenance["model_connection"] = connection.kind
    provenance["model_budget"] = {"max_calls": args.max_model_calls, "max_tokens": args.max_model_tokens,
                                  **request_options(args)}
    if args.browser_url:
        provenance["playwright_version"] = package_version("playwright")
    records = []
    for case in scenarios:
        for index in range(args.repeat):
            variants = ["baseline"]
            if experience is not None:
                variants.append("experience")
            if args.strategy_candidate_id:
                variants.append("strategy_candidate")
            # Alternate which run sees the application first within each pair.
            if index % 2:
                variants.reverse()
            for variant in variants:
                with tempfile.TemporaryDirectory() as folder:
                    root = Path(folder).resolve()
                    output = root / "artifacts"
                    output.mkdir()
                    _write_fixture(output, case.get("initial_files", {}))
                    path = root / "state.json"
                    goal = case["goal"].replace("{output_dir}", str(output))
                    create(path, goal, list(case["rules"]), max_steps=case.get("max_steps", 25))
                    verifier = FileVerifier(case["rules"], output)
                    planner = ModelPlanner(connection.model, connection.api_key, connection.endpoint,
                                           max_calls=args.max_model_calls,
                                           max_tokens=args.max_model_tokens,
                                           experience_store=experience if variant == "experience" else None,
                                           experience_environment=args.experience_environment,
                                           evolution_store=evolution if variant == "strategy_candidate" else None,
                                           evolution_environment=args.evolution_environment if variant == "strategy_candidate" else "",
                                           strategy_candidate_id=args.strategy_candidate_id if variant == "strategy_candidate" else None,
                                           **request_options(args))
                    from .policy import ActionPolicy, fingerprint
                    policy_path = getattr(args, "policy", None)
                    policy = ActionPolicy.from_json(policy_path) if policy_path else ActionPolicy()
                    browser_url = _case_url(args.browser_url, case) if args.browser_url else None
                    scope = (browser_scope(browser_url, output) if browser_url else
                             fingerprint({"nova": str(Path(args.nova).resolve())}))
                    start = time.perf_counter()
                    with open_session(planner, nova=args.nova, browser_url=browser_url,
                                      download_dir=output if args.browser_url else None,
                                      browser_executable=args.browser_executable) as target:
                        state = run(path, target, planner, verifier=verifier, backend_scope=scope,
                                    trace_tools="expected_trajectory" in case, policy=policy)
                        backend_version = getattr(target, "backend_version", None)
                    record = _record(case, index, variant, state, verifier, state["steps"],
                                     time.perf_counter() - start)
                    record["backend_version"] = backend_version
                    records.append(record)
    result = _report("live-browser" if args.browser_url else "live-nova", records, provenance)
    result["model"] = connection.model
    return result


def main():
    parser = argparse.ArgumentParser(description="Evaluate Praxis goal execution")
    add_request_arguments(parser)
    parser.add_argument("suite", type=Path)
    parser.add_argument("--case", help="Only run this scenario")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output", type=Path)
    backend = parser.add_mutually_exclusive_group()
    backend.add_argument("--nova", help="Run a live suite against the specified Nova executable")
    backend.add_argument("--browser-url", help="Run a live suite in isolated Playwright Chromium")
    parser.add_argument("--browser-executable")
    parser.add_argument("--policy", type=Path, help="Trusted exact-match action policy for live benchmarks")
    add_model_arguments(parser)
    parser.add_argument("--max-model-calls", type=int, default=35)
    parser.add_argument("--max-model-tokens", type=int, default=50_000)
    parser.add_argument("--experience-store", type=Path)
    parser.add_argument("--experience-environment", default="")
    parser.add_argument("--evolution-store", type=Path)
    parser.add_argument("--evolution-environment", default="")
    parser.add_argument("--strategy-candidate-id", help="Run paired baseline/strategy-candidate evaluation")
    parser.add_argument("--allow-failures", action="store_true",
                        help="Write evaluation reports even when benchmark success rate is below 100 percent")
    args = parser.parse_args()
    try:
        suite = json.loads(args.suite.read_text(encoding="utf-8"))["scenarios"]
        if args.case:
            suite = [case for case in suite if case["name"] == args.case]
        if args.nova or args.browser_url:
            if args.browser_executable and not args.browser_url:
                raise ValueError("--browser-executable requires --browser-url")
            if bool(args.experience_store) != bool(args.experience_environment):
                raise ValueError("--experience-store and --experience-environment must be used together")
            if bool(args.evolution_store) != bool(args.evolution_environment):
                raise ValueError("--evolution-store and --evolution-environment must be used together")
            if args.strategy_candidate_id and not args.evolution_store:
                raise ValueError("--strategy-candidate-id requires --evolution-store")
            if min(args.max_model_calls, args.max_model_tokens) < 0:
                raise ValueError("Model limits must be nonnegative")
            result = live_benchmark(suite, args)
        else:
            if args.litellm_base_url is not None:
                raise ValueError("--litellm-base-url requires a live backend")
            if (args.experience_store or args.experience_environment or args.evolution_store
                    or args.evolution_environment or args.strategy_candidate_id
                    or args.browser_executable or args.policy):
                raise ValueError("Experience and browser options require a live backend")
            result = benchmark(suite, repeat=args.repeat)
        output = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        parser.error(str(exc))
    print(output)
    if result["success_rate"] != 1 and not args.allow_failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
