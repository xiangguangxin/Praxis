"""Deterministic checkpoint memory derived from runtime evidence, never model claims."""
from .diagnostics import safe_summary
from .policy import fingerprint


def refresh_memory(state):
    memory = state.setdefault("context_memory", {"version": 1, "failed_approaches": [], "seen": []})
    if not isinstance(memory, dict) or memory.get("version") != 1:
        raise ValueError("Unsupported context memory")
    if (not isinstance(memory.get("failed_approaches"), list) or not isinstance(memory.get("seen"), list)
            or any(not isinstance(row, dict) or "id" not in row or type(row.get("count")) is not int
                   for row in memory["failed_approaches"])):
        raise ValueError("Corrupt context memory")
    runtime = state.get("task_runtime") or {}
    graph = runtime.get("graph") or {}
    statuses = graph.get("status", {})
    completed = [name for name, status in statuses.items() if status == "completed"]
    facts = []
    for name in completed:
        result = runtime.get("results", {}).get(name, {})
        if result.get("passed") is True:
            facts.append({"task_id": name, "checks": len(result.get("checks", {})),
                          "historical": True, "evidence_ref": "/task_runtime/results/" + name.replace("~", "~0").replace("/", "~1")})
    active = state.get("active_task") or {}
    memory["progress"] = {"completed_tasks": completed, "remaining_tasks": [name for name in statuses if name not in completed],
                          "active_task": active.get("id"), "steps": state["steps"]}
    memory["summary"] = f'{len(completed)} of {len(statuses)} tasks independently completed; {state["steps"]} tool actions recorded.'
    memory["evidence_index"] = facts[-24:]
    if state.get("artifact_verification"):
        memory["final_check_ref"] = "/artifact_verification"
    failures, seen = memory.setdefault("failed_approaches", []), memory.setdefault("seen", [])
    def record(event_id, signature, tool, reason, step):
        if event_id in seen: return
        seen.append(event_id)
        item = next((row for row in failures if row["id"] == signature), None)
        if item is None:
            item = {"id": signature, "tool": tool, "reason": reason, "count": 0}
        else:
            failures.remove(item)
        item["count"] += 1
        item["last_step"] = step
        failures.append(item)
    for action in state.get("recent_actions", []):
        if action.get("ok") is False:
            signature = fingerprint([action.get("tool"), action.get("repeat_id")])
            event_id = fingerprint(["action", action.get("step"), signature])
            record(event_id, signature, action.get("tool"), "action_failed", action.get("step", 0))
    for event in state.get("history", []):
        if event.get("event") in {"error", "task_acceptance_failed", "task_replan_failed"}:
            reason = safe_summary(event.get("detail", ""), 160)
            signature = fingerprint([event["event"], reason])
            event_id = fingerprint([event.get("sequence"), event.get("time"), signature])
            record(event_id, signature, None, reason, state["steps"])
    memory["failed_approaches"], memory["seen"] = failures[-24:], seen[-256:]
    return memory


def memory_view(state):
    memory = refresh_memory(state)
    return {"source": "runtime_derived_historical_evidence", "summary": memory["summary"],
            "progress": memory["progress"], "historical_evidence": memory["evidence_index"][-12:],
            "failed_approaches": memory["failed_approaches"][-12:],
            "instruction": "Historical checks and failed attempts are context only. Reobserve current state and reverify final delivery."}
