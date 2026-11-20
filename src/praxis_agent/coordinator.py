"""Coordinate durable scheduler rows and filesystem checkpoints.

SQLite and JSON files cannot share one atomic transaction. The coordinator uses
an immutable job/checkpoint identity and a recoverable publication order:
create or validate checkpoint -> persist job identity in checkpoint -> bind it
in SQLite under the current lease. Every intermediate state is safe to retry.
"""
from pathlib import Path

from .state import create, load, persist


class CoordinationError(RuntimeError):
    """Raised when scheduler and checkpoint state cannot be reconciled safely."""


class ExecutionCoordinator:
    def __init__(self, scheduler, checkpoint_dir):
        self.scheduler = scheduler
        self.checkpoint_dir = Path(checkpoint_dir).resolve()
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    def _expected_path(self, job):
        job_id = job["id"]
        if not isinstance(job_id, str) or not job_id or Path(job_id).name != job_id:
            raise CoordinationError("Scheduled job id is not a safe path component")
        return self.checkpoint_dir / (job_id + ".json")

    def open_checkpoint(self, job):
        """Open or create the single durable checkpoint assigned to a claimed job."""
        criteria = job.get("payload", {}).get("criteria")
        if not isinstance(criteria, list) or not criteria:
            raise CoordinationError("Scheduled job requires acceptance criteria")
        expected = self._expected_path(job)
        bound_path = job.get("checkpoint_path")
        if bound_path:
            path = Path(bound_path)
            if path != expected:
                raise CoordinationError("Stored checkpoint path is outside the job's deterministic location")
            if not path.is_file() or path.is_symlink():
                raise CoordinationError("Bound checkpoint is missing or unsafe; refusing to recreate execution history")
        else:
            path = expected

        if path.is_symlink():
            raise CoordinationError("Scheduled checkpoint path must not be a symbolic link")
        if path.exists():
            try:
                state = load(path)
            except (OSError, ValueError) as exc:
                raise CoordinationError("Existing scheduled checkpoint is unreadable") from exc
            if state.get("goal") != job["goal"] or state.get("criteria") != criteria:
                raise CoordinationError("Existing checkpoint does not match the scheduled goal and criteria")
            linked_job = state.get("scheduled_job_id")
            if linked_job not in (None, job["id"]):
                raise CoordinationError("Checkpoint is already associated with another scheduled job")
            if job.get("checkpoint_id") and state["id"] != job["checkpoint_id"]:
                raise CoordinationError("Checkpoint identity does not match the scheduler record")
        else:
            if bound_path:
                raise CoordinationError("Bound checkpoint disappeared during recovery")
            state = create(path, job["goal"], criteria)

        if state.get("scheduled_job_id") != job["id"]:
            state["scheduled_job_id"] = job["id"]
            persist(path, state)

        # If the process crashes before the SQLite bind, the next lease adopts
        # this checkpoint by its deterministic path and embedded job identity.
        self.scheduler.bind_checkpoint(
            job["id"], str(path), state["id"],
            lease_token=job["lease_token"], owner=job["lease_owner"])
        return path

    def audit(self, *, limit=100):
        """Read-only consistency report; never repairs ambiguous state automatically."""
        jobs = self.scheduler.list(limit=limit)
        findings = []
        for job in jobs:
            path_value = job.get("checkpoint_path")
            if not path_value:
                expected = self._expected_path(job)
                if expected.exists():
                    findings.append({"job_id": job["id"], "issue": "unbound_checkpoint",
                                     "checkpoint_path": str(expected), "recoverable": True})
                continue
            path = Path(path_value)
            if path != self._expected_path(job):
                findings.append({"job_id": job["id"], "issue": "checkpoint_path_mismatch",
                                 "recoverable": False})
                continue
            if not path.is_file() or path.is_symlink():
                findings.append({"job_id": job["id"], "issue": "checkpoint_missing_or_unsafe",
                                 "recoverable": False})
                continue
            try:
                state = load(path)
            except (OSError, ValueError):
                findings.append({"job_id": job["id"], "issue": "checkpoint_corrupt",
                                 "recoverable": False})
                continue
            if state["id"] != job.get("checkpoint_id"):
                issue = "checkpoint_id_mismatch"
            elif state.get("scheduled_job_id") != job["id"]:
                issue = "checkpoint_job_id_mismatch"
            elif state["goal"] != job["goal"] or state["criteria"] != job["payload"].get("criteria"):
                issue = "checkpoint_goal_mismatch"
            elif state["status"] == "completed" and job["status"] != "completed":
                issue = "checkpoint_completed_before_scheduler_commit"
            elif job["status"] == "completed" and state["status"] != "completed":
                issue = "scheduler_completed_before_checkpoint"
            else:
                continue
            findings.append({"job_id": job["id"], "issue": issue,
                             "checkpoint_path": str(path),
                             "recoverable": issue == "checkpoint_completed_before_scheduler_commit"})
        return {"checked": len(jobs), "consistent": not findings, "findings": findings}
