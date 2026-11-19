import tempfile
import unittest
from pathlib import Path
from praxis_agent.agent import run
from praxis_agent.state import create
from praxis_agent.planning import Task, TaskGraph, TaskPlanRuntime, TaskRuntimeError
from praxis_agent.verifiers.registry import VerificationRegistry, CriteriaVerifier

class TaskPlanRuntimeTests(unittest.TestCase):
    def test_task_lifecycle_retries_and_unlocks_dependencies(self):
        graph=TaskGraph([Task("a","A",(),("build output exists",)),
                         Task("b","B",("a",),("report exists",))])
        runtime=TaskPlanRuntime(graph.to_dict(),["build output exists","report exists"])
        first=runtime.active()
        self.assertEqual(first.id,"a")
        runtime.reject("a",{"passed":False})
        self.assertEqual(runtime.graph.status["a"],"pending")
        first=runtime.active()
        runtime.complete(first.id,{"passed":True})
        self.assertEqual(runtime.active().id,"b")
        runtime.complete("b",{"passed":True})
        self.assertEqual(runtime.status,"completed")

    def test_task_acceptance_must_cover_goal_criteria(self):
        graph=TaskGraph([Task("a","A",(),("one",))])
        with self.assertRaises(TaskRuntimeError):
            TaskPlanRuntime(graph.to_dict(),["one","two"])

    def test_runtime_checkpoint_round_trip_preserves_active_task(self):
        graph=TaskGraph([Task("a","A",(),("one",))])
        runtime=TaskPlanRuntime(graph.to_dict(),["one"])
        runtime.active()
        restored=TaskPlanRuntime(graph.to_dict(),["one"],data=runtime.to_dict())
        self.assertEqual(restored.active().id,"a")
        self.assertEqual(restored.attempts["a"],1)

    def test_replan_preserves_completed_nodes_and_replaces_unfinished_nodes(self):
        graph=TaskGraph([Task("build","Build",(),("build output exists",)),
                         Task("report","Report",("build",),("report exists",))])
        runtime=TaskPlanRuntime(graph.to_dict(),["build output exists","report exists"],max_attempts=1)
        runtime.active()
        runtime.complete("build",{"passed":True})
        runtime.active()
        runtime.reject("report",{"passed":False})
        replacement=TaskGraph([Task("build","Build",(),("build output exists",)),
                               Task("new-report","Revised report",("build",),("report exists",))],
                              {"build":"completed","new-report":"pending"})
        runtime.replace_remaining(replacement.to_dict())
        self.assertEqual(runtime.graph.status,{"build":"completed","new-report":"pending"})
        self.assertNotIn("report",runtime.graph.tasks)
        self.assertEqual(runtime.results["build"],{"passed":True})

    def test_invalid_replan_does_not_mutate_existing_graph(self):
        graph=TaskGraph([Task("build","Build",(),("build output exists",)),
                         Task("report","Report",("build",),("report exists",))])
        runtime=TaskPlanRuntime(graph.to_dict(),["build output exists","report exists"])
        before=runtime.to_dict()
        invalid=TaskGraph([Task("build","Build",(),("build output exists",)),
                           Task("new-report","Revised report",("build",),("not a goal criterion",))],
                          {"build":"completed","new-report":"pending"})
        with self.assertRaises(TaskRuntimeError):
            runtime.replace_remaining(invalid.to_dict())
        self.assertEqual(runtime.to_dict(),before)

class AgentTaskIntegrationTests(unittest.TestCase):
    def _verifier(self, callback):
        registry=VerificationRegistry()
        registry.register("check",lambda params:callback(params))
        return CriteriaVerifier(registry,{"build output exists":{"verifier":"check"},
                                          "report exists":{"verifier":"check"}})

    def test_agent_executes_and_verifies_dag_before_goal_completion(self):
        calls=[]
        verifier=self._verifier(lambda params:{"passed":True,"evidence":{"source":"fixture"}})
        class Desktop:
            def observe(self): return {"summary":"fixture ready"}
        class Planner:
            usage={}
            def prepare_plan(self,state,observation):
                return TaskGraph([Task("build","Build",(),("build output exists",)),
                                  Task("report","Report",("build",),("report exists",))]).to_dict()
            def decide(self,state,observation):
                calls.append((state.get("active_task") or {}).get("id"))
                if state.get("active_task"): return {"type":"task_complete","evidence":{}}
                return {"type":"complete","evidence":{}}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"goal.json"
            create(path,"Build and report",["build output exists","report exists"])
            result=run(path,Desktop(),Planner(),verifier=verifier)
            self.assertEqual(result["status"],"completed")
            self.assertEqual(calls,["build","report",None])
            self.assertEqual(result["task_runtime"]["graph"]["status"],{"build":"completed","report":"completed"})
            self.assertEqual([x["event"] for x in result["history"] if x["event"]=="task_completed"],
                             ["task_completed","task_completed"])

    def test_failed_task_triggers_bounded_replan_of_unfinished_dag(self):
        checks={"count":0}
        verifier=self._verifier(lambda params:{"passed":True,"evidence":{"source":"fixture"}})
        class Desktop:
            def observe(self): return {"summary":"fixture ready"}
        class Planner:
            usage={}
            def __init__(self): self.replans=0
            def prepare_plan(self,state,observation):
                return TaskGraph([Task("build","Build",(),("build output exists",)),
                                  Task("report","Report",("build",),("report exists",))]).to_dict()
            def replan_tasks(self,state,observation):
                self.replans+=1
                return TaskGraph([Task("rebuild","Revised build",(),("build output exists",)),
                                  Task("new-report","Revised report",("rebuild",),("report exists",))]).to_dict()
            def decide(self,state,observation):
                if state.get("active_task"): return {"type":"task_complete","evidence":{}}
                return {"type":"complete","evidence":{}}
        planner=Planner()
        # First task acceptance fails; revised DAG then verifies both tasks.
        def check(params):
            checks["count"]+=1
            return {"passed":checks["count"]>1,"evidence":{"attempt":checks["count"]}}
        verifier=self._verifier(check)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"goal.json"
            create(path,"Build and report",["build output exists","report exists"])
            result=run(path,Desktop(),planner,verifier=verifier)
            self.assertEqual(result["status"],"completed")
            self.assertEqual(planner.replans,1)
            self.assertEqual(result["task_plan_replans"],1)
            self.assertEqual(result["task_runtime"]["graph"]["status"],
                             {"rebuild":"completed","new-report":"completed"})

    def test_failed_task_check_retries_without_unlocking_dependent(self):
        checks={"count":0}
        def check(params):
            checks["count"]+=1
            return {"passed":checks["count"]>1,"evidence":{"attempt":checks["count"]}}
        verifier=self._verifier(check)
        class Desktop:
            def observe(self): return {"summary":"fixture ready"}
        class Planner:
            usage={}
            def prepare_plan(self,state,observation):
                return TaskGraph([Task("build","Build",(),("build output exists",)),
                                  Task("report","Report",("build",),("report exists",))]).to_dict()
            def decide(self,state,observation):
                if state.get("active_task"): return {"type":"task_complete"}
                return {"type":"complete","evidence":{}}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"goal.json"
            create(path,"Build and report",["build output exists","report exists"])
            result=run(path,Desktop(),Planner(),verifier=verifier)
            self.assertEqual(result["status"],"completed")
            self.assertEqual(result["task_runtime"]["attempts"]["build"],2)
            self.assertEqual(result["task_runtime"]["graph"]["status"]["report"],"completed")

    def test_model_cannot_complete_goal_while_task_is_active(self):
        verifier=self._verifier(lambda params:{"passed":True})
        class Desktop:
            def observe(self): return {"summary":"fixture ready"}
        class Planner:
            usage={}
            def prepare_plan(self,state,observation):
                return TaskGraph([Task("build","Build",(),("build output exists","report exists"))]).to_dict()
            def decide(self,state,observation): return {"type":"complete","evidence":{}}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"goal.json"
            create(path,"Build and report",["build output exists","report exists"])
            result=run(path,Desktop(),Planner(),verifier=verifier)
            self.assertEqual(result["status"],"blocked")
            self.assertEqual(result["task_runtime"]["graph"]["status"]["build"],"running")
