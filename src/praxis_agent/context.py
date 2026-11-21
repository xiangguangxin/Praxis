"""Bounded structured context; required task contracts are never sliced."""
import json


class ContextBudgetError(ValueError):
    pass


def bounded_prompt(payload, *, max_chars=12000):
    if type(max_chars) is not int or max_chars < 500:
        raise ContextBudgetError("max_chars must be >=500")
    payload = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    def encoded(): return json.dumps(payload, ensure_ascii=False)
    observation = payload.get("observation", {})
    for key in ("ui_tree", "elements", "summary", "tools", "active_window", "window"):
        if len(encoded()) <= max_chars: return encoded()
        if key in observation:
            observation.pop(key)
            observation["context_omitted"] = True
    if len(encoded()) > max_chars and "previous_plan" in payload:
        plan = payload["previous_plan"] or {}
        payload["previous_plan"] = {"task_ids": [task.get("id") for task in plan.get("tasks", [])]}
    for field in ("completed_tasks", "remaining_tasks"):
        if len(encoded()) > max_chars and isinstance(payload.get(field), list):
            payload[field] = [{key: row[key] for key in ("id", "dependencies", "status") if key in row}
                              for row in payload[field]]
    if len(encoded()) > max_chars:
        payload.pop("last_action_result", None)
        payload.pop("failure_analysis", None)
        payload.pop("context", None)
    if len(encoded()) > max_chars:
        raise ContextBudgetError("Required goal, criteria and acceptance catalog exceed the context budget")
    return encoded()


def pack_context(state, observation, *, max_chars=12000, recent_events=8):
    if type(recent_events) is not int or not 1 <= recent_events <= 50:
        raise ContextBudgetError("recent_events must be 1..50")
    core = {key: state.get(key) for key in ("goal", "criteria", "status", "steps")}
    obs = {key: observation[key] for key in ("summary", "window", "active_window", "ui_tree", "elements", "tools")
           if key in observation}
    payload = {"task": core, "observation": obs, "recent_events": state.get("history", [])[-recent_events:]}
    try:
        return json.loads(bounded_prompt(payload, max_chars=max_chars))
    except ContextBudgetError:
        payload["recent_events"] = []
        return json.loads(bounded_prompt(payload, max_chars=max_chars))
