import json
import tempfile
import unittest
from pathlib import Path
from praxis_agent.evolution import EvolutionStore, EvolutionError
from praxis_agent.experience import ExperienceStore
from praxis_agent.state import create, persist
from praxis_agent.verification import FileVerifier
from praxis_agent.planner import ModelPlanner
from unittest.mock import patch

class EvolutionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        checkpoint=self.root/"verified.json"
        state=create(checkpoint,"Generate a verified coverage report",["report"])
        state["status"]="completed"
        persist(checkpoint,state)
        (self.root/"report.txt").write_text("PASS")
        verifier=FileVerifier({"report":{"path":"report.txt","contains":"PASS"}},self.root)
        self.experiences=ExperienceStore(self.root/"experiences.json")
        self.experience=self.experiences.add_verified(checkpoint,verifier=verifier,
            environment="linux-ci",note="Verified report generation",
            preconditions=["fixture is available"],key_steps=["Open build summary","Generate report"],
            failure_modes=["Do not skip the build wait"])
        self.store=EvolutionStore(self.root/"evolution.json",self.experiences)
        self.strategy=self.store.propose(goal="Generate a verified coverage report",
            environment="linux-ci",instruction="Check the build summary before opening the report.",
            source_experience_id=self.experience["id"],rationale="Verified workflow consistently exposed build status first")

    def report(self,variant,successes,*,runtime="same-runtime"):
        records=[]
        for i in range(10):
            passed=i<successes
            records.append({"scenario":"coverage","iteration":i+1,"variant":variant,
                "passed":passed,"completed_and_verified":passed,"trajectory":{"passed":passed}})
        return {"kind":"live-browser","model":"model-a","provenance":{
            "suite_sha256":"suite-hash","runtime_source_sha256":runtime,
            "runtime_package_version":"0.1.0","python_version":"3.12.0",
            "os":"Linux","architecture":"x86_64","repeat":10,
            "model_connection":"litellm","model_budget":{"max_calls":30,"max_tokens":50000}},
            "records":records}

    def test_verified_experience_can_be_extracted_into_inert_candidate(self):
        candidate=self.store.extract_candidate(self.experience["id"])
        self.assertEqual(candidate["status"],"candidate")
        self.assertIn("Open build summary -> Generate report",candidate["instruction"])
        self.assertEqual(self.store.search("coverage report",environment="linux-ci"),[])

    def test_candidate_requires_paired_benchmark_before_promotion(self):
        self.assertEqual(self.store.search("coverage report",environment="linux-ci"),[])
        with self.assertRaises(EvolutionError):
            self.store.promote(self.strategy["id"])
        report={"provenance":self.report("baseline",5)["provenance"],
                "records":self.report("baseline",5)["records"]+self.report("strategy_candidate",8)["records"]}
        result=self.store.evaluate_variants(self.strategy["id"],report)
        self.assertTrue(result["passed"])
        promoted=self.store.promote(self.strategy["id"])
        self.assertEqual(promoted["status"],"promoted")
        hints=self.store.search("coverage report",environment="linux-ci")
        self.assertEqual([item["id"] for item in hints],[self.strategy["id"]])
        self.assertEqual(self.store.search("coverage report",environment="linux-ci",
            include_candidate_id="another-candidate"),[])

    def test_planner_only_receives_promoted_strategies_unless_candidate_preview_requested(self):
        report={"provenance":self.report("baseline",5)["provenance"],
                "records":self.report("baseline",5)["records"]+self.report("strategy_candidate",8)["records"]}
        self.store.evaluate_variants(self.strategy["id"],report)
        self.store.promote(self.strategy["id"])
        captured=[]
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self):
                return json.dumps({"choices":[{"message":{"content":'{"type":"pause","reason":"inspect"}'}}],
                                   "usage":{"prompt_tokens":10,"completion_tokens":5}}).encode()
        def fake(request,timeout):
            captured.append(json.loads(request.data))
            return Response()
        planner=ModelPlanner("model","key","https://invalid",
            evolution_store=self.store,evolution_environment="linux-ci")
        state={"goal":"Generate a verified coverage report","criteria":["report"],"steps":0,"max_steps":5}
        with patch("praxis_agent.planner.urllib.request.urlopen",side_effect=fake):
            planner.decide(state,{"summary":"ready"})
        self.assertIn("untrusted_strategy_hints",json.dumps(captured[-1]))
        self.assertEqual(planner.usage["strategy_hint_calls"],1)

    def test_candidate_preview_is_opt_in_and_does_not_promote(self):
        self.assertEqual(self.store.search("coverage report",environment="linux-ci",
            include_candidate_id=self.strategy["id"])[0]["status"],"candidate")
        self.assertEqual(self.store.search("coverage report",environment="linux-ci"),[])

    def test_no_improvement_fails_gate_and_cannot_promote(self):
        baseline=self.report("baseline",8)
        candidate=self.report("strategy_candidate",8)
        result=self.store.evaluate(self.strategy["id"],baseline,candidate)
        self.assertFalse(result["passed"])
        with self.assertRaises(EvolutionError):
            self.store.promote(self.strategy["id"])

    def test_regression_in_verified_completion_or_trajectory_blocks_promotion(self):
        baseline=self.report("baseline",7)
        candidate=self.report("strategy_candidate",9)
        candidate["records"][0]["completed_and_verified"]=False
        candidate["records"][0]["trajectory"]["passed"]=False
        result=self.store.evaluate(self.strategy["id"],baseline,candidate)
        self.assertFalse(result["passed"])
        self.assertTrue(result["safety_regressions"])

    def test_provenance_mismatch_is_rejected(self):
        baseline=self.report("baseline",5)
        candidate=self.report("strategy_candidate",9,runtime="changed-runtime")
        with self.assertRaises(EvolutionError):
            self.store.evaluate(self.strategy["id"],baseline,candidate)

    def test_insufficient_pairs_and_duplicate_rows_are_rejected(self):
        baseline=self.report("baseline",5)
        candidate=self.report("strategy_candidate",9)
        baseline["records"]=baseline["records"][:5]
        candidate["records"]=candidate["records"][:5]
        with self.assertRaises(EvolutionError):
            self.store.evaluate(self.strategy["id"],baseline,candidate)
        baseline=self.report("baseline",5)
        candidate=self.report("strategy_candidate",9)
        candidate["records"].append(candidate["records"][0])
        with self.assertRaises(EvolutionError):
            self.store.evaluate(self.strategy["id"],baseline,candidate)

    def test_failed_reassessment_suspends_promoted_strategy_automatically(self):
        report={"provenance":self.report("baseline",5)["provenance"],
                "records":self.report("baseline",5)["records"]+self.report("strategy_candidate",8)["records"]}
        self.store.evaluate_variants(self.strategy["id"],report)
        self.store.promote(self.strategy["id"])
        regression={"provenance":self.report("baseline",8)["provenance"],
                    "records":self.report("baseline",8)["records"]+self.report("strategy_candidate",7)["records"]}
        result=self.store.evaluate_variants(self.strategy["id"],regression)
        self.assertFalse(result["passed"])
        self.assertEqual(self.store.list(status="suspended")[0]["id"],self.strategy["id"])
        self.assertEqual(self.store.search("coverage report",environment="linux-ci"),[])

    def test_rollback_removes_strategy_from_retrieval(self):
        report={"provenance":self.report("baseline",5)["provenance"],
                "records":self.report("baseline",5)["records"]+self.report("strategy_candidate",8)["records"]}
        self.store.evaluate_variants(self.strategy["id"],report)
        self.store.promote(self.strategy["id"])
        self.store.rollback(self.strategy["id"],reason="Application version changed")
        self.assertEqual(self.store.search("coverage report",environment="linux-ci"),[])
        self.assertEqual(self.store.list(status="rolled_back")[0]["id"],self.strategy["id"])

    def test_only_active_verified_experience_can_seed_strategy(self):
        self.experiences.invalidate(self.experience["id"],reason="source stale")
        with self.assertRaises(EvolutionError):
            self.store.propose(goal="New workflow",environment="linux-ci",instruction="Try another route.",
                source_experience_id=self.experience["id"],rationale="test invalid source")

if __name__=="__main__":
    unittest.main()
