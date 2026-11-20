import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from threading import Event

from praxis_agent.scheduler import Scheduler, LeaseLost
from praxis_agent.worker import Worker

class WorkerLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db=Path(self.temp.name)/"jobs.sqlite"
        self.scheduler=Scheduler(self.db)

    def test_claim_heartbeat_and_fenced_finish(self):
        job=self.scheduler.enqueue("long job",run_at=0)
        now=time.time()
        first=self.scheduler.claim(now=now,lease_seconds=10,owner="worker-a")
        self.assertEqual(first["attempts"],1)
        beat=self.scheduler.heartbeat(job["id"],lease_token=first["lease_token"],
                                      owner="worker-a",now=now+5,lease_seconds=10)
        self.assertEqual(beat["lease_expires_at"],now+15)
        self.assertIsNone(self.scheduler.claim(now=now+11,lease_seconds=10,owner="worker-b"))
        second=self.scheduler.claim(now=now+16,lease_seconds=10,owner="worker-b")
        self.assertEqual(second["attempts"],2)
        with self.assertRaises(LeaseLost):
            self.scheduler.finish(job["id"],{"status":"completed"},
                                  lease_token=first["lease_token"],owner="worker-a")
        self.scheduler.finish(job["id"],{"status":"completed"},
                              lease_token=second["lease_token"],owner="worker-b")
        self.assertEqual(self.scheduler.get(job["id"])["status"],"completed")

    def test_expired_lease_is_requeued_until_attempt_limit(self):
        job=self.scheduler.enqueue("crash recovery",run_at=0,max_attempts=2)
        first=self.scheduler.claim(now=100,lease_seconds=10)
        second=self.scheduler.claim(now=111,lease_seconds=10)
        self.assertEqual(second["id"],job["id"])
        self.assertEqual(second["attempts"],2)
        self.assertIsNone(self.scheduler.claim(now=122,lease_seconds=10))
        current=self.scheduler.get(job["id"])
        self.assertEqual(current["status"],"failed")
        self.assertIn("attempt limit",current["error"])
        self.assertNotIn("lease_token",current)

    def test_failed_job_can_be_explicitly_retried_with_fresh_attempt_budget(self):
        job=self.scheduler.enqueue("retry me",max_attempts=1)
        claim=self.scheduler.claim()
        self.scheduler.fail(job["id"],"fixture failure",lease_token=claim["lease_token"])
        queued=self.scheduler.retry(job["id"])
        self.assertEqual((queued["status"],queued["attempts"]),("queued",0))
        next_claim=self.scheduler.claim()
        self.assertEqual(next_claim["attempts"],1)

    def test_worker_renews_lease_during_long_running_callback(self):
        job=self.scheduler.enqueue("slow job")
        entered=Event()
        def execute(item):
            entered.set()
            time.sleep(1.35)
            return {"status":"completed","result":"ok"}
        worker=Worker(self.scheduler,execute,lease_seconds=1,heartbeat_interval=0.2)
        result=worker.run_once()
        self.assertTrue(entered.is_set())
        self.assertEqual(result["status"],"completed")
        stored=self.scheduler.get(job["id"])
        self.assertEqual(stored["status"],"completed")
        self.assertGreater(stored["heartbeat_at"],stored["started_at"])

    def test_stale_worker_cannot_overwrite_reclaimed_job(self):
        job=self.scheduler.enqueue("fenced",run_at=0)
        first=self.scheduler.claim(lease_seconds=10,owner="old-worker")
        other=Scheduler(self.db)
        second=other.claim(now=first["lease_expires_at"]+1,lease_seconds=10,owner="new-worker")
        with self.assertRaises(LeaseLost):
            self.scheduler.fail(job["id"],"late failure",lease_token=first["lease_token"],owner="old-worker")
        other.finish(job["id"],{"status":"completed"},lease_token=second["lease_token"],owner="new-worker")
        self.assertEqual(self.scheduler.get(job["id"])["status"],"completed")

    def test_legacy_database_is_migrated_without_losing_jobs(self):
        legacy_db=Path(self.temp.name)/"legacy.sqlite"
        db=sqlite3.connect(legacy_db)
        db.execute("""CREATE TABLE jobs (
            id TEXT PRIMARY KEY, goal TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
            run_at REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, result TEXT, error TEXT)""")
        db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
                   ("legacy","Old goal","{}","queued",0,1,1,0,None,None))
        db.commit(); db.close()
        migrated=Scheduler(legacy_db)
        claim=migrated.claim(now=2)
        self.assertEqual(claim["id"],"legacy")
        self.assertEqual(claim["max_attempts"],3)
        self.assertTrue(claim["lease_token"])

    def test_worker_does_not_complete_paused_result(self):
        job=self.scheduler.enqueue("paused")
        result=Worker(self.scheduler,lambda item:{"status":"paused"}).run_once()
        self.assertEqual(result["status"],"failed")
        self.assertEqual(self.scheduler.get(job["id"])["status"],"failed")


    def test_lost_heartbeat_cancels_guarded_executor(self):
        job=self.scheduler.enqueue("cancel on lease loss")
        entered=Event()
        def execute(item, *, lease_guard):
            entered.set()
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                lease_guard.raise_if_lost()
                time.sleep(0.01)
            return {"status":"completed"}
        with patch.object(self.scheduler,"heartbeat",side_effect=LeaseLost("lease expired")):
            result=Worker(self.scheduler,execute,lease_seconds=1,heartbeat_interval=0.05).run_once()
        self.assertTrue(entered.is_set())
        self.assertEqual(result["status"],"lease_lost")
        self.assertNotEqual(self.scheduler.get(job["id"])["status"],"completed")

    def test_invalid_lease_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            Worker(self.scheduler,lambda item:{},lease_seconds=0.5)
        with self.assertRaises(ValueError):
            self.scheduler.enqueue("bad attempts",max_attempts=0)

