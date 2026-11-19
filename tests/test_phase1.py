import tempfile
import unittest
from pathlib import Path

from praxis_agent.planning import Task, TaskGraph, TaskGraphError
from praxis_agent.tools import ToolRegistry, ToolSpec, ToolError, RegisteredToolsBackend
from praxis_agent.verifiers.registry import VerificationRegistry, CriteriaVerifier
from praxis_agent.state import create
from praxis_agent.agent import run


class Phase1Tests(unittest.TestCase):
    def test_dag_dependencies_round_trip_and_replan(self):
        graph = TaskGraph([Task("build", "Compile"), Task("test", "Run tests", ("build",)),
                           Task("report", "Generate report", ("test",))])
        self.assertEqual([t.id for t in graph.ready()], ["build"])
        graph.replan("build", "Compile with instrumentation")
        graph.transition("build", "running")
        graph.transition("build", "completed")
        self.assertEqual([t.id for t in graph.ready()], ["test"])
        restored = TaskGraph.from_dict(graph.to_dict())
        self.assertEqual(restored.tasks["build"].goal, "Compile with instrumentation")
        with self.assertRaises(TaskGraphError):
            restored.transition("report", "running")

    def test_cycle_and_duplicate_rejected(self):
        for tasks in ([Task("a", "A", ("b",)), Task("b", "B", ("a",))],
                      [Task("a", "A"), Task("a", "B")]):
            with self.assertRaises(TaskGraphError):
                TaskGraph(tasks)

    def test_registry_validates_before_dispatch(self):
        calls = []
        registry = ToolRegistry()
        registry.register(ToolSpec("read", lambda args: calls.append(args["path"]) or {"ok": True},
                                   {"path": str}, impact="low"))
        registry.register(ToolSpec("delete", lambda args: {"ok": True}, {"path": str}, impact="high"))
        backend = RegisteredToolsBackend(registry, lambda: {"summary": "ready"})
        self.assertEqual(len(backend.observe()["tools"]), 2)
        self.assertTrue(backend.act({"name": "read", "arguments": {"path": "report.json"}})["ok"])
        self.assertEqual(calls, ["report.json"])
        with self.assertRaises(ToolError):
            backend.act({"name": "read", "arguments": {"path": 123}})
        with self.assertRaises(PermissionError):
            backend.act({"name": "delete", "arguments": {"path": "report.json"}})

    def test_independent_verifier_blocks_false_completion(self):
        registry = VerificationRegistry()
        registry.register("check", lambda params: {"passed": params["ok"], "evidence": {"source": "trusted"}})
        verifier = CriteriaVerifier(registry, {"report": {"verifier": "check", "parameters": {"ok": True}}})
        class Desktop:
            def observe(self):
                return {"summary": "ready"}
            def act(self, action):
                raise AssertionError("No action expected")
        class Planner:
            def decide(self, state, observation):
                return {"type": "complete", "evidence": {"report": False}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            create(path, "Verify report", ["report"])
            self.assertEqual(run(path, Desktop(), Planner(), verifier=verifier)["status"], "completed")
            denied = CriteriaVerifier(registry, {"report": {"verifier": "check", "parameters": {"ok": False}}})
            other = Path(folder) / "other.json"
            create(other, "Verify report", ["report"])
            self.assertNotEqual(run(other, Desktop(), Planner(), verifier=denied)["status"], "completed")

    def test_registered_backend_and_verifier_work_at_final_action_budget(self):
        calls = []
        tools = ToolRegistry()
        tools.register(ToolSpec("read", lambda args: calls.append(args["path"]) or {"ok": True},
                                {"path": str}, impact="low"))
        backend = RegisteredToolsBackend(tools, lambda: {"summary": "fixture"})
        checks = VerificationRegistry()
        checks.register("observed", lambda params: {"passed": calls == ["report.json"], "evidence": {"calls": len(calls)}})
        verifier = CriteriaVerifier(checks, {"report": {"verifier": "observed"}})
        class Planner:
            def decide(self, state, observation):
                return {"type": "act", "action": {"name": "read", "impact": "low", "arguments": {"path": "report.json"}}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"state.json"
            create(path, "Inspect fixture", ["report"], max_steps=1)
            result = run(path, backend, Planner(), verifier=verifier)
            self.assertEqual((result["status"], result["steps"]), ("completed", 1))
            self.assertEqual(result["verification"]["source"], "criteria_registry")
            self.assertTrue(result["artifact_verification"]["passed"])
