"""At-least-once transactional outbox with per-consumer leases and fencing."""
import json
import time
import uuid
from pathlib import Path


class OutboxError(RuntimeError):
    pass


class OutboxDispatcher:
    def __init__(self, database, *, consumer, handler, lease_seconds=30, max_attempts=12):
        if not isinstance(consumer, str) or not consumer.strip():
            raise ValueError("consumer name required")
        if not callable(handler):
            raise ValueError("event handler required")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts must be 1..100")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float)) or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        self.database, self.consumer, self.handler = str(database), consumer, handler
        self.lease_seconds, self.max_attempts = float(lease_seconds), max_attempts
        self.owner = str(uuid.uuid4())

    def _connect(self):
        import sqlite3
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _claim(self):
        now, token = time.time(), str(uuid.uuid4())
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT d.event_id,e.event_type,e.payload,d.attempts
                FROM outbox_deliveries d JOIN outbox_events e ON e.id=d.event_id
                WHERE d.consumer=? AND ((d.status='pending' AND d.available_at<=?)
                OR (d.status='delivering' AND d.lease_expires_at<=?))
                ORDER BY e.created_at,d.event_id LIMIT 1""", (self.consumer, now, now)).fetchone()
            if row is None:
                db.commit()
                return None
            if row["attempts"] >= self.max_attempts:
                db.execute("""UPDATE outbox_deliveries SET status='dead',lease_owner=NULL,
                    lease_token=NULL,lease_expires_at=NULL,last_error=COALESCE(last_error,'attempt limit reached')
                    WHERE event_id=? AND consumer=?""", (row["event_id"], self.consumer))
                db.commit()
                return {"dead": True}
            changed = db.execute("""UPDATE outbox_deliveries SET status='delivering',
                attempts=attempts+1,lease_owner=?,lease_token=?,lease_expires_at=?
                WHERE event_id=? AND consumer=? AND ((status='pending' AND available_at<=?)
                OR (status='delivering' AND lease_expires_at<=?))""",
                (self.owner, token, now + self.lease_seconds, row["event_id"], self.consumer, now, now)).rowcount
            if changed != 1:
                db.rollback()
                return None
            event_row = db.execute("SELECT * FROM outbox_events WHERE id=?", (row["event_id"],)).fetchone()
            db.commit()
            return {"id": event_row["id"], "type": event_row["event_type"],
                    "payload": json.loads(event_row["payload"]), "attempt": row["attempts"] + 1,
                    "lease_token": token}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _settle(self, event, *, error=None):
        now = time.time()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT attempts FROM outbox_deliveries
                WHERE event_id=? AND consumer=? AND status='delivering'
                AND lease_owner=? AND lease_token=? AND lease_expires_at>?""",
                (event["id"], self.consumer, self.owner, event["lease_token"], now)).fetchone()
            if row is None:
                raise OutboxError("Delivery lease lost; stale consumer cannot settle event")
            if error is None:
                db.execute("""UPDATE outbox_deliveries SET status='delivered',delivered_at=?,
                    lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,last_error=NULL
                    WHERE event_id=? AND consumer=? AND lease_token=?""",
                    (now, event["id"], self.consumer, event["lease_token"]))
                status = "delivered"
            else:
                dead = row["attempts"] >= self.max_attempts
                delay = min(300, 2 ** min(row["attempts"], 8))
                db.execute("""UPDATE outbox_deliveries SET status=?,available_at=?,
                    lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,last_error=?
                    WHERE event_id=? AND consumer=? AND lease_token=?""",
                    ("dead" if dead else "pending", now + delay, str(error)[:500],
                     event["id"], self.consumer, event["lease_token"]))
                status = "dead" if dead else "retry"
            db.commit()
            return status
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def dispatch(self, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be 1..1000")
        results = []
        while len(results) < limit:
            event = self._claim()
            if event is None:
                break
            if event.get("dead"):
                results.append({"status": "dead"})
                continue
            try:
                public = {key: value for key, value in event.items() if key != "lease_token"}
                self.handler(public)
                status = self._settle(event)
            except Exception as exc:
                status = self._settle(event, error=type(exc).__name__ + ": " + str(exc))
            results.append({"event_id": event["id"], "status": status, "attempt": event["attempt"]})
        return {"consumer": self.consumer, "processed": len(results),
                "delivered": sum(x["status"] == "delivered" for x in results),
                "retried": sum(x["status"] == "retry" for x in results),
                "dead": sum(x["status"] == "dead" for x in results), "results": results}


class ExperienceEventConsumer:
    def __init__(self, store, verifier, *, environment, note):
        if not isinstance(environment, str) or not environment.strip():
            raise ValueError("Experience consumer requires an environment")
        if not isinstance(note, str) or not note.strip():
            raise ValueError("Experience consumer requires an operator-curated note")
        self.store, self.verifier = store, verifier
        self.environment, self.note = environment, note

    def __call__(self, event):
        if event.get("type") != "experience.verified_checkpoint":
            raise OutboxError("Unsupported experience event")
        payload = event["payload"]
        self.store.add_verified(Path(payload["checkpoint_path"]), verifier=self.verifier,
                                environment=self.environment, note=self.note, event_id=event["id"])


class EvolutionEventConsumer:
    def __init__(self, store):
        self.store = store

    def __call__(self, event):
        return self.store.record_verified_source_event(event)
