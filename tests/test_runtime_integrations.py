import tempfile, unittest
from pathlib import Path
from praxis_agent.model_router import ModelProfile, ModelRouter
from praxis_agent.planner import ModelPlanner
from praxis_agent.scheduler import Scheduler
from praxis_agent.worker import Worker
class RuntimeIntegrationTests(unittest.TestCase):
    def test_router_selects_vision_and_high_risk_profiles(self):
        profiles=[ModelProfile("cheap","small","https://example.test/v1/chat/completions","CHEAP_KEY",frozenset({"chat"}),1),ModelProfile("vision","large","https://example.test/v1/chat/completions","VISION_KEY",frozenset({"chat","vision"}),3)]
        router=ModelRouter(profiles,"cheap")
        self.assertEqual(router.select().name,"cheap")
        self.assertEqual(router.select(requires_vision=True).name,"vision")
        self.assertEqual(router.select(risk="high").name,"vision")
    def test_planner_generates_validated_dag(self):
        planner=ModelPlanner("base","key","https://example.test/v1/chat/completions")
        planner._request=lambda messages,**kwargs: {"tasks":[{"id":"open","goal":"Open app","dependencies":[],"acceptance":["target inspected"]},{"id":"inspect","goal":"Inspect target","dependencies":["open"],"acceptance":["target inspected"]}]}
        plan=planner.prepare_plan({"goal":"Inspect app","criteria":["target inspected"],"model_usage":{}},{"summary":"desktop"})
        self.assertEqual([t["id"] for t in plan["tasks"]],["open","inspect"])
        self.assertEqual(planner.usage["plan_calls"],1)
    def test_invalid_plan_rejected(self):
        planner=ModelPlanner("base","key","https://example.test/v1/chat/completions")
        planner._request=lambda *a,**k: {"tasks":[{"id":"loop","goal":"bad","dependencies":["loop"]}]}
        with self.assertRaises(ValueError): planner.prepare_plan({"goal":"Do it","criteria":["done"],"model_usage":{}},{})
    def test_worker_claims_and_executes_job(self):
        with tempfile.TemporaryDirectory() as d:
            scheduler=Scheduler(Path(d)/"jobs.sqlite")
            job=scheduler.enqueue("Goal",{"criteria":["done"]})
            result=Worker(scheduler,lambda item:{"status":"completed"}).run_once()
            self.assertEqual(result["status"],"completed")
            self.assertEqual(scheduler.get(job["id"])["status"],"completed")
    def test_worker_does_not_mark_paused_as_completed(self):
        with tempfile.TemporaryDirectory() as d:
            scheduler=Scheduler(Path(d)/"jobs.sqlite")
            job=scheduler.enqueue("Goal",{})
            result=Worker(scheduler,lambda item:{"status":"paused"}).run_once()
            self.assertEqual(result["status"],"failed")
            self.assertEqual(scheduler.get(job["id"])["status"],"failed")
