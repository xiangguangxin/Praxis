"""Cross-store scheduler/checkpoint coordination and crash recovery tests."""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from praxis_agent.coordinator import CoordinationError, ExecutionCoordinator
from praxis_agent.scheduler import LeaseLost, Scheduler
from praxis_agent.state import create, load, persist


class ExecutionCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.scheduler = Scheduler(root / "jobs.sqlite")
        self.checkpoints = root / "checkpoints"
        self.coordinator = ExecutionCoordinator(self.scheduler, self.checkpoints)

    def _claim(self, goal="inspect", criteria=None):
        job = self.scheduler.enqueue(goal, {"criteria": criteria or ["verified"]}, run_at=0)
        return self.scheduler.claim(lease_seconds=30), job

    def test_first_claim_creates_and_persists_bidirectional_identity(self):
        claimed, queued = self._claim()
        path = self.coordinator.open_checkpoint(claimed)
        state = load(path)
        stored = self.scheduler.get(queued["id"])
        self.assertEqual(state["scheduled_job_id"], queued["id"])
        self.assertEqual(stored["checkpoint_path"], str(path))
        self.assertEqual(stored["checkpoint_id"], state["id"])

    def test_reclaimed_job_reuses_same_checkpoint_instead_of_recreating(self):
        claimed, queued = self._claim()
        path = self.coordinator.open_checkpoint(claimed)
        original = load(path)
        original["steps"] = 4
        persist(path, original)
        second = self.scheduler.claim(now=claimed["lease_expires_at"] + 1, lease_seconds=30)
        self.assertEqual(second["id"], queued["id"])
        resumed_path = self.coordinator.open_checkpoint(second)
        self.assertEqual(resumed_path, path)
        self.assertEqual(load(path)["id"], original["id"])
        self.assertEqual(load(path)["steps"], 4)

    def test_crash_after_checkpoint_publication_before_sqlite_binding_is_adopted(self):
        claimed, queued = self._claim()
        path = self.checkpoints / (queued["id"] + ".json")
        state = create(path, queued["goal"], queued["payload"]["criteria"])
        state["scheduled_job_id"] = queued["id"]
        persist(path, state)
        adopted = self.coordinator.open_checkpoint(claimed)
        self.assertEqual(adopted, path)
        self.assertEqual(self.scheduler.get(queued["id"])["checkpoint_id"], state["id"])

    def test_missing_bound_checkpoint_fails_closed(self):
        claimed, queued = self._claim()
        path = self.coordinator.open_checkpoint(claimed)
        stored = self.scheduler.get(queued["id"])
        claimed.update({"checkpoint_path": stored["checkpoint_path"],
                        "checkpoint_id": stored["checkpoint_id"]})
        path.unlink()
        with self.assertRaisesRegex(CoordinationError, "missing"):
            self.coordinator.open_checkpoint(claimed)
        self.assertEqual(self.scheduler.get(queued["id"])["status"], "running")

    def test_checkpoint_goal_or_job_identity_mismatch_is_rejected(self):
        claimed, queued = self._claim()
        path = self.coordinator.open_checkpoint(claimed)
        state = load(path)
        state["scheduled_job_id"] = "another-job"
        persist(path, state)
        with self.assertRaisesRegex(CoordinationError, "another scheduled job"):
            self.coordinator.open_checkpoint(claimed)

    def test_stale_lease_cannot_bind_checkpoint(self):
        claimed, queued = self._claim()
        second_scheduler = Scheduler(self.scheduler.database)
        replacement = second_scheduler.claim(now=claimed["lease_expires_at"] + 1, lease_seconds=30)
        with self.assertRaises(LeaseLost):
            self.scheduler.bind_checkpoint(queued["id"], "/tmp/job.json", "checkpoint",
                                           lease_token=claimed["lease_token"],
                                           owner=claimed["lease_owner"])
        self.assertEqual(replacement["id"], queued["id"])

    def test_audit_reports_checkpoint_completed_before_scheduler_commit(self):
        claimed, queued = self._claim()
        path = self.coordinator.open_checkpoint(claimed)
        state = load(path)
        state["status"] = "completed"
        persist(path, state)
        report = self.coordinator.audit()
        self.assertFalse(report["consistent"])
        self.assertEqual(report["findings"][0]["issue"],
                         "checkpoint_completed_before_scheduler_commit")
        self.assertTrue(report["findings"][0]["recoverable"])

    def test_legacy_scheduler_schema_migrates_checkpoint_mapping_columns(self):
        database = Path(self.temp.name) / "legacy.sqlite"
        db = sqlite3.connect(database)
        db.execute("""CREATE TABLE jobs (
            id TEXT PRIMARY KEY, goal TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
            run_at REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, result TEXT, error TEXT)""")
        db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
                   ("legacy", "Legacy goal", '{"criteria":["done"]}', "queued",
                    0, 1, 1, 0, None, None))
        db.commit()
        db.close()
        migrated = Scheduler(database)
        claim = migrated.claim(now=time.time(), lease_seconds=30)
        path = ExecutionCoordinator(migrated, Path(self.temp.name) / "legacy-checkpoints").open_checkpoint(claim)
        stored = migrated.get("legacy")
        self.assertEqual(stored["checkpoint_path"], str(path))
        self.assertEqual(stored["checkpoint_id"], load(path)["id"])


if __name__ == "__main__":
    unittest.main()
