import tempfile, unittest
from pathlib import Path
from praxis_agent.planning import Task, TaskGraph
from praxis_agent.planning.orchestrator import TaskOrchestrator, OrchestrationError

class TaskOrchestratorTests(unittest.TestCase):
    def test_verified_dag_execution_order(self):
        with tempfile.TemporaryDirectory() as d:
            calls=[]
            graph=TaskGraph([Task("build","Build"),Task("test","Test",("build",)),Task("report","Report",("test",))])
            runner=TaskOrchestrator(graph,lambda t:calls.append(t.id) or {"ok":True},
                lambda t,r:{"passed":True,"evidence":{"checked":t.id}},Path(d)/"plan.json")
            self.assertEqual(runner.run()["status"],"completed")
            self.assertEqual(calls,["build","test","report"])
    def test_failure_retries_then_blocks_dependents(self):
        with tempfile.TemporaryDirectory() as d:
            calls=[]; graph=TaskGraph([Task("a","A"),Task("b","B",("a",))])
            runner=TaskOrchestrator(graph,lambda t:calls.append(t.id) or {},lambda t,r:False,
                Path(d)/"p.json",max_attempts=2)
            result=runner.run()
            self.assertEqual(calls,["a","a"])
            self.assertEqual(result["tasks"],{"a":"failed","b":"blocked"})
    def test_interrupted_task_requires_reconciliation(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"p.json"; graph=TaskGraph([Task("a","A"),Task("b","B",("a",))])
            runner=TaskOrchestrator(graph,lambda t:{},lambda t,r:True,path)
            runner.graph.transition("a","running"); runner._save()
            resumed=TaskOrchestrator.resume(path,lambda t:{},lambda t,r:True)
            self.assertEqual(resumed.graph.status["a"],"blocked")
            with self.assertRaises(OrchestrationError):
                resumed.reconcile("a","completed",result={},verified=False)
            resumed.reconcile("a","retry")
            self.assertEqual(resumed.run()["status"],"completed")
    def test_budget_pause_and_resume(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"p.json"; graph=TaskGraph([Task("a","A"),Task("b","B")]); calls=[]
            runner=TaskOrchestrator(graph,lambda t:calls.append(t.id) or {},lambda t,r:True,path,max_tasks=1)
            self.assertEqual(runner.run()["status"],"paused")
            resumed=TaskOrchestrator.resume(path,lambda t:calls.append(t.id) or {},lambda t,r:True)
            self.assertEqual(resumed.run()["status"],"completed"); self.assertEqual(calls,["a","b"])
