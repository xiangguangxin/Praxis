"""Explicit, audited reconciliation of uncertain in-flight actions."""
from __future__ import annotations
from pathlib import Path
from .state import load, persist, _record
from .locking import checkpoint_lock
from .tool_trace import reconcile_last

def reconcile(path: Path, *, outcome: str, note: str) -> dict:
    if outcome not in {"executed", "not-executed"}:
        raise ValueError("outcome must be executed or not-executed")
    if not note.strip():
        raise ValueError("A human reconciliation note is required")
    with checkpoint_lock(path):
        state = load(path)
        if state["status"] != "blocked" or not state.get("pending_action"):
            raise ValueError("Only blocked in-flight actions may be reconciled")
        # Even when the action was not executed, never replay the old command:
        # re-observe and ask the planner to choose a fresh action.
        action_name = state["pending_action"]["name"]
        state["pending_action"] = None
        state["pending_approval"] = None
        state.pop("approved_action", None)
        state.pop("pending_review", None)
        state["status"] = "paused"
        state["failures"] = 0
        state["steps"] += 1
        reconcile_last(state, outcome)
        _record(state, "reconciled", f"{action_name}: {outcome}; {note[:100]}")
        return persist(path, state)


def retry(path: Path, *, note: str, max_steps=None) -> dict:
    """Reopen a known-outcome failure; uncertain actions always require reconcile."""
    if not note.strip():
        raise ValueError("An operator retry note is required")
    with checkpoint_lock(path):
        state = load(path)
        if state.get("pending_action"):
            raise ValueError("Uncertain actions require reconcile, not retry")
        if state["status"] not in {"blocked", "paused"} or state.get("pending_approval"):
            raise ValueError("Only paused or blocked goals without pending approval can be retried")
        if state.get("stop_reason") == "policy_denied":
            raise ValueError("Denied goals require a new authorized goal")
        if max_steps is not None:
            if type(max_steps) is not int or max_steps <= state["steps"]:
                raise ValueError("New step budget must exceed steps already executed")
            state["max_steps"] = max_steps
        state["failures"], state["status"] = 0, "paused"
        state["recent_actions"] = []
        state.pop("stop_reason", None)
        state.pop("pending_review", None)
        from .policy import fingerprint
        # Retain an audit fingerprint, never raw operator notes or UI content.
        _record(state, "retry_requested", fingerprint(note))
        return persist(path, state)
