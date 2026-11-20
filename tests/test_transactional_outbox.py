"""Transactional outbox, at-least-once delivery and idempotent store consumers."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from praxis_agent.evolution import EvolutionStore
from praxis_agent.experience import ExperienceStore
from praxis_agent.outbox import EvolutionEventConsumer, ExperienceEventConsumer, OutboxDispatcher
from praxis_agent.scheduler import Scheduler
from praxis_agent.state import create, persist
from praxis_agent.verification import FileVerifier


class TransactionalOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "jobs.sqlite"
        self.scheduler = Scheduler(self.database)
        self.checkpoint = self.root / "checkpoint.json"
        state = create(self.checkpoint, "Generate report", ["report"])
        state["status"] = "completed"
        state["verification"] = {"level": "independent_artifact", "verifier_id": "test-verifier"}
        state["artifact_verification"] = {"passed": True, "checks": {"report": {"status": "passed"}}}
        persist(self.checkpoint, state)
        (self.root / "report.txt").write_text("PASS", encoding="utf-8")
        self.verifier = FileVerifier({"report": {"path": "report.txt", "contains": "PASS"}}, self.root)
        self.state = state

    def _finish(self, verified=True):
        job = self.scheduler.enqueue("Generate report", {"criteria": ["report"]}, run_at=0)
        claim = self.scheduler.claim()
        result = dict(self.state, _checkpoint_path=str(self.checkpoint))
        if not verified:
            result["verification"] = {"level": "model_evidence"}
        self.scheduler.finish(job["id"], result, lease_token=claim["lease_token"])
        return job

    def test_verified_finish_publishes_two_consumer_deliveries(self):
        self._finish()
        db = sqlite3.connect(self.database)
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT consumer,status FROM outbox_deliveries ORDER BY consumer").fetchall(),
                             [("evolution", "pending"), ("experience", "pending")])
        finally:
            db.close()

    def test_unverified_completion_never_publishes_learning_events(self):
        self._finish(verified=False)
        db = sqlite3.connect(self.database)
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0], 0)
        finally:
            db.close()

    def test_experience_consumer_is_idempotent_after_side_effect_before_ack(self):
        self._finish()
        store = ExperienceStore(self.root / "experience.json")
        consumer = ExperienceEventConsumer(store, self.verifier, environment="linux-ci",
                                            note="Operator reviewed the verified workflow")
        db = sqlite3.connect(self.database)
        try:
            event_id, payload = db.execute("SELECT id,payload FROM outbox_events WHERE event_type='experience.verified_checkpoint'").fetchone()
        finally:
            db.close()
        event = {"id": event_id, "type": "experience.verified_checkpoint", "payload": json.loads(payload)}
        first = consumer(event)
        second = consumer(event)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(store._read()["experiences"]), 1)
        self.assertEqual(store._read()["experiences"][0]["source_event_id"], event_id)

    def test_evolution_consumer_records_source_but_does_not_promote(self):
        self._finish()
        store = EvolutionStore(self.root / "evolution.json")
        result = OutboxDispatcher(self.database, consumer="evolution",
            handler=EvolutionEventConsumer(store)).dispatch()
        self.assertEqual(result["delivered"], 1)
        self.assertEqual(len(store.list_verified_sources()), 1)
        self.assertEqual(store.list(), [])

    def test_failed_delivery_retries_and_then_acknowledges(self):
        self._finish()
        attempts = []
        def flaky(event):
            attempts.append(event["id"])
            if len(attempts) == 1:
                raise RuntimeError("temporary store lock")
        dispatcher = OutboxDispatcher(self.database, consumer="evolution", handler=flaky)
        self.assertEqual(dispatcher.dispatch(limit=1)["retried"], 1)
        db = sqlite3.connect(self.database)
        try:
            db.execute("UPDATE outbox_deliveries SET available_at=0 WHERE consumer='evolution'")
            db.commit()
        finally:
            db.close()
        self.assertEqual(dispatcher.dispatch(limit=1)["delivered"], 1)
        self.assertEqual(len(attempts), 2)

    def test_stale_delivery_owner_cannot_ack_reclaimed_event(self):
        self._finish()
        first = OutboxDispatcher(self.database, consumer="evolution", handler=lambda event: None, lease_seconds=1)
        claimed = first._claim()
        db = sqlite3.connect(self.database)
        try:
            db.execute("UPDATE outbox_deliveries SET lease_expires_at=0 WHERE consumer='evolution'")
            db.commit()
        finally:
            db.close()
        second = OutboxDispatcher(self.database, consumer="evolution", handler=lambda event: None)
        replacement = second._claim()
        with self.assertRaisesRegex(RuntimeError, "lease lost"):
            first._settle(claimed)
        second._settle(replacement)


if __name__ == "__main__":
    unittest.main()
