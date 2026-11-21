import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from praxis_agent.agent import run
from praxis_agent.experience import ExperienceStore
from praxis_agent.hint_tracking import hint_outcomes
from praxis_agent.planner import ModelPlanner, TokenBudgetExceeded
from praxis_agent.planning import Task, TaskGraph
from praxis_agent.state import create, load, persist
from praxis_agent.verification import FileVerifier


class Backend:
    def observe(self): return {"summary": "ready"}


class CompletePlanner:
    def decide(self, *args): return {"type": "complete"}


class HintTrackingTests(unittest.TestCase):
    def fixture(self, root):
        (root / "report.txt").write_text("PASS", encoding="utf-8")
        verifier = FileVerifier({"report": {"path": "report.txt", "contains": "PASS"}}, root)
        source = root / "source.json"
        create(source, "Inspect report", ["report"])
        state = run(source, Backend(), CompletePlanner(), verifier=verifier)
        store = ExperienceStore(root / "experiences.json")
        item = store.add_verified(source, verifier=verifier, environment="fixture", note="private-operator-note")
        path = root / "run.json"
        create(path, "Inspect report", ["report"])
        planner = ModelPlanner("fixture", "key", "https://fixture.test", experience_store=store, experience_environment="fixture")
        planner.prepare_plan = lambda state, observation: TaskGraph([Task("check", "Inspect", (), ("report",))]).to_dict()
        def response(kind):
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps({"type": kind})}}],
                                          "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode())
        with patch("urllib.request.urlopen", side_effect=[response("task_complete"), response("complete")]):
            result = run(path, Backend(), planner, verifier=verifier)
        self.assertEqual(result["status"], "completed")
        return store, item, path, verifier

    def test_actual_exposure_is_checkpointed_without_hint_content(self):
        with tempfile.TemporaryDirectory() as folder:
            store, item, path, verifier = self.fixture(Path(folder))
            state = load(path)
            self.assertEqual(len(state["hint_exposures"]), 2)
            self.assertTrue(all(h["id"] == item["id"] for h in state["hint_exposures"]))
            self.assertNotIn("private-operator-note", json.dumps(state))
            report = hint_outcomes(Path(folder))
            group = report["groups"][0]
            self.assertEqual(group["distinct_runs"], 1)
            self.assertEqual(group["verified_completions"], 1)
            self.assertFalse(report["causal_attribution"])

    def test_verified_feedback_is_idempotent_and_conflicts_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            store, item, path, verifier = self.fixture(Path(folder))
            first = store.feedback_run(item["id"], path, verifier=verifier, outcome="success", note="Operator observed the hint helped")
            again = store.feedback_run(item["id"], path, verifier=verifier, outcome="success", note="Retry delivery")
            self.assertEqual(first, again)
            self.assertEqual(store._read()["experiences"][0]["successes"], 1)
            with self.assertRaises(ValueError):
                store.feedback_run(item["id"], path, verifier=verifier, outcome="failure", note="Conflicting receipt")

    def test_missing_exposure_stale_artifact_or_uncertain_action_cannot_receive_feedback(self):
        for mode in ("missing", "stale", "uncertain"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                store, item, path, verifier = self.fixture(root)
                state = load(path)
                if mode == "missing": state["hint_exposures"] = []
                elif mode == "stale": (root / "report.txt").write_text("FAIL", encoding="utf-8")
                else: state["pending_action"] = {"name": "unknown"}
                persist(path, state)
                with self.assertRaises(ValueError):
                    store.feedback_run(item["id"], path, verifier=verifier, outcome="success", note="Operator attribution")
                self.assertEqual(store._read()["experiences"][0]["successes"], 0)

    def test_budget_failure_before_dispatch_does_not_claim_exposure(self):
        with tempfile.TemporaryDirectory() as folder:
            store, item, path, verifier = self.fixture(Path(folder))
            planner = ModelPlanner("fixture", "key", "https://fixture.test", max_tokens=1,
                                   experience_store=store, experience_environment="fixture")
            with self.assertRaises(TokenBudgetExceeded):
                planner.decide({"goal": "Inspect report", "criteria": ["report"], "steps": 0, "max_steps": 5}, {})
            self.assertEqual(planner.hint_exposures, [])

    def test_two_distinct_attributed_failures_invalidate_experience(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, item, path, verifier = self.fixture(root)
            exposures = load(path)["hint_exposures"]
            (root / "report.txt").write_text("FAIL", encoding="utf-8")
            for index in range(2):
                failed_path = root / f"failure-{index}.json"
                state = create(failed_path, "Inspect report", ["report"])
                state.update(status="blocked", hint_exposures=exposures, verifier_id=verifier.identity)
                persist(failed_path, state)
                store.feedback_run(item["id"], failed_path, verifier=verifier,
                                   outcome="failure", note="Operator attributed the failed approach")
            self.assertEqual(store._read()["experiences"][0]["status"], "invalidated")
            self.assertEqual(store.search("Inspect report", environment="fixture"), [])
