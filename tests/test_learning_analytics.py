"""Regression tests for provenance-safe learning analytics."""
import json
import tempfile
import unittest
from pathlib import Path

from praxis_agent.learning import cluster_experiences, mine_failure_patterns
from praxis_agent.state import create, persist


class LearningAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def checkpoint(self, name, *, category="transient_backend_failure",
                   code="connection_timeout", phase="observation", status="failed",
                   failure_analysis=None):
        path = self.root / (name + ".json")
        state = create(path, "Sync the project report", ["report is available"])
        state["status"] = status
        state["last_error"] = {"code": code, "phase": phase, "may_have_executed": False}
        state["last_result"] = {"ok": False, "code": code, "phase": phase}
        if failure_analysis:
            state["failure_analysis"] = failure_analysis
        persist(path, state)
        return path

    def experience(self, identity, goal, *, environment="win11", status="active",
                   source="independently-verified", steps=None):
        return {
            "id": identity, "goal": goal, "criteria": ["report exists"],
            "environment": environment, "source": source, "status": status,
            "key_steps": steps or ["open project", "export report"],
            "preconditions": ["project is loaded"], "failure_modes": [],
        }

    def test_mines_repeated_signatures_and_keeps_checkpoint_provenance(self):
        self.checkpoint("a")
        self.checkpoint("b")
        self.checkpoint("c", code="permission_denied", phase="policy")
        report = mine_failure_patterns(self.root)
        self.assertEqual(report["analyzed_failures"], 3)
        repeated = next(pattern for pattern in report["patterns"] if pattern["occurrences"] == 2)
        self.assertEqual(repeated["category"], "transient_backend_failure")
        self.assertEqual(repeated["recurrence"], "recurring")
        self.assertEqual(len(repeated["checkpoint_ids"]), 2)
        self.assertIn("not_root_cause_confidence", repeated["evidence_strength"])

    def test_min_occurrence_filter_and_recursive_scan(self):
        self.checkpoint("top")
        nested = self.root / "nested"
        nested.mkdir()
        path = nested / "child.json"
        state = create(path, "Nested task", ["done"])
        state.update({"status": "failed", "last_error": {"code": "timeout", "phase": "run"},
                      "last_result": {"ok": False}})
        persist(path, state)
        self.assertEqual(mine_failure_patterns(self.root, min_occurrences=2)["patterns"], [])
        report = mine_failure_patterns(self.root, recursive=True)
        self.assertEqual(report["analyzed_failures"], 2)

    def test_corrupt_and_oversized_checkpoints_are_skipped(self):
        (self.root / "bad.json").write_text("{", encoding="utf-8")
        (self.root / "large.json").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        self.checkpoint("valid")
        report = mine_failure_patterns(self.root)
        self.assertEqual(report["analyzed_failures"], 1)
        self.assertEqual(report["skipped_checkpoints"], 2)

    def test_successful_checkpoint_is_not_mined_as_failure(self):
        path = self.root / "success.json"
        state = create(path, "Done", ["done"])
        state.update({"status": "completed", "last_result": {"ok": True}})
        persist(path, state)
        self.assertEqual(mine_failure_patterns(self.root)["patterns"], [])

    def test_stored_analysis_must_have_a_known_schema(self):
        path = self.root / "bad-analysis.json"
        state = create(path, "Done", ["done"])
        state.update({"status": "failed", "failure_analysis": {"category": "model_says_root_cause"}})
        persist(path, state)
        self.assertEqual(mine_failure_patterns(self.root)["patterns"], [])

    def test_clusters_verified_active_experiences_within_environment(self):
        records = [
            self.experience("a", "Export project report"),
            self.experience("b", "Export project report"),
            self.experience("c", "Export project report", environment="linux"),
            self.experience("d", "Delete user account", steps=["delete account"]),
            self.experience("e", "Export project report", status="invalidated"),
            self.experience("f", "Export project report", source="unverified"),
        ]
        report = cluster_experiences(records, min_similarity=0.3)
        self.assertEqual(report["eligible_experiences"], 4)
        self.assertEqual(report["cluster_count"], 1)
        cluster = report["clusters"][0]
        self.assertEqual(cluster["member_experience_ids"], ["a", "b"])
        self.assertGreaterEqual(cluster["minimum_pair_similarity"], 0.3)
        self.assertIsNone(cluster["embedding_model"])

    def test_complete_link_prevents_transitive_similarity_chains(self):
        records = [
            self.experience("a", "alpha beta gamma", steps=["alpha beta"]),
            self.experience("b", "alpha beta delta", steps=["alpha beta"]),
            self.experience("c", "alpha delta epsilon", steps=["alpha delta"]),
        ]
        report = cluster_experiences(records, min_similarity=0.4, include_singletons=True)
        clusters = report["clusters"]
        self.assertEqual(sum(c["size"] for c in clusters), 3)
        self.assertTrue(all(c["minimum_pair_similarity"] >= 0.4 for c in clusters))

    def test_clustering_is_deterministic_and_duplicate_ids_are_ignored(self):
        a = self.experience("a", "Export project report")
        b = self.experience("b", "Export project report")
        first = cluster_experiences([a, b], min_similarity=0.3)
        second = cluster_experiences([b, a, dict(a)], min_similarity=0.3)
        self.assertEqual(first["clusters"], second["clusters"])

    def test_invalid_similarity_and_symlink_checkpoint_root_are_rejected(self):
        with self.assertRaises(ValueError):
            cluster_experiences([], min_similarity=0)
        link = self.root / "linked"
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises(ValueError):
            mine_failure_patterns(link)


if __name__ == "__main__":
    unittest.main()
