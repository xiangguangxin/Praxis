"""Bounded, observable goal execution with write-ahead action recovery."""
from __future__ import annotations
import time
from pathlib import Path
from typing import Any
from .state import load, persist, _record, TERMINAL
from .policy import ActionPolicy, fingerprint, review_arguments
from .verifier import EvidenceOnlyVerifier
from .verification import FileVerifier
from .verifiers.registry import CriteriaVerifier
from .locking import checkpoint_lock
from .planner import TokenBudgetExceeded
from .diagnostics import error_detail, safe_summary
from .observations import normalize
from .validation import validate_action
from . import tool_trace
from .planning import TaskPlanRuntime, TaskRuntimeError
from .failure_analysis import analyze_failure
from .scheduler import LeaseLost

APPROVAL_TTL = 600


def _task_runtime(state, verifier, *, data=None):
    catalog = list(verifier.rules) if isinstance(verifier, (FileVerifier, CriteriaVerifier)) else state["criteria"]
    return TaskPlanRuntime(state.get("task_plan"), state["criteria"], data=data, available_criteria=catalog)


def run(path: Path, desktop, planner, *, approval: bool = False, verifier=None,
        backend_scope=None, trace_tools: bool = False, policy=None, approval_id=None) -> dict[str, Any]:
    with checkpoint_lock(path):
        return _run_locked(path, desktop, planner, approval=approval, verifier=verifier,
                           backend_scope=backend_scope, trace_tools=trace_tools,
                           policy=policy or ActionPolicy(), approval_id=approval_id)


def _verify(state, verifier, evidence, observation):
    if isinstance(verifier, (FileVerifier, CriteriaVerifier)):
        state["artifact_verification"] = verifier.evaluate(state)
        accepted = state["artifact_verification"]["passed"]
        level, source = ((getattr(verifier, "verification_level", "independent_artifact"),
                          getattr(verifier, "verification_source", "file_verifier")) if isinstance(verifier, FileVerifier)
                         else ("custom_verifier", "criteria_registry"))
        if not accepted:
            _record(state, "artifact_rejected", ", ".join(
                check["status"] for check in state["artifact_verification"]["checks"].values()))
    else:
        selected = verifier or EvidenceOnlyVerifier()
        accepted = selected.verify(state["criteria"], evidence, observation)
        level = "model_evidence" if isinstance(selected, EvidenceOnlyVerifier) else "custom_verifier"
        source = "model_claim" if level == "model_evidence" else type(selected).__name__
    if accepted:
        state["status"] = "completed"
        state["verification"] = {"level": level, "source": source, "checked_at": time.time(),
                                 "criteria_count": len(state["criteria"])}
        if isinstance(verifier, (FileVerifier, CriteriaVerifier)):
            state["verification"]["checks"] = state["artifact_verification"]["checks"]
        if isinstance(verifier, FileVerifier):
            state["verification"]["verifier_id"] = verifier.identity
        state["verification_level"] = level
        state["stop_reason"] = "criteria_satisfied"
        state["pending_action"] = None
        state["pending_approval"] = None
        state.pop("pending_review", None)
        _record(state, "completed", level)
    return accepted



def _task_verification_state(state, task, runtime):
    task_state = dict(state)
    task_state["criteria"] = list(task.acceptance)
    task_state["steps"] = 0
    task_state["pending_action"] = None
    task_state["last_result"] = None
    task_state["artifact_baseline"] = runtime.baselines.get(task.id)
    return task_state


def _prepare_task_verification(state, task, runtime, verifier):
    if isinstance(verifier, FileVerifier) and task.id not in runtime.baselines:
        task_state = _task_verification_state(state, task, runtime)
        verifier.prepare(task_state)
        runtime.baselines[task.id] = task_state.get("artifact_baseline")


def _verify_task(state, task, runtime, verifier):
    if not isinstance(verifier, (FileVerifier, CriteriaVerifier)):
        raise ValueError("Task completion requires an independent artifact or criteria verifier")
    task_state = _task_verification_state(state, task, runtime)
    result = verifier.evaluate(task_state)
    return {"passed": result["passed"], "checks": result.get("checks", {})}




def _pause(state, reason):
    state["status"], state["stop_reason"] = "paused", reason
    _record(state, "paused", reason)


def _prepare_start(path, *, approval, verifier, backend_scope, policy, approval_id):
    state = load(path)
    if state.get("pending_action") and state["status"] in TERMINAL - {"blocked"}:
        raise ValueError("Terminal checkpoint contains an unresolved action")
    if state["status"] in TERMINAL:
        return state, False
    if state["status"] == "paused" and state["pending_approval"] and not approval:
        return state, False
    if state.get("pending_action"):
        # A crash may have happened after a non-idempotent action was executed.
        # Never automatically replay it; require operator reconciliation.
        state["status"] = "blocked"
        state["stop_reason"] = "reconciliation_required"
        _record(state, "reconciliation_required", state["pending_action"]["name"])
        return persist(path, state), False
    saved_scope = state.get("backend_scope")
    if saved_scope is not None and backend_scope != saved_scope:
        raise ValueError("Resume requires the original backend settings")
    if backend_scope is not None and saved_scope is None:
        if state["steps"] or state.get("last_result") or state.get("pending_approval"):
            raise ValueError("Start backend-scoped execution with a fresh checkpoint")
        state["backend_scope"] = backend_scope
    saved_policy = state.get("policy_id")
    if saved_policy is not None and saved_policy != policy.identity:
        raise ValueError("Resume requires the original operator policy")
    state["policy_id"] = policy.identity
    saved_verifier = state.get("verifier_id")
    if state.get("criteria_verifier_id") and not isinstance(verifier, CriteriaVerifier):
        raise ValueError("Resume requires the original criteria verifier")
    if saved_verifier is not None and (not isinstance(verifier, FileVerifier) or saved_verifier != verifier.identity):
        raise ValueError("Resume requires the original artifact verifier")
    if isinstance(verifier, FileVerifier):
        state["verifier_id"] = verifier.identity
        verifier.prepare(state)
    elif isinstance(verifier, CriteriaVerifier):
        if state.get("artifact_baseline") is not None:
            raise ValueError("Resume requires the original artifact verifier")
        verifier.prepare(state)
        if state.get("criteria_verifier_id", verifier.identity) != verifier.identity:
            raise ValueError("Resume requires the original criteria verifier rules and revisions")
        state["criteria_verifier_id"] = verifier.identity
    elif state.get("artifact_baseline") is not None:
        raise ValueError("Resume requires the original artifact verifier")
    if state.get("task_runtime"):
        if not isinstance(verifier, (FileVerifier, CriteriaVerifier)):
            raise ValueError("Resuming task-orchestrated goals requires the original independent verifier")
        _task_runtime(state, verifier, data=state["task_runtime"])
    if approval_id is not None and (not approval or approval_id != state.get("pending_approval")):
        raise ValueError("Approval ID does not match the pending request")
    return state, True


def run_session(path: Path, session_factory, planner, *, approval=False, verifier=None,
                backend_scope=None, trace_tools=False, policy=None, approval_id=None,
                execution_guard=None):
    """Validate and bind configuration while locked, before launching any backend."""
    policy = policy or ActionPolicy()
    options = dict(approval=approval, verifier=verifier, backend_scope=backend_scope,
                   policy=policy, approval_id=approval_id)
    with checkpoint_lock(path):
        state, ready = _prepare_start(path, **options)
        if not ready:
            return state
        persist(path, state)
        try:
            with session_factory() as backend:
                return _run_locked(path, backend, planner, trace_tools=trace_tools, state=state,
                                   execution_guard=execution_guard, **options)
        except KeyboardInterrupt:
            if not state.get("pending_action") and state["status"] != "completed":
                _pause(state, "interrupted")
            persist(path, state)
            raise
        except Exception as exc:
            state["last_error"] = error_detail(exc, "session", bool(state.get("pending_action")))
            if state["status"] != "completed":
                state["status"] = "blocked"
                state["stop_reason"] = "reconciliation_required" if state.get("pending_action") else "retry_required"
            _record(state, "session_error", state["last_error"]["code"])
            return persist(path, state)


def _run_locked(path, desktop, planner, *, approval, verifier, backend_scope, trace_tools, policy, approval_id,
                state=None, execution_guard=None):
    if state is None:
        state, ready = _prepare_start(path, approval=approval, verifier=verifier, backend_scope=backend_scope,
                                      policy=policy, approval_id=approval_id)
        if not ready:
            return state
    approved = None
    if approval and state["pending_approval"]:
        pending = state.get("pending_review")
        if approval_id is not None and approval_id != state["pending_approval"]:
            raise ValueError("Approval ID does not match the pending request")
        # Legacy hash-only requests cannot be reviewed or bound to current context.
        if pending and pending.get("expires_at", 0) >= time.time():
            approved = state["pending_approval"]
        state["pending_approval"] = None
    state.pop("approved_action", None)
    if callable(getattr(verifier, "bind_backend", None)):
        verifier.bind_backend(desktop)
    state["status"] = "running"
    if isinstance(verifier, (FileVerifier, CriteriaVerifier)):
        state["task_acceptance_catalog"] = list(verifier.rules)
    state.pop("stop_reason", None)
    if trace_tools:
        tool_trace.enable(state)
    if hasattr(planner, "usage"):
        planner.usage.update(state.get("model_usage", {}))
    persist(path, state)
    discoveries = 0
    run_started = time.monotonic()
    previous_elapsed = state.get("elapsed_run_ms", 0)
    run_limit = getattr(planner, "max_run_seconds", 0)
    remaining = run_limit - previous_elapsed/1000 if run_limit else None
    if remaining is not None and callable(getattr(planner, "set_run_deadline", None)):
        planner.set_run_deadline(remaining)
    def check_deadline():
        if remaining is not None and time.monotonic()-run_started >= remaining:
            raise TokenBudgetExceeded("Run deadline exhausted")
    while True:
        phase = "observation"
        try:
            check_deadline()
            if execution_guard is not None:
                execution_guard.raise_if_lost()
            observation = normalize(desktop.observe())
            check_deadline()
            failure_analysis = analyze_failure(state)
            if failure_analysis is not None:
                if state.get("failure_analysis") != failure_analysis:
                    state["failure_analysis"] = failure_analysis
                    _record(state, "failure_analyzed", failure_analysis["category"])
                    persist(path, state)
            elif (state.get("last_result") or {}).get("ok") is True:
                state.pop("failure_analysis", None)
            state["last_observation"] = {"observed_at": observation["observed_at"],
                                         "observation_id": observation["observation_id"],
                                         "summary": safe_summary(observation.get("summary", ""))}
            # The action budget never prevents a final, read-only artifact check.
            if state["steps"] >= state["max_steps"] and isinstance(verifier, (FileVerifier, CriteriaVerifier)):
                phase = "verification"
                task_status = None
                if state.get("task_runtime"):
                    task_status = _task_runtime(state, verifier, data=state["task_runtime"]).status
                if task_status == "blocked":
                    state["status"], state["stop_reason"] = "blocked", "task_plan_blocked"
                    _record(state, "task_plan_blocked", "Task retry budget exhausted")
                elif task_status not in (None, "completed"):
                    _pause(state, "step_budget_exhausted")
                elif not _verify(state, verifier, {}, observation):
                    _pause(state, "step_budget_exhausted")
                break
            phase = "planning"
            if hasattr(planner, "prepare_plan"):
                should_plan = not state.get("task_plan")
                failed_since_plan = ((state.get("last_result") or {}).get("ok") is False
                                     and state.get("task_plan_at_step", -1) < state["steps"])
                if should_plan or (failed_since_plan and not state.get("task_runtime")
                                   and state.get("task_plan_replans", 0) < 3):
                    try:
                        state["task_plan"] = planner.prepare_plan(state.copy(), observation)
                        state["task_plan_at_step"] = state["steps"]
                        if not should_plan:
                            state["task_plan_replans"] = state.get("task_plan_replans", 0) + 1
                        _record(state, "task_plan_created" if should_plan else "task_plan_revised",
                                "revision " + str(state.get("task_plan_replans", 0)))
                    finally:
                        if hasattr(planner, "usage"):
                            state["model_usage"] = dict(planner.usage)
                    if isinstance(verifier, (FileVerifier, CriteriaVerifier)) and not state.get("task_runtime"):
                        try:
                            task_runtime = _task_runtime(state, verifier)
                        except TaskRuntimeError as exc:
                            state["task_plan_mode"] = "advisory"
                            _record(state, "task_plan_advisory", str(exc))
                        else:
                            state["task_runtime"] = task_runtime.to_dict()
                            state["task_plan_mode"] = "verified"
                            _record(state, "task_runtime_enabled", "independent per-task verification")
                    persist(path, state)
            task_runtime = None
            active_task = None
            if state.get("task_runtime"):
                task_runtime = _task_runtime(state, verifier, data=state["task_runtime"])
                active_task = task_runtime.active()
                if active_task is not None:
                    _prepare_task_verification(state, active_task, task_runtime, verifier)
                    state["active_task"] = {"id": active_task.id, "goal": active_task.goal,
                                            "acceptance": list(active_task.acceptance),
                                            "attempt": task_runtime.attempts[active_task.id]}
                else:
                    state["active_task"] = None
                    if task_runtime.status == "blocked":
                        state["status"], state["stop_reason"] = "blocked", "task_plan_blocked"
                        _record(state, "task_plan_blocked", "No runnable tasks remain")
                        break
                state["task_runtime"] = task_runtime.to_dict()
                persist(path, state)
            try:
                decision = planner.decide(state.copy(), observation)
            finally:
                if hasattr(planner, "usage"):
                    state["model_usage"] = dict(planner.usage)
            if not isinstance(decision, dict):
                raise TypeError("Planner decision must be a dict")
            kind = decision.get("type")
            # An approval is consumed by the immediately following decision only.
            grant, approved = approved, None
            state.pop("pending_review", None)
            if kind == "task_complete":
                if task_runtime is None or active_task is None:
                    raise ValueError("No independently verifiable active task")
                phase = "task_verification"
                task_verification = _verify_task(state, active_task, task_runtime, verifier)
                if task_verification["passed"]:
                    task_runtime.complete(active_task.id, task_verification)
                    state.pop("last_task_verification", None)
                    state.pop("last_error", None)
                    state.pop("failure_analysis", None)
                    _record(state, "task_completed", active_task.id)
                else:
                    task_runtime.reject(active_task.id, task_verification)
                    state["last_task_verification"] = {"task_id": active_task.id,
                                                        "checks": task_verification["checks"],
                                                        "attempt": task_runtime.attempts[active_task.id]}
                    state["last_error"] = {"code": "task_acceptance_failed", "phase": phase,
                                           "may_have_executed": False,
                                           "recovery": "Inspect failed checks and retry the active task."}
                    state["failure_analysis"] = analyze_failure(state)
                    _record(state, "task_acceptance_failed", active_task.id)
                    if (callable(getattr(planner, "replan_tasks", None))
                            and state.get("task_plan_replans", 0) < 3):
                        state["task_runtime"] = task_runtime.to_dict()
                        state["active_task"] = None
                        try:
                            revised = planner.replan_tasks(state.copy(), observation)
                            task_runtime.replace_remaining(revised)
                            state["task_plan"] = revised
                            state["task_plan_replans"] = state.get("task_plan_replans", 0) + 1
                            _record(state, "task_plan_revised_after_failure",
                                    "revision " + str(state["task_plan_replans"]))
                        except Exception as exc:
                            _record(state, "task_replan_failed", type(exc).__name__)
                        finally:
                            if hasattr(planner, "usage"):
                                state["model_usage"] = dict(planner.usage)
                state["task_runtime"] = task_runtime.to_dict()
                state["active_task"] = None
                persist(path, state)
                continue
            if kind == "complete":
                if active_task is not None:
                    raise ValueError("Complete the active task with task_complete before completing the overall goal")
                phase = "verification"
                if not _verify(state, verifier, decision.get("evidence", {}), observation):
                    raise ValueError("Missing acceptance evidence for one or more criteria")
                break
            if kind == "pause":
                _pause(state, "planner_requested")
                break
            if state["steps"] >= state["max_steps"]:
                _pause(state, "step_budget_exhausted")
                break
            if kind == "discover":
                discoveries += 1
                if discoveries > 4:
                    _pause(state, "tool_discovery_budget_exhausted")
                    break
                query, offset = decision.get("query", ""), decision.get("offset", 0)
                names = decision.get("tools", [])
                known = {tool["name"] for tool in observation.get("tools", []) if isinstance(tool, dict) and isinstance(tool.get("name"), str)}
                if (not isinstance(query, str) or len(query) > 160 or type(offset) is not int or offset < 0
                        or not isinstance(names, list) or len(names) > 6
                        or any(not isinstance(name, str) or name not in known for name in names)):
                    raise ValueError("Invalid tool discovery request")
                state["tool_selection"] = {"query": query, "offset": offset, "names": names}
                continue
            if kind != "act" or not isinstance(decision.get("action"), dict):
                raise ValueError("Planner must choose act, discover, task_complete, complete or pause")
            action = decision["action"]
            if not isinstance(action.get("name"), str) or not action["name"] or not isinstance(action.get("arguments", {}), dict):
                raise ValueError("Action requires a name and argument object")
            phase = "validation"
            validate_action(action, observation.get("tools"))
            phase = "policy"
            effect, reason = policy.classify(action)
            if effect == "deny":
                state["status"], state["stop_reason"] = "blocked", "policy_denied"
                state["last_error"] = {"code": "policy_denied", "phase": phase, "may_have_executed": False,
                                       "recovery": "Review the operator policy and create a new authorized goal."}
                state["failure_analysis"] = analyze_failure(state)
                _record(state, "failure_analyzed", state["failure_analysis"]["category"])
                _record(state, "policy_denied", action["name"])
                break
            scope = {"backend": backend_scope, "target": observation.get("action_scope"),
                     "view": observation["observation_id"], "policy": policy.identity}
            action_id = fingerprint({"scope": scope, "action": action})
            if effect == "approve" and grant != action_id:
                state["pending_approval"] = action_id
                state["pending_review"] = {"id": action_id, "tool": safe_summary(action["name"], 128),
                    "arguments": review_arguments(action.get("arguments", {})), "reason": reason,
                    "scope_id": fingerprint(scope), "created_at": time.time(), "expires_at": time.time() + APPROVAL_TTL}
                _pause(state, "approval_required")
                break
            state.pop("pending_review", None)
            # Repeated identical actions on an unchanged observed view signal no progress.
            repeat_id = fingerprint({"view": observation["observation_id"], "action": action})
            recent = state.setdefault("recent_actions", [])
            if len(recent) >= 3 and all(item.get("repeat_id") == repeat_id for item in recent[-3:]):
                _pause(state, "no_progress")
                break
            # Write-ahead checkpoint: after crash, never replay this action blindly.
            check_deadline()
            state["pending_action"] = {"name": action["name"], "since": time.time()}
            event = tool_trace.begin(state, action["name"], observation.get("tools"))
            persist(path, state)
            started = time.monotonic()
            phase = "action"
            if execution_guard is not None:
                execution_guard.raise_if_lost()
            result = desktop.act(action)
            if not isinstance(result, dict):
                raise TypeError("Backend action result must be a dict")
            ok = result.get("ok") is True
            if event is not None:
                event["outcome"] = "ok" if ok else "error"
                event["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
            state["steps"] += 1
            discoveries = 0
            state["pending_action"] = None
            state["last_result"] = {"ok": ok, "summary": safe_summary(result.get("summary", ""))}
            if ok:
                state.pop("last_error", None)
                state.pop("failure_analysis", None)
            else:
                state["failure_analysis"] = analyze_failure(state)
                _record(state, "failure_analyzed", state["failure_analysis"]["category"])
            known = {tool.get("name") for tool in observation.get("tools", []) if isinstance(tool, dict)}
            recent.append({"tool": action["name"] if action["name"] in known else None,
                           "ok": ok, "step": state["steps"], "repeat_id": repeat_id})
            state["recent_actions"] = recent[-8:]
            state["failures"] = 0 if ok else state["failures"] + 1
            _record(state, "action", f"{action['name']}: {ok}")
            if state["failures"] >= 3:
                state["status"], state["stop_reason"] = "blocked", "action_failures"
                break
        except LeaseLost:
            if state.get("pending_action"):
                state["status"], state["stop_reason"] = "blocked", "reconciliation_required"
                _record(state, "lease_lost", "action outcome requires reconciliation")
            else:
                _pause(state, "lease_lost")
                _record(state, "lease_lost", "stopped before the next action")
            break
        except TokenBudgetExceeded as exc:
            _pause(state, "run_budget_exhausted" if "deadline" in str(exc).lower() else "model_budget_exhausted")
            break
        except KeyboardInterrupt:
            if state.get("pending_action"):
                state["status"], state["stop_reason"] = "blocked", "reconciliation_required"
            else:
                _pause(state, "interrupted")
            _record(state, "interrupted", "action outcome unknown" if state.get("pending_action") else phase)
            raise
        except Exception as exc:
            state["failures"] += 1
            detail = error_detail(exc, phase, bool(state["pending_action"]))
            state["last_error"] = detail
            state["last_result"] = {"ok": False, "summary": detail["message"], "code": detail["code"], "phase": phase}
            state["failure_analysis"] = analyze_failure(state)
            _record(state, "failure_analyzed", state["failure_analysis"]["category"])
            _record(state, "error", f"{phase}: {detail['code']}")
            if state["pending_action"] or state["failures"] >= 3 or not detail["retryable"]:
                state["status"] = "blocked"
                state["stop_reason"] = "reconciliation_required" if state["pending_action"] else "retry_required"
                break
        finally:
            state["elapsed_run_ms"] = previous_elapsed + max(0, round((time.monotonic()-run_started)*1000))
            persist(path, state)
    return persist(path, state)
