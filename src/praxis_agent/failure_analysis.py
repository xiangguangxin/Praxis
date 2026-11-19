"""Deterministic failure classification from trusted runtime signals only."""
from __future__ import annotations

def analyze_failure(state: dict) -> dict | None:
    if not isinstance(state, dict):
        raise ValueError("Runtime state must be an object")
    error = state.get("last_error") if isinstance(state.get("last_error"), dict) else {}
    result = state.get("last_result") if isinstance(state.get("last_result"), dict) else {}
    task = state.get("last_task_verification") if isinstance(state.get("last_task_verification"), dict) else {}
    code = error.get("code") or result.get("code")
    phase = error.get("phase") or result.get("phase")
    checks = task.get("checks")
    if not isinstance(checks, dict):
        checks = (state.get("artifact_verification") or {}).get("checks", {})
    if (result.get("ok") is True and not state.get("pending_action")
            and code not in ("task_acceptance_failed", "policy_denied")
            and error.get("may_have_executed") is not True):
        return None
    failed_checks = sorted(
        str(name)[:100] + ":" + str(item.get("status", "unknown"))[:40]
        for name, item in checks.items()
        if isinstance(item, dict) and item.get("status") not in ("passed", "ok", "verified")
    )[:10] if isinstance(checks, dict) else []

    if state.get("pending_action") or error.get("may_have_executed") is True:
        category, severity, retryable, reconcile = "uncertain_action", "critical", False, True
        recovery = "Stop and reconcile the external action outcome before any retry."
    elif code == "policy_denied":
        category, severity, retryable, reconcile = "policy_denied", "high", False, False
        recovery = "Review the trusted operator policy; do not attempt to bypass it."
    elif code == "task_acceptance_failed" or failed_checks:
        category, severity, retryable, reconcile = "verification_failed", "medium", True, False
        recovery = "Inspect failed acceptance checks and revise the unfinished task plan."
    elif code and any(token in str(code).casefold() for token in ("timeout", "connection", "unavailable", "temporar")):
        category, severity, retryable, reconcile = "transient_backend_failure", "medium", True, False
        recovery = "Reobserve backend readiness and retry only after confirming no action is in flight."
    elif result.get("ok") is False:
        category, severity, retryable, reconcile = "action_failed", "medium", True, False
        recovery = "Reobserve the target and choose a different bounded action."
    elif state.get("stop_reason") in ("step_budget_exhausted", "model_budget_exhausted"):
        category, severity, retryable, reconcile = "budget_exhausted", "low", False, False
        recovery = "Increase the relevant budget or split the goal; do not repeat the same plan unchanged."
    else:
        return None

    signals = []
    if isinstance(code, str) and code:
        signals.append("error_code:" + code[:100])
    if isinstance(phase, str) and phase:
        signals.append("phase:" + phase[:40])
    signals.extend("failed_check:" + item for item in failed_checks)
    if state.get("pending_action"):
        signals.append("pending_action:true")
    return {
        "schema_version": 1, "category": category, "severity": severity,
        "retryable": retryable, "requires_reconciliation": reconcile,
        "signals": signals[:12], "recommended_recovery": recovery,
    }
