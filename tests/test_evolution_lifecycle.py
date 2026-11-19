import json
import tempfile
import unittest
from pathlib import Path
from praxis_agent.evolution import EvolutionStore, EvolutionError
from praxis_agent.experience import ExperienceStore
from praxis_agent.state import create, persist
from praxis_agent.verification import FileVerifier

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
            environment="linux-ci",note="Verified report generation")
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
