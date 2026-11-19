import tempfile, unittest
from pathlib import Path
from praxis_agent.planning.dynamic import DynamicTaskPlanner, PlanError
from praxis_agent.context import pack_context, ContextBudgetError
from praxis_agent.model_router import ModelProfile, ModelRouter
from praxis_agent.observability import EventJournal
from praxis_agent.scheduler import Scheduler
class PlatformExtensionTests(unittest.TestCase):
    def test_dynamic_plan_validates_output(self):
        planner=DynamicTaskPlanner(lambda req:{"tasks":[{"id":"a","goal":"Build","dependencies":[]},{"id":"b","goal":"Test","dependencies":["a"]}]})
        self.assertEqual([t.id for t in planner.plan("Build and test",["tests pass"]).ready()],["a"])
        bad=DynamicTaskPlanner(lambda req:{"tasks":[{"id":"a","goal":"A","dependencies":["missing"]}]})
        with self.assertRaises(PlanError): bad.plan("Goal",["done"])
    def test_context_pack_preserves_acceptance(self):
        packed=pack_context({"goal":"Do work","criteria":["file exists"],"steps":2,"history":[{"event":"x"}]},{"summary":"ok","ui_tree":"x"*5000},max_chars=500)
        self.assertEqual(packed["task"]["criteria"],["file exists"])
        with self.assertRaises(ContextBudgetError): pack_context({"goal":"x"*2000,"criteria":["y"*2000]}, {},max_chars=500)
    def test_model_router_respects_capabilities(self):
        profiles=[ModelProfile("cheap","small","https://example.test","KEY",frozenset({"chat"}),1),ModelProfile("vision","large","https://example.test","KEY",frozenset({"chat","vision"}),3)]
        router=ModelRouter(profiles,"cheap")
        self.assertEqual(router.select(requires_vision=True).name,"vision")
        self.assertEqual(router.select(risk="high").name,"vision")
        with self.assertRaises(ValueError): router.select(preferred="cheap",requires_vision=True)
    def test_event_journal_allowlists(self):
        with tempfile.TemporaryDirectory() as d:
            journal=EventJournal(Path(d)/"events.jsonl")
            journal.emit("task_completed",run_id="r1",task_id="t1",counters={"tool_calls":2})
            self.assertEqual(len(journal.read()),1)
            with self.assertRaises(ValueError): journal.emit("prompt",run_id="r1")
    def test_scheduler_durable_claim(self):
        with tempfile.TemporaryDirectory() as d:
            scheduler=Scheduler(Path(d)/"jobs.db"); job=scheduler.enqueue("do work",{"kind":"test"},run_at=10)
            self.assertIsNone(scheduler.claim(now=9))
            self.assertEqual(scheduler.claim(now=11)["id"],job["id"])
            scheduler.finish(job["id"],{"ok":True})
            self.assertEqual(scheduler.get(job["id"])["status"],"completed")
            with self.assertRaises(ValueError): scheduler.retry(job["id"])
