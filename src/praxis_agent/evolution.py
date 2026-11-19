"""Versioned, evidence-gated strategy learning.

Strategies are inert candidates until paired benchmark evidence passes the promotion
gate. Only promoted strategies are exposed to the planner, and every hint remains
untrusted advice rather than an executable action.
"""
from __future__ import annotations
import hashlib
import json
import time
import uuid
from pathlib import Path
from .experience import ExperienceStore, _terms
from .locking import checkpoint_lock
from .storage import atomic_json_write

class EvolutionError(ValueError):
    pass

class EvolutionStore:
    VERSION = 1
    MIN_PAIRS = 10
    MIN_IMPROVEMENT = 0.10

    def __init__(self, path: Path, experience_store: ExperienceStore | None = None):
        self.path = Path(path)
        self.experience_store = experience_store

    def _read(self):
        if not self.path.exists():
            return {"version": self.VERSION, "strategies": []}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("version") != self.VERSION
                or not isinstance(data.get("strategies"), list)):
            raise EvolutionError("Unsupported evolution store")
        ids = [item.get("id") for item in data["strategies"] if isinstance(item, dict)]
        if len(ids) != len(data["strategies"]) or len(set(ids)) != len(ids):
            raise EvolutionError("Corrupt evolution store")
        return data

    def _write(self, data):
        atomic_json_write(self.path, data)

    @staticmethod
    def _report(value):
        if isinstance(value, (str, Path)):
            value = json.loads(Path(value).read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("provenance"), dict):
            raise EvolutionError("Benchmark report with provenance is required")
        records = value.get("records")
        if not isinstance(records, list) or not records:
            raise EvolutionError("Benchmark report contains no records")
        return value

    @staticmethod
    def _signature(report):
        provenance = report["provenance"]
        required = ("suite_sha256", "runtime_source_sha256", "runtime_package_version",
                    "python", "platform", "repeat")
        if any(key not in provenance for key in required):
            raise EvolutionError("Benchmark provenance is incomplete")
        return {key: provenance[key] for key in required} | {
            "model": report.get("model"),
            "model_connection": provenance.get("model_connection"),
            "model_budget": provenance.get("model_budget"),
        }

    @staticmethod
    def _matched(baseline, candidate):
        def index(report):
            result = {}
            for row in report["records"]:
                if not isinstance(row, dict) or not isinstance(row.get("scenario"), str):
                    raise EvolutionError("Malformed benchmark record")
                key = (row["scenario"], row.get("iteration"))
                if key in result or type(row.get("passed")) is not bool:
                    raise EvolutionError("Duplicate or invalid benchmark record")
                result[key] = row
            return result
        left, right = index(baseline), index(candidate)
        if set(left) != set(right):
            raise EvolutionError("Baseline and candidate must contain identical scenario/iteration pairs")
        pairs = [(left[key], right[key]) for key in sorted(left)]
        return pairs

    def propose(self, *, goal: str, environment: str, instruction: str,
                source_experience_id: str, rationale: str) -> dict:
        for value, name, limit in ((goal, "goal", 500), (environment, "environment", 200),
                                   (instruction, "instruction", 600), (rationale, "rationale", 400)):
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise EvolutionError(f"{name} must be nonempty and at most {limit} characters")
        if not isinstance(source_experience_id, str) or not source_experience_id:
            raise EvolutionError("A verified source experience is required")
        if self.experience_store is None:
            raise EvolutionError("An ExperienceStore is required to ground strategy proposals")
        source = next((item for item in self.experience_store._read()["experiences"]
                       if item.get("id") == source_experience_id), None)
        if (source is None or source.get("source") != "independently-verified"
                or source.get("status", "active") != "active"):
            raise EvolutionError("Strategy source must be an active independently verified experience")
        item = {"id": str(uuid.uuid4()), "goal": goal, "environment": environment,
                "instruction": instruction, "rationale": rationale,
                "source_experience_id": source_experience_id, "status": "candidate",
                "created_at": time.time(), "evaluation": None, "promoted_at": None}
        with checkpoint_lock(self.path):
            data = self._read()
            data["strategies"].append(item)
            self._write(data)
        return item

    def evaluate_variants(self, strategy_id: str, report, *, baseline_variant="baseline",
                          candidate_variant="strategy_candidate") -> dict:
        report = self._report(report)
        def selected(name):
            subset = [row for row in report["records"] if row.get("variant") == name]
            if not subset:
                raise EvolutionError("Benchmark report is missing variant: " + name)
            return {**report, "records": subset}
        return self.evaluate(strategy_id, selected(baseline_variant), selected(candidate_variant))

    def evaluate(self, strategy_id: str, baseline_report, candidate_report) -> dict:
        baseline, candidate = self._report(baseline_report), self._report(candidate_report)
        if self._signature(baseline) != self._signature(candidate):
            raise EvolutionError("Benchmark provenance mismatch; use the same suite, runtime, model and budget")
        pairs = self._matched(baseline, candidate)
        if len(pairs) < self.MIN_PAIRS:
            raise EvolutionError(f"At least {self.MIN_PAIRS} paired runs are required")
        wins = losses = ties = 0
        safety_regressions = []
        trajectory_pairs = 0
        trajectory_wins = trajectory_losses = 0
        for left, right in pairs:
            wins += right["passed"] and not left["passed"]
            losses += left["passed"] and not right["passed"]
            ties += left["passed"] == right["passed"]
            if left.get("completed_and_verified") and not right.get("completed_and_verified"):
                safety_regressions.append(left["scenario"])
            if "trajectory" in left and "trajectory" in right:
                trajectory_pairs += 1
                trajectory_wins += right["trajectory"].get("passed") is True and left["trajectory"].get("passed") is not True
                trajectory_losses += left["trajectory"].get("passed") is True and right["trajectory"].get("passed") is not True
        baseline_rate = sum(row["passed"] for row, _ in pairs) / len(pairs)
        candidate_rate = sum(row["passed"] for _, row in pairs) / len(pairs)
        improvement = candidate_rate - baseline_rate
        passed = (improvement >= self.MIN_IMPROVEMENT and wins > losses
                  and not safety_regressions and trajectory_losses <= trajectory_wins)
        evaluation = {
            "passed": bool(passed), "paired_runs": len(pairs), "wins": wins, "losses": losses, "ties": ties,
            "baseline_success_rate": round(baseline_rate, 4),
            "candidate_success_rate": round(candidate_rate, 4),
            "improvement": round(improvement, 4),
            "trajectory_pairs": trajectory_pairs, "trajectory_wins": trajectory_wins,
            "trajectory_losses": trajectory_losses,
            "safety_regressions": sorted(set(safety_regressions)),
            "baseline_report_sha256": hashlib.sha256(json.dumps(baseline, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            "candidate_report_sha256": hashlib.sha256(json.dumps(candidate, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            "checked_at": time.time(),
        }
        with checkpoint_lock(self.path):
            data = self._read()
            item = self._find(data, strategy_id)
            if item["status"] != "candidate":
                raise EvolutionError("Only candidate strategies can be evaluated")
            item["evaluation"] = evaluation
            self._write(data)
        return evaluation

    @staticmethod
    def _find(data, strategy_id):
        for item in data["strategies"]:
            if item["id"] == strategy_id:
                return item
        raise KeyError(strategy_id)

    def promote(self, strategy_id: str) -> dict:
        with checkpoint_lock(self.path):
            data = self._read()
            item = self._find(data, strategy_id)
            if item["status"] != "candidate" or not item.get("evaluation", {}).get("passed"):
                raise EvolutionError("Promotion requires a passing paired benchmark evaluation")
            item["status"], item["promoted_at"] = "promoted", time.time()
            self._write(data)
            return item

    def rollback(self, strategy_id: str, *, reason: str) -> dict:
        if not isinstance(reason, str) or not reason.strip():
            raise EvolutionError("Rollback reason is required")
        with checkpoint_lock(self.path):
            data = self._read()
            item = self._find(data, strategy_id)
            if item["status"] != "promoted":
                raise EvolutionError("Only promoted strategies can be rolled back")
            item["status"], item["rollback_reason"] = "rolled_back", reason[:300]
            item["rolled_back_at"] = time.time()
            self._write(data)
            return item

    def search(self, query: str, *, environment: str, limit: int = 2,
               include_candidate_id: str | None = None) -> list[dict]:
        if not isinstance(query, str) or not query.strip() or not isinstance(environment, str) or not environment.strip():
            return []
        if type(limit) is not int or not 1 <= limit <= 5:
            raise EvolutionError("limit must be 1..5")
        terms = _terms(query)
        matches = []
        for item in self._read()["strategies"]:
            eligible = item.get("status") == "promoted" or (
                include_candidate_id is not None and item.get("id") == include_candidate_id
                and item.get("status") == "candidate")
            if not eligible or item.get("environment") != environment:
                continue
            overlap = len(terms & _terms(item.get("goal", "")))
            if overlap:
                matches.append((overlap / max(1, len(terms)), item))
        matches.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
        return [dict(item, match_score=round(score, 4)) for score, item in matches[:limit]]

    def list(self, *, status=None):
        items = self._read()["strategies"]
        return [item for item in items if status is None or item.get("status") == status]
