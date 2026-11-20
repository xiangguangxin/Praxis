"""SQLite job queue with renewable leases, expiry recovery and fenced completion."""
import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager

class LeaseLost(RuntimeError):
    """Raised when a worker no longer owns the current job lease."""

class Scheduler:
    def __init__(self, database):
        self.database = str(database)
        self.worker_id = str(uuid.uuid4())
        with self._session() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, goal TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL, run_at REAL NOT NULL, created_at REAL NOT NULL,
                updated_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                result TEXT, error TEXT, lease_owner TEXT, lease_token TEXT,
                lease_expires_at REAL, heartbeat_at REAL, started_at REAL,
                max_attempts INTEGER NOT NULL DEFAULT 3, checkpoint_path TEXT, checkpoint_id TEXT)""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            migrations = {
                "lease_owner": "TEXT", "lease_token": "TEXT", "lease_expires_at": "REAL",
                "heartbeat_at": "REAL", "started_at": "REAL",
                "max_attempts": "INTEGER NOT NULL DEFAULT 3",
                "checkpoint_path": "TEXT", "checkpoint_id": "TEXT",
            }
            for name, declaration in migrations.items():
                if name not in columns:
                    db.execute("ALTER TABLE jobs ADD COLUMN " + name + " " + declaration)
            db.execute("CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status,run_at)")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_lease_expiry ON jobs(status,lease_expires_at)")

    def _connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _session(self):
        db = self._connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _lease_duration(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 1 <= value <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        return float(value)

    @staticmethod
    def _timestamp(value, default):
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("timestamp must be finite")
        return float(value)

    def enqueue(self, goal, payload=None, *, run_at=None, max_attempts=3):
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("goal required")
        if payload is not None and not isinstance(payload, dict):
            raise ValueError("payload must be object")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 20:
            raise ValueError("max_attempts must be 1..20")
        now = time.time()
        scheduled = self._timestamp(run_at, now)
        encoded = json.dumps(payload or {}, ensure_ascii=False, allow_nan=False)
        jid = str(uuid.uuid4())
        with self._session() as db:
            db.execute("""INSERT INTO jobs
                (id,goal,payload,status,run_at,created_at,updated_at,max_attempts)
                VALUES(?,?,?,?,?,?,?,?)""",
                (jid, goal, encoded, "queued", scheduled, now, now, max_attempts))
        return self.get(jid)

    @staticmethod
    def _decode(row, *, include_lease_token=False):
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        if item["result"] is not None:
            item["result"] = json.loads(item["result"])
        if not include_lease_token:
            item.pop("lease_token", None)
        return item

    def get(self, jid):
        with self._session() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if row is None:
            raise KeyError(jid)
        return self._decode(row)

    def _recover_expired(self, db, now):
        db.execute("""UPDATE jobs SET status='failed',error='worker lease expired; attempt limit reached',
            lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,heartbeat_at=NULL,updated_at=?
            WHERE status='running' AND lease_expires_at<=? AND attempts>=max_attempts""", (now, now))
        db.execute("""UPDATE jobs SET status='queued',error='worker lease expired; checkpoint recovery required',
            lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,heartbeat_at=NULL,updated_at=?
            WHERE status='running' AND lease_expires_at<=? AND attempts<max_attempts""", (now, now))

    def claim(self, *, now=None, lease_seconds=60, owner=None):
        now = self._timestamp(now, time.time())
        duration = self._lease_duration(lease_seconds)
        owner = self.worker_id if owner is None else owner
        if not isinstance(owner, str) or not owner.strip() or len(owner) > 128:
            raise ValueError("owner must be a nonempty string of at most 128 characters")
        token = str(uuid.uuid4())
        with self._session() as db:
            db.execute("BEGIN IMMEDIATE")
            self._recover_expired(db, now)
            row = db.execute("""SELECT * FROM jobs WHERE status='queued' AND run_at<=?
                AND attempts<max_attempts ORDER BY run_at,created_at LIMIT 1""", (now,)).fetchone()
            if row is None:
                return None
            changed = db.execute("""UPDATE jobs SET status='running',attempts=attempts+1,
                updated_at=?,started_at=?,heartbeat_at=?,lease_owner=?,lease_token=?,lease_expires_at=?
                WHERE id=? AND status='queued'""",
                (now, now, now, owner, token, now + duration, row["id"])).rowcount
            if changed != 1:
                return None
            claimed = db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            return self._decode(claimed, include_lease_token=True)


    def bind_checkpoint(self, jid, path, checkpoint_id, *, lease_token, owner=None):
        """Persist the immutable job-to-checkpoint identity under the active lease."""
        if not isinstance(path, str) or not path.strip() or len(path) > 4096:
            raise ValueError("checkpoint path must be a nonempty string")
        if not isinstance(checkpoint_id, str) or not checkpoint_id.strip() or len(checkpoint_id) > 128:
            raise ValueError("checkpoint id must be a nonempty string")
        now = time.time()
        owner = self.worker_id if owner is None else owner
        if not isinstance(lease_token, str) or not lease_token:
            raise LeaseLost("A current lease token is required")
        with self._session() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT status,lease_owner,lease_token,lease_expires_at,
                checkpoint_path,checkpoint_id FROM jobs WHERE id=?""", (jid,)).fetchone()
            if row is None:
                raise KeyError(jid)
            if (row["status"] != "running" or row["lease_owner"] != owner
                    or row["lease_token"] != lease_token or row["lease_expires_at"] is None
                    or row["lease_expires_at"] <= now):
                raise LeaseLost("Cannot bind a checkpoint without the active job lease")
            if row["checkpoint_path"] is not None or row["checkpoint_id"] is not None:
                if row["checkpoint_path"] != path or row["checkpoint_id"] != checkpoint_id:
                    raise ValueError("Scheduled job is already bound to a different checkpoint")
                return {"job_id": jid, "checkpoint_path": path, "checkpoint_id": checkpoint_id,
                        "already_bound": True}
            db.execute("""UPDATE jobs SET checkpoint_path=?,checkpoint_id=?,updated_at=?
                WHERE id=? AND status='running' AND lease_owner=? AND lease_token=?""",
                (path, checkpoint_id, now, jid, owner, lease_token))
        return {"job_id": jid, "checkpoint_path": path, "checkpoint_id": checkpoint_id,
                "already_bound": False}

    def heartbeat(self, jid, *, lease_token, owner=None, now=None, lease_seconds=60):
        now = self._timestamp(now, time.time())
        duration = self._lease_duration(lease_seconds)
        owner = self.worker_id if owner is None else owner
        if not isinstance(lease_token, str) or not lease_token:
            raise LeaseLost("A current lease token is required")
        with self._session() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,lease_owner,lease_token,lease_expires_at FROM jobs WHERE id=?", (jid,)).fetchone()
            if row is None:
                raise KeyError(jid)
            if (row["status"] != "running" or row["lease_owner"] != owner
                    or row["lease_token"] != lease_token or row["lease_expires_at"] is None
                    or row["lease_expires_at"] <= now):
                raise LeaseLost("Worker lease expired or was superseded")
            db.execute("""UPDATE jobs SET heartbeat_at=?,lease_expires_at=?,updated_at=?
                WHERE id=? AND status='running' AND lease_owner=? AND lease_token=?""",
                (now, now + duration, now, jid, owner, lease_token))
        return {"job_id": jid, "heartbeat_at": now, "lease_expires_at": now + duration}

    def _resolve_token(self, jid, lease_token, owner):
        owner = self.worker_id if owner is None else owner
        if lease_token is not None:
            return lease_token, owner
        # Backward-compatible convenience for callers using the same Scheduler
        # instance that claimed the job; a different owner cannot infer a token.
        with self._session() as db:
            row = db.execute("""SELECT status,lease_owner,lease_token,lease_expires_at
                FROM jobs WHERE id=?""", (jid,)).fetchone()
        if (row is None or row["status"] != "running" or row["lease_owner"] != owner
                or not row["lease_token"] or row["lease_expires_at"] is None
                or row["lease_expires_at"] <= time.time()):
            raise LeaseLost("No active lease owned by this scheduler")
        return row["lease_token"], owner

    def _transition(self, jid, status, *, lease_token, owner=None, **fields):
        now = time.time()
        owner = self.worker_id if owner is None else owner
        if not isinstance(lease_token, str) or not lease_token:
            raise LeaseLost("A current lease token is required")
        with self._session() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT status,lease_owner,lease_token,lease_expires_at
                FROM jobs WHERE id=?""", (jid,)).fetchone()
            if row is None:
                raise KeyError(jid)
            if (row["status"] != "running" or row["lease_owner"] != owner
                    or row["lease_token"] != lease_token or row["lease_expires_at"] is None
                    or row["lease_expires_at"] <= now):
                raise LeaseLost("Cannot finish a job without its active lease")
            updates = {"status": status, "updated_at": now, "lease_owner": None,
                       "lease_token": None, "lease_expires_at": None, **fields}
            sql = ", ".join(key + "=?" for key in updates)
            db.execute("UPDATE jobs SET " + sql + " WHERE id=? AND status='running' AND lease_token=?",
                       (*updates.values(), jid, lease_token))

    def finish(self, jid, result, *, lease_token=None, owner=None):
        if not isinstance(result, dict):
            raise ValueError("result must be object")
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        token, owner = self._resolve_token(jid, lease_token, owner)
        self._transition(jid, "completed", lease_token=token, owner=owner,
                         result=encoded, error=None)

    def fail(self, jid, error, *, lease_token=None, owner=None):
        if not isinstance(error, str) or not error.strip():
            raise ValueError("error required")
        token, owner = self._resolve_token(jid, lease_token, owner)
        self._transition(jid, "failed", lease_token=token, owner=owner, error=error[:500])

    def retry(self, jid, *, run_at=None):
        scheduled = self._timestamp(run_at, time.time())
        now = time.time()
        with self._session() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
            if row is None:
                raise KeyError(jid)
            if row["status"] != "failed":
                raise ValueError("Only failed jobs can be retried")
            db.execute("""UPDATE jobs SET status='queued',run_at=?,updated_at=?,attempts=0,
                error=NULL,result=NULL,started_at=NULL,heartbeat_at=NULL,lease_owner=NULL,
                lease_token=NULL,lease_expires_at=NULL WHERE id=? AND status='failed'""",
                (scheduled, now, jid))
        return self.get(jid)

    def list(self, *, status=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be 1..1000")
        with self._session() as db:
            rows = db.execute("SELECT * FROM jobs " + ("WHERE status=? " if status else "")
                              + "ORDER BY created_at DESC LIMIT ?",
                              ((status, limit) if status else (limit,))).fetchall()
        return [self._decode(row) for row in rows]
