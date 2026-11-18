"""Version-1 checkpoint compatibility."""
from __future__ import annotations
import json, os, tempfile, time, uuid
from pathlib import Path
from typing import Any
TERMINAL = {"completed", "failed", "blocked"}
ALLOWED = {"planning", "running", "paused", "blocked", "completed", "failed"}
def _save(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".goal-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


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
    _save(path, state)
    return state


def load(path: Path) -> dict[str, Any]:
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("version") not in (1, 2) or state.get("status") not in ALLOWED:
        raise ValueError("Unsupported or corrupt checkpoint")
    if not isinstance(state.get("id"), str) or not isinstance(state.get("criteria"), list) or not isinstance(state.get("steps"), int):
        raise ValueError("Corrupt checkpoint structure")
    if (not isinstance(state.get("goal"), str) or not state["goal"].strip()
            or not state["criteria"] or any(not isinstance(c, str) or not c.strip() for c in state["criteria"])
            or type(state["steps"]) is not int or state["steps"] < 0
            or type(state.get("max_steps")) is not int or state["max_steps"] < 1
            or type(state.get("failures")) is not int or state["failures"] < 0
            or not isinstance(state.get("history"), list)
            or any(key not in state for key in ("pending_action", "pending_approval"))):
        raise ValueError("Corrupt checkpoint structure")
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


