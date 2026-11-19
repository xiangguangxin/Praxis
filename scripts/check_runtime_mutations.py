#!/usr/bin/env python3
"""Verify that selected Runtime regressions fail when critical guards are weakened."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

RUNTIME = Path(__file__).resolve().parents[1]
MUTATIONS = (
    ("skip-tool-contract", "agent.py", 'validate_action(action, observation.get("tools"))', "pass",
     "test_dispatch_recovery.DispatchRecoveryTests.test_planner_can_correct_a_rejected_action_without_spending_a_step"),
    ("coerce-policy-boolean", "policy.py", 'fingerprint(rule["arguments"]) == fingerprint(args)',
     'rule["arguments"] == args', "test_dispatch_recovery.DispatchRecoveryTests.test_policy_boolean_allow_does_not_match_numeric_argument"),
    ("overwrite-competing-checkpoint", "storage.py", 'os.link(temporary, path)', 'os.replace(temporary, path)',
     "test_dispatch_recovery.DispatchRecoveryTests.test_create_only_publication_never_overwrites_competing_file"),
    ("ignore-sensitive-control-target", "policy.py", "if any(_sensitive(value) for value in targets):", "if False:",
     "test_reliability.ReliabilityTests.test_generic_click_sensitive_targets_need_review_in_both_languages"),
    ("skip-action-budget", "agent.py", 'if state["steps"] >= state["max_steps"]:', "if False:",
     "test_reliability.ReliabilityTests.test_budget_never_permits_an_extra_mutation"),
    ("hide-tools-after-twelve", "planner.py", "for tool in (tools if limit is None else tools[:limit]):",
     "for tool in tools[:12]:", "test_reliability.ReliabilityTests.test_tool_after_twelfth_can_be_discovered_with_full_schema"),
    ("accept-stale-artifact", "verification.py", 'return "stale_artifact", evidence',
     'return "passed", evidence', "test_artifact_acceptance.CoverageAcceptanceTests.test_stale_report_cannot_pass"),
    ("ignore-action-count", "tool_trace.py", "if counts[tool] > maximum:", "if False:",
     "test_tool_trajectory.ToolTrajectoryTests.test_verified_file_does_not_hide_extra_dispatch"),
    ("skip-reconciliation", "agent.py", '    if state.get("pending_action"):\n        # A crash',
     '    if False:\n        # A crash', "test_recovery_verification.Tests.test_crash_requires_human_reconcile"),
)


def check(source, *tests):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(source), str(RUNTIME / "tests"))))
    return subprocess.run([sys.executable, "-B", "-m", "unittest", *tests], env=env, cwd=source.parent,
                          capture_output=True, text=True, timeout=30)


def main():
    try:
        baseline = check(RUNTIME / "src", *(case[-1] for case in MUTATIONS))
    except subprocess.TimeoutExpired:
        print("Baseline tests timed out", file=sys.stderr)
        return 1
    if baseline.returncode:
        sys.stderr.write(baseline.stderr)
        return 1

    outcomes = []
    for name, module, before, after, test in MUTATIONS:
        with tempfile.TemporaryDirectory(prefix="praxis-mutation-") as folder:
            source = Path(folder) / "src"
            shutil.copytree(RUNTIME / "src", source, ignore=shutil.ignore_patterns("__pycache__"))
            path = source / "praxis_agent" / module
            original = path.read_text(encoding="utf-8")
            if original.count(before) != 1:
                outcomes.append({"mutation": name, "status": "anchor_changed"})
                continue
            path.write_text(original.replace(before, after, 1), encoding="utf-8")
            try:
                result = check(source, test)
            except subprocess.TimeoutExpired:
                outcomes.append({"mutation": name, "status": "timed_out"})
                continue

        if result.returncode == 0:
            status = "survived"
        elif result.returncode == 1 and re.search(r"^FAILED \(failures=1\)$", result.stderr, re.MULTILINE):
            status = "killed"
        else:
            status = "invalid_run"
            sys.stderr.write(result.stderr)
        outcomes.append({"mutation": name, "status": status})

    print(json.dumps(outcomes, indent=2))
    return 0 if all(case["status"] == "killed" for case in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
