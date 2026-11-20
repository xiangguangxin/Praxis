"""Opt-in, independently verified experience hints with conservative lifecycle."""
from __future__ import annotations
import json
import math
import os
import re
import tempfile
import time
from functools import wraps
from .locking import checkpoint_lock
from pathlib import Path
from .state import load
from .storage import atomic_json_write

DAY = 86400
DEFAULT_TTL_DAYS = 90
DEFAULT_HALF_LIFE_DAYS = 30
MIN_SCORE = 0.20

def _terms(text):
    # Unicode words + individual CJK characters: deterministic, no embedding API.
    return set(re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]", text.casefold()))

def _writer(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with checkpoint_lock(self.path):
            return method(self, *args, **kwargs)
    return locked


def _notes(values, name):
    if values is None:
        return []
    if not isinstance(values, (list, tuple)) or len(values) > 8 or any(
            not isinstance(v, str) or not v.strip() or len(v) > 200 for v in values):
        raise ValueError(f"{name}: at most eight nonempty notes of 200 characters")
    return list(values)

class ExperienceStore:
    def __init__(self, path: Path):
        self.path = path

    def _read(self):
        if not self.path.exists():
            return {"version": 1, "experiences": []}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("experiences"), list):
            raise ValueError("Unsupported experience store")
        return data

    def _write(self, data):
        atomic_json_write(self.path, data)

    @staticmethod
    def _effective(item, now):
        if item.get("source") != "independently-verified" or item.get("status", "active") != "active":
            return 0.0
        created = item.get("recorded_at")
        if type(created) not in (float, int) or not math.isfinite(created) or created > now + DAY:
            return 0.0
        age = max(0, now - created)
        ttl = item.get("ttl_days", DEFAULT_TTL_DAYS)
        half = item.get("half_life_days", DEFAULT_HALF_LIFE_DAYS)
        if type(ttl) not in (float, int) or type(half) not in (float, int) or not math.isfinite(ttl) or not math.isfinite(half) or ttl <= 0 or half <= 0:
            return 0.0
        if age >= ttl * DAY:
            return 0.0
        successes = item.get("successes", 0)
        failures = item.get("failures", 0)
        if type(successes) is not int or type(failures) is not int or successes < 0 or failures < 0:
            return 0.0
        reliability = (successes + 1) / (successes + failures + 1)
        return 2 ** (-age / (half * DAY)) * reliability

    @_writer
    def add_verified(self, checkpoint: Path, *, verifier, environment: str, note: str,
                     preconditions=None, key_steps=None, failure_modes=None, event_id=None) -> dict:
        state = load(checkpoint)
        if state["status"] != "completed":
            raise ValueError("Only completed tasks can become experiences")
        if not environment.strip() or not note.strip():
            raise ValueError("Environment and operator note required")
        from .verification import FileVerifier
        if not isinstance(verifier, FileVerifier):
            raise ValueError("Experience curation requires independent file verification")
        evaluation = verifier.evaluate(state)
        passed = evaluation["passed"]
        if not passed:
            raise ValueError("Independent verifier did not pass")
        item = {
            "id": state["id"], "goal": state["goal"][:1000],
            "criteria": state["criteria"][:20], "environment": environment[:200],
            "note": note[:500], "steps": state["steps"],
            "recorded_at": time.time(), "source": "independently-verified",
            "status": "active", "successes": 0, "failures": 0,
            "ttl_days": DEFAULT_TTL_DAYS, "half_life_days": DEFAULT_HALF_LIFE_DAYS,
            "preconditions": _notes(preconditions, "preconditions"),
            "key_steps": _notes(key_steps, "key_steps"),
            "failure_modes": _notes(failure_modes, "failure_modes"),
            "evidence": {"checkpoint_id": state["id"], "verifier_id": verifier.identity,
                         "checked_at": time.time(), "passed_checks": len(evaluation["checks"]),
                         "artifact_hashes": [check["sha256"] for check in evaluation["checks"].values()
                                             if "sha256" in check][:20]},
        }
        data = self._read()
        existing = next((entry for entry in data["experiences"] if entry.get("id") == item["id"]), None)
        if existing is not None:
            evidence = existing.get("evidence", {})
            if (event_id and evidence.get("checkpoint_id") == state["id"]
                    and evidence.get("verifier_id") == verifier.identity
                    and existing.get("source") == "independently-verified"):
                return existing
            raise ValueError("Experience already recorded or ID has conflicting provenance")
        if event_id:
            if not isinstance(event_id, str) or not event_id:
                raise ValueError("event_id must be a nonempty string")
            item["source_event_id"] = event_id
        data["experiences"].append(item)
        self._write(data)
        return item

    def search(self, query: str, *, environment: str, limit: int = 5, now=None) -> list[dict]:
        if not query.strip() or not environment.strip() or limit < 1:
            return []
        now = time.time() if now is None else now
        terms = _terms(query)
        matches = []
        for item in self._read()["experiences"]:
            if item.get("environment") != environment:
                continue
            strength = self._effective(item, now)
            overlap = len(terms & _terms(item.get("goal", "")))
            if strength >= MIN_SCORE and overlap:
                score = strength * overlap / max(1, len(terms))
                matches.append((score, item))
        matches.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
        return [dict(item, effective_score=round(score, 4)) for score, item in matches[:limit]]

    @_writer
    def feedback(self, experience_id: str, *, outcome: str, note: str = "") -> dict:
        """Explicit verified feedback only; do not infer failure from planner errors."""
        if outcome not in ("success", "failure") or not note.strip():
            raise ValueError("Outcome and audit note required")
        data = self._read()
        for item in data["experiences"]:
            if item.get("id") == experience_id:
                if item.get("status", "active") != "active":
                    raise ValueError("Inactive experience cannot receive feedback")
                field = "successes" if outcome == "success" else "failures"
                item[field] = item.get(field, 0) + 1
                item["last_feedback_at"] = time.time()
                item["last_feedback_note"] = note[:200]
                if outcome == "failure" and item["failures"] >= 2:
                    item["status"] = "invalidated"
                    item["invalidated_reason"] = "two verified failures"
                self._write(data)
                return item
        raise KeyError(experience_id)

    @_writer
    def invalidate(self, experience_id: str, *, reason: str) -> dict:
        if not reason.strip():
            raise ValueError("Reason required")
        data = self._read()
        for item in data["experiences"]:
            if item.get("id") == experience_id:
                item["status"] = "invalidated"
                item["invalidated_reason"] = reason[:200]
                self._write(data)
                return item
        raise KeyError(experience_id)

    @_writer
    def expire(self, *, now=None) -> int:
        """Persist time-expired records as invalidated; no silent deletion."""
        now = time.time() if now is None else now
        data = self._read()
        count = 0
        for item in data["experiences"]:
            if item.get("status", "active") == "active" and self._effective(item, now) == 0:
                item["status"] = "invalidated"
                item["invalidated_reason"] = "expired or invalid lifecycle metadata"
                count += 1
        if count:
            self._write(data)
        return count
