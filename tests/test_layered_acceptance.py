import tempfile
import unittest
from pathlib import Path
from praxis_agent.agent import run
from praxis_agent.planning import Task, TaskGraph, TaskPlanRuntime, TaskRuntimeError
from praxis_agent.state import create
from praxis_agent.verifiers.registry import CriteriaVerifier, VerificationRegistry


class LayeredAcceptanceTests(unittest.TestCase):
    def test_intermediate_checks_resume_and_final_delivery(self):
        stage, checked = {"value": 0}, []
        registry = VerificationRegistry()
        def check(params):
            checked.append(params["stage"])
            return {"passed": stage["value"] >= params["stage"]}
        registry.register("stage", check)
        rules = {name: {"verifier": "stage", "parameters": {"stage": value}}
                 for name, value in [("app open", 1), ("configured", 2), ("report valid", 3)]}
        verifier = CriteriaVerifier(registry, rules)
        graph = TaskGraph([Task("open", "Open", (), ("app open",)),
                           Task("configure", "Configure", ("open",), ("configured",)),
                           Task("export", "Export", ("configure",), ("report valid",))])
        class Backend:
            def observe(self):
                return {"summary": str(stage["value"]), "tools": [{"name": "advance", "inputSchema": {
                    "type": "object", "properties": {}, "additionalProperties": False}}]}
            def act(self, action):
                stage["value"] += 1
                return {"ok": True}
        class Planner:
            pause = True
            def prepare_plan(self, state, observation): return graph.to_dict()
            def decide(self, state, observation):
                active = state.get("active_task")
                if not active: return {"type": "complete"}
                expected = {"open": 1, "configure": 2, "export": 3}[active["id"]]
                if active["id"] == "configure" and self.pause:
                    self.pause = False
                    return {"type": "pause", "reason": "fixture interruption"}
                if stage["value"] >= expected: return {"type": "task_complete"}
                return {"type": "act", "action": {"name": "advance", "arguments": {}, "impact": "low"}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            create(path, "Export report", ["report valid"])
            planner = Planner()
            first = run(path, Backend(), planner, verifier=verifier)
            self.assertEqual(first["status"], "paused")
            self.assertEqual(checked, [1])
            result = run(path, Backend(), planner, verifier=verifier)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["steps"], 3)
            self.assertEqual(checked, [1, 2, 3, 3])

    def test_unknown_checks_and_missing_final_coverage_are_rejected(self):
        for acceptance in [("invented",), ("app open",)]:
            graph = TaskGraph([Task("open", "Open", (), acceptance)])
            with self.assertRaises(TaskRuntimeError):
                TaskPlanRuntime(graph.to_dict(), ["report valid"], available_criteria=["app open", "report valid"])

    def test_resume_cannot_change_trusted_catalog(self):
        graph = TaskGraph([Task("open", "Open", (), ("app open",)),
                           Task("export", "Export", ("open",), ("report valid",))])
        catalog = ["app open", "report valid"]
        runtime = TaskPlanRuntime(graph.to_dict(), ["report valid"], available_criteria=catalog)
        runtime.active()
        runtime.complete("open", {"passed": True})
        runtime.replace_remaining(runtime.graph.to_dict())
        with self.assertRaises(TaskRuntimeError):
            TaskPlanRuntime(graph.to_dict(), ["report valid"], data=runtime.to_dict(),
                            available_criteria=catalog + ["unreviewed"])
