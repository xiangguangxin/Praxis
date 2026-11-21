import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from praxis_agent.agent import run
from praxis_agent.context import bounded_prompt, pack_context, ContextBudgetError
from praxis_agent.context_memory import refresh_memory
from praxis_agent.planner import ModelPlanner, _context
from praxis_agent.state import create, persist, load, _record
from praxis_agent.verification import FileVerifier, _json_pointer


class ContextMemoryTests(unittest.TestCase):
    def test_failure_survives_history_retention_and_resume_without_double_counting(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state = create(path, "Inspect", ["done"])
            state.update(steps=1, recent_actions=[{"tool": "read", "ok": False, "step": 1, "repeat_id": "view-action"}])
            refresh_memory(state)
            state["recent_actions"] = []
            for index in range(150): _record(state, "observation", str(index))
            persist(path, state)
            restored = load(path)
            memory = refresh_memory(restored)
            self.assertEqual(memory["failed_approaches"][0]["count"], 1)
            self.assertEqual(memory["failed_approaches"][0]["tool"], "read")
            self.assertEqual(refresh_memory(restored)["failed_approaches"][0]["count"], 1)

    def test_evidence_index_uses_actual_checks_and_excludes_payloads(self):
        state = {"steps": 3, "history": [], "task_runtime": {
            "graph": {"status": {"open": "completed", "export": "pending"}},
            "results": {"open": {"passed": True, "checks": {"ready": {"body": "private-payload"}}}}}}
        memory = refresh_memory(state)
        fact = memory["evidence_index"][0]
        self.assertTrue(fact["historical"])
        self.assertTrue(_json_pointer(state, fact["evidence_ref"])["passed"])
        self.assertNotIn("private-payload", json.dumps(memory))
        self.assertEqual(memory["progress"]["remaining_tasks"], ["export"])
        self.assertEqual(refresh_memory({"steps": 0, "task_plan": {"status": "completed"}})["evidence_index"], [])

    def test_packing_preserves_complete_contract_and_valid_json(self):
        state = {"goal": "g"*2500, "criteria": [f"criterion-{i}" for i in range(25)]}
        result = pack_context(state, {"ui_tree": "界"*20000})
        self.assertEqual(result["task"]["goal"], state["goal"])
        self.assertEqual(result["task"]["criteria"], state["criteria"])
        with self.assertRaises(ContextBudgetError): bounded_prompt({"goal": "x"*2000}, max_chars=500)

    def test_large_plan_compacts_while_active_contract_stays_exact(self):
        plan = {"tasks": [{"id": str(i), "goal": "x"*2000} for i in range(30)]}
        state = {"goal": "Export", "criteria": ["report valid"], "steps": 8, "max_steps": 40,
                 "task_plan": plan, "active_task": {"id": "export", "goal": "Export report", "acceptance": ["report valid"]}}
        payload = json.loads(_context(state, {"summary": "x"*10000}, 5000))
        self.assertEqual(payload["active_task"], state["active_task"])
        self.assertEqual(payload["criteria"], state["criteria"])
        self.assertIn("definitions_ref", payload["task_plan"])
        self.assertEqual(len(plan["tasks"][0]["goal"]), 2000)

    def test_oversized_catalog_pauses_before_model_dispatch(self):
        class Backend:
            def observe(self): return {"summary": "ready"}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "state.json"
            create(path, "Inspect", ["done"])
            rules = {"done": {"path": "report.txt"}}
            rules.update({f"{i}-"+"x"*500: {"path": "report.txt"} for i in range(40)})
            with patch("urllib.request.urlopen") as request:
                result = run(path, Backend(), ModelPlanner("fixture", "key", "https://fixture.test"), verifier=FileVerifier(rules, root))
            self.assertEqual(result["stop_reason"], "context_budget_exhausted")
            request.assert_not_called()
