"""Bounded worker with renewable leases and stale-owner fencing."""
from threading import Event, Thread
from .scheduler import LeaseLost

class Worker:
    def __init__(self, scheduler, execute, *, lease_seconds=60, heartbeat_interval=None):
        if not callable(execute):
            raise ValueError("execute callback required")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float)) or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        interval = heartbeat_interval if heartbeat_interval is not None else lease_seconds / 3
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 0 < interval < lease_seconds:
            raise ValueError("heartbeat_interval must be positive and shorter than the lease")
        self.scheduler, self.execute = scheduler, execute
        self.lease_seconds, self.heartbeat_interval = float(lease_seconds), float(interval)

    def run_once(self):
        job = self.scheduler.claim(lease_seconds=self.lease_seconds)
        if job is None:
            return {"status": "idle"}
        token, owner = job["lease_token"], job["lease_owner"]
        stop, lost = Event(), Event()

        def renew():
            while not stop.wait(self.heartbeat_interval):
                try:
                    self.scheduler.heartbeat(job["id"], lease_token=token, owner=owner,
                                              lease_seconds=self.lease_seconds)
                except Exception:
                    lost.set()
                    return

        heartbeat = Thread(target=renew, name="praxis-worker-heartbeat", daemon=True)
        heartbeat.start()
        try:
            result = self.execute(job)
            if lost.is_set():
                return {"status": "lease_lost", "job_id": job["id"]}
            if not isinstance(result, dict):
                raise TypeError("Job executor must return an object")
            if result.get("status") == "completed":
                self.scheduler.finish(job["id"], result, lease_token=token, owner=owner)
                return {"status": "completed", "job_id": job["id"], "result": result}
            self.scheduler.fail(job["id"], "Agent goal ended with status " + str(result.get("status", "unknown")),
                                lease_token=token, owner=owner)
            return {"status": "failed", "job_id": job["id"], "result": result}
        except LeaseLost:
            return {"status": "lease_lost", "job_id": job["id"]}
        except Exception as exc:
            if lost.is_set():
                return {"status": "lease_lost", "job_id": job["id"]}
            try:
                self.scheduler.fail(job["id"], type(exc).__name__ + ": " + str(exc)[:300],
                                    lease_token=token, owner=owner)
            except LeaseLost:
                return {"status": "lease_lost", "job_id": job["id"]}
            return {"status": "failed", "job_id": job["id"], "error": type(exc).__name__}
        finally:
            stop.set()
            heartbeat.join(timeout=max(1.0, self.heartbeat_interval * 2))

    def run(self, *, max_jobs=1):
        if type(max_jobs) is not int or not 1 <= max_jobs <= 100:
            raise ValueError("max_jobs must be 1..100")
        results = []
        for _ in range(max_jobs):
            item = self.run_once()
            if item["status"] == "idle":
                break
            results.append(item)
        return results
