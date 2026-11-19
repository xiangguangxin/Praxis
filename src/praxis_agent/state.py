"""Version-1 checkpoint compatibility."""
from __future__ import annotations
import json, time, uuid
from .storage import atomic_json_write
from pathlib import Path
from typing import Any
TERMINAL = {"completed", "failed", "blocked"}
ALLOWED = {"planning", "running", "paused", "blocked", "completed", "failed"}
def _save(path: Path, state: dict[str, Any]) -> None:
    atomic_json_write(path, state)


def create(path: Path, goal: str, criteria: list[str], *, max_steps: int = 40) -> dict[str, Any]:
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite checkpoint: {path}")
    if (not isinstance(goal, str) or not goal.strip() or len(goal) > 2000
            or not isinstance(criteria, list) or not 1 <= len(criteria) <= 20
            or any(not isinstance(c, str) or not c.strip() or len(c) > 500 for c in criteria)
            or len(set(criteria)) != len(criteria) or type(max_steps) is not int or max_steps < 1):
        raise ValueError("Goal, criteria and positive step budget required")
    state = {
        "version": 2, "id": str(uuid.uuid4()), "goal": goal,
        "criteria": criteria, "status": "planning", "steps": 0,
        "max_steps": max_steps, "failures": 0, "last_observation": None,
        "last_result": None, "pending_action": None, "pending_approval": None,
        "history": [], "verification_level": "unverified", "recent_actions": [], "created_at": time.time(), "updated_at": time.time(),
    }
    atomic_json_write(path, state, create_only=True)
    return state


def load(path: Path) -> dict[str, Any]:
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or type(state.get("version")) is not int or state.get("version") not in (1, 2) or not isinstance(state.get("status"), str) or state.get("status") not in ALLOWED:
        raise ValueError("Unsupported or corrupt checkpoint")
    if not isinstance(state.get("id"), str) or not state["id"] or not isinstance(state.get("criteria"), list) or not isinstance(state.get("steps"), int):
        raise ValueError("Corrupt checkpoint structure")
    if (not isinstance(state.get("goal"), str) or not state["goal"].strip()
            or not state["criteria"] or any(not isinstance(c, str) or not c.strip() for c in state["criteria"])
            or len(set(state["criteria"])) != len(state["criteria"])
            or type(state["steps"]) is not int or state["steps"] < 0
            or type(state.get("max_steps")) is not int or state["max_steps"] < 1
            or type(state.get("failures")) is not int or state["failures"] < 0
            or not isinstance(state.get("history"), list)
            or any(key not in state for key in ("pending_action", "pending_approval"))):
        raise ValueError("Corrupt checkpoint structure")
    pending = state.get("pending_action")
    if pending is not None and (not isinstance(pending, dict) or not isinstance(pending.get("name"), str) or not pending["name"]):
        raise ValueError("Corrupt pending action")
    if state["pending_approval"] is not None and not isinstance(state["pending_approval"], str):
        raise ValueError("Corrupt pending approval")
    recent = state.get("recent_actions", [])
    if not isinstance(recent, list) or len(recent) > 8 or any(not isinstance(a, dict) for a in recent):
        raise ValueError("Corrupt recent actions")
    review = state.get("pending_review")
    if review is not None and (not isinstance(review, dict)
            or not isinstance(review.get("id"), str) or not isinstance(review.get("arguments"), dict)
            or type(review.get("expires_at")) not in (int, float)):
        raise ValueError("Corrupt pending review")
    for key in ("last_result", "last_observation", "artifact_verification", "model_usage", "tool_selection"):
        if state.get(key) is not None and not isinstance(state[key], dict):
            raise ValueError("Corrupt checkpoint metadata")
    usage = state.get("model_usage", {})
    if any(type(value) is not int or value < 0 for value in usage.values()):
        raise ValueError("Corrupt model usage counters")
    baseline = state.get("artifact_baseline")
    if baseline is not None and (not isinstance(baseline, dict) or not isinstance(baseline.get("verifier"), str)
                                 or not isinstance(baseline.get("sha256"), dict)):
        raise ValueError("Corrupt artifact baseline")
    # Also rejects nested NaN/Infinity accepted by Python's default JSON decoder.
    json.dumps(state, allow_nan=False)
    state.setdefault("verification_level", "legacy_unspecified" if state["status"] == "completed" else "unverified")
    state.setdefault("recent_actions", [])
    # v1 is readable in-memory; migrated to v2 only on next intentional write.
    if state["version"] == 1:
        state["version"] = 2
    return state


def persist(path: Path, state: dict[str, Any]) -> dict[str, Any]:
    state["version"] = 2
    state["updated_at"] = time.time()
    _save(path, state)
    return state


def _record(state: dict[str, Any], event: str, detail: Any = None) -> None:
    # No raw screenshots, control trees, credentials, or action payloads on disk.
    state["history"].append({"time": time.time(), "event": event,
                             "detail": str(detail or "")[:160]})
    state["history"] = state["history"][-100:]


