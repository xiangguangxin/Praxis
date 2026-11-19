import tempfile, unittest
from pathlib import Path
from praxis_agent.planning import Task, TaskGraph
from praxis_agent.planning.task_graph import TaskGraphError
from praxis_agent.planning.orchestrator import TaskOrchestrator, OrchestrationError

class Phase5DAGTests(unittest.TestCase):
    def test_acceptance_round_trip(self):
        graph=TaskGraph([Task("a","Build",(),("binary exists","build log clean"))])
        restored=TaskGraph.from_dict(graph.to_dict())
        self.assertEqual(restored.tasks["a"].acceptance,("binary exists","build log clean"))
    def test_dynamic_add_remove(self):
        graph=TaskGraph([Task("a","A"),Task("b","B",("a",))])
        graph.transition("a","running"); graph.transition("a","completed")
        graph.add_task(Task("c","C",("a",)))
        self.assertEqual({t.id for t in graph.ready()},{"b","c"})
        with self.assertRaises(TaskGraphError): graph.remove_task("a")
        graph.remove_task("c")
        self.assertNotIn("c",graph.tasks)
    def test_replace_pending_preserves_completed(self):
        graph=TaskGraph([Task("setup","Setup"),Task("old","Old",("setup",))])
        graph.transition("setup","running"); graph.transition("setup","completed")
        graph.replace_pending([Task("new","New",("setup",),("output exists",))])
        self.assertEqual(graph.status,{"setup":"completed","new":"pending"})
        self.assertEqual(graph.tasks["setup"].goal,"Setup")
        self.assertEqual(graph.ready()[0].id,"new")
    def test_replan_rejects_running_and_completed_overwrite(self):
        graph=TaskGraph([Task("a","A"),Task("b","B",("a",))])
        graph.transition("a","running")
        with self.assertRaises(TaskGraphError): graph.replace_pending([Task("x","X")])
        graph.transition("a","blocked"); graph.transition("a","pending")
        graph.transition("a","running"); graph.transition("a","completed")
        with self.assertRaises(TaskGraphError): graph.replace_pending([Task("a","Rewrite")])
    def test_remove_last_is_atomic(self):
        graph=TaskGraph([Task("only","Only")])
        with self.assertRaises(TaskGraphError): graph.remove_task("only")
        self.assertIn("only",graph.tasks)

class Phase5OrchestrationTests(unittest.TestCase):
    def test_mutations_are_checkpointed(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"plan.json"; graph=TaskGraph([Task("a","A"),Task("b","B",("a",))])
            runner=TaskOrchestrator(graph,lambda t:{"ok":True},lambda t,r:{"passed":True,"evidence":{"artifact":t.id}},path)
            runner.graph.transition("a","running"); runner.graph.transition("a","completed")
            runner.status="paused"; runner._save()
            runner.add_task(Task("c","C",("a",))); runner.remove_task("b")
            runner.replan_remaining([Task("d","D",("a",),("D artifact exists",))],reason="Scope changed")
            resumed=TaskOrchestrator.resume(path,lambda t:{"ok":True},lambda t,r:True)
            self.assertEqual(resumed.graph.status,{"a":"completed","d":"pending"})
            self.assertEqual(resumed.graph.tasks["d"].acceptance,("D artifact exists",))
            self.assertEqual(resumed.events[-1]["event"],"plan_replaced")
    def test_replan_preserves_verified_work_even_when_not_referenced(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"plan.json"; graph=TaskGraph([Task("done","Done"),Task("todo","Todo",("done",))])
            runner=TaskOrchestrator(graph,lambda t:{},lambda t,r:True,path)
            runner.graph.transition("done","running"); runner.graph.transition("done","completed")
            runner.status="paused"
            result=runner.replan_remaining([Task("replacement","New")],reason="Scope changed")
            self.assertEqual(result["tasks"],{"done":"completed","replacement":"pending"})
    def test_verification_evidence_persisted_per_node(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"plan.json"; graph=TaskGraph([Task("a","A",(),("file exists",))])
            runner=TaskOrchestrator(graph,lambda t:{"artifact":"a.json"},
                lambda t,r:{"passed":True,"evidence":{"artifact":"a.json","check":"exists"}},path)
            result=runner.run()
            self.assertEqual(result["status"],"completed")
            self.assertEqual(result["results"]["a"]["verification"]["check"],"exists")

    def test_dynamic_planner_replans_only_unfinished_work(self):
        from praxis_agent.planning.dynamic import DynamicTaskPlanner
        with tempfile.TemporaryDirectory() as d:
            graph=TaskGraph([Task("setup","Setup"),Task("old","Old",("setup",))])
            graph.transition("setup","running"); graph.transition("setup","completed")
            seen={}
            def propose(payload):
                seen.update(payload)
                return {"tasks":[{"id":"new","goal":"New step","dependencies":["setup"],
                                  "acceptance":["new artifact exists"]}]}
            planner=DynamicTaskPlanner(propose,max_tasks=5)
            updated=planner.replan_remaining("Finish workflow",["Final report exists"],graph,
                context={"last_error":"old route unavailable"})
            self.assertEqual(updated.status,{"setup":"completed","new":"pending"})
            self.assertEqual(seen["completed_tasks"][0]["id"],"setup")
            self.assertEqual(seen["remaining_tasks"][0]["id"],"old")
    def test_orchestrator_replan_with_persists_replacement(self):
        from praxis_agent.planning.dynamic import DynamicTaskPlanner
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"plan.json"
            graph=TaskGraph([Task("done","Done"),Task("obsolete","Obsolete",("done",))])
            runner=TaskOrchestrator(graph,lambda t:{"ok":True},lambda t,r:True,path)
            runner.graph.transition("done","running"); runner.graph.transition("done","completed")
            runner.status="paused"
            planner=DynamicTaskPlanner(lambda payload:{"tasks":[
                {"id":"replacement","goal":"Replacement","dependencies":["done"],
                 "acceptance":["replacement output verified"]}]})
            result=runner.replan_with(planner,"Overall goal",["Final output"],reason="Old path unavailable")
            self.assertEqual(result["tasks"],{"done":"completed","replacement":"pending"})
            resumed=TaskOrchestrator.resume(path,lambda t:{"ok":True},lambda t,r:True)
            self.assertEqual(resumed.graph.tasks["done"].goal,"Done")
            self.assertEqual(resumed.events[-1]["event"],"plan_replaced")
