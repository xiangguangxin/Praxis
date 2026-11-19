"""Checkpointed DAG orchestration with independent verification and bounded parallel workers."""
import json
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from .task_graph import Task, TaskGraph, TaskGraphError
from .locks import TaskLockManager
from ..locking import checkpoint_lock

class OrchestrationError(ValueError): pass

class TaskOrchestrator:
    """Run dependency-ready tasks concurrently while fencing shared resources."""
    VERSION = 3

    def __init__(self, graph, execute, verify, checkpoint, *, max_attempts=1,
                 max_tasks=100, max_workers=1, resource_lock_dir=None,
                 resource_lock_timeout=300):
        if not isinstance(graph, TaskGraph) or not callable(execute) or not callable(verify):
            raise OrchestrationError("Graph, execute and verify callbacks required")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
            raise OrchestrationError("max_attempts must be 1..5")
        if type(max_tasks) is not int or not 1 <= max_tasks <= 1000:
            raise OrchestrationError("max_tasks must be 1..1000")
        if type(max_workers) is not int or not 1 <= max_workers <= 32:
            raise OrchestrationError("max_workers must be 1..32")
        self.graph, self.execute, self.verify = graph, execute, verify
        self.checkpoint = Path(checkpoint)
        self.max_attempts, self.max_tasks, self.max_workers = max_attempts, max_tasks, max_workers
        self.resource_lock_dir = Path(resource_lock_dir) if resource_lock_dir else self.checkpoint.parent / ".praxis-resource-locks"
        self.resource_locks = TaskLockManager(self.resource_lock_dir, timeout=resource_lock_timeout)
        self.attempts = {name: 0 for name in graph.tasks}
        self.results, self.events, self.status = {}, [], "ready"
        with checkpoint_lock(self.checkpoint):
            if self.checkpoint.exists():
                raise FileExistsError("Refusing to overwrite task checkpoint; use resume()")
            self._save()

    def _payload(self):
        return {"version": self.VERSION, "graph": self.graph.to_dict(), "attempts": self.attempts,
                "results": self.results, "events": self.events[-200:], "status": self.status,
                "max_attempts": self.max_attempts, "max_tasks": self.max_tasks,
                "max_workers": self.max_workers, "resource_lock_dir": str(self.resource_lock_dir)}

    def _save(self):
        self.checkpoint.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".praxis-plan-", suffix=".tmp", dir=self.checkpoint.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self._payload(), stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.checkpoint)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _event(self, event, task_id, detail=""):
        self.events.append({"time": time.time(), "event": event, "task_id": task_id,
                            "detail": str(detail)[:240]})
        self.events = self.events[-200:]

    @classmethod
    def resume(cls, checkpoint, execute, verify):
        checkpoint = Path(checkpoint)
        with checkpoint_lock(checkpoint):
            data = json.loads(checkpoint.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") not in (1, 2, cls.VERSION):
                raise OrchestrationError("Unsupported checkpoint")
            graph = TaskGraph.from_dict(data["graph"])
            obj = cls.__new__(cls)
            obj.graph, obj.execute, obj.verify = graph, execute, verify
            obj.checkpoint = checkpoint
            obj.max_attempts, obj.max_tasks = data["max_attempts"], data["max_tasks"]
            obj.max_workers = data.get("max_workers", 1)
            obj.resource_lock_dir = Path(data.get("resource_lock_dir") or checkpoint.parent / ".praxis-resource-locks")
            obj.resource_locks = TaskLockManager(obj.resource_lock_dir)
            obj.attempts, obj.results = data["attempts"], data["results"]
            obj.events, obj.status = data["events"], data["status"]
            if (not isinstance(obj.attempts, dict) or set(obj.attempts) != set(graph.tasks)
                    or any(type(value) is not int or value < 0 for value in obj.attempts.values())
                    or not isinstance(obj.results, dict) or set(obj.results) - set(graph.tasks)
                    or not isinstance(obj.events, list)
                    or type(obj.max_attempts) is not int or not 1 <= obj.max_attempts <= 5
                    or type(obj.max_tasks) is not int or not 1 <= obj.max_tasks <= 1000
                    or type(obj.max_workers) is not int or not 1 <= obj.max_workers <= 32):
                raise OrchestrationError("Corrupt task checkpoint")
            interrupted = [name for name, status in graph.status.items() if status == "running"]
            for name in interrupted:
                graph.transition(name, "blocked")
                obj._event("interrupted", name, "Explicit reconciliation required; never blindly replay")
            if interrupted:
                obj.status = "blocked"
                obj._save()
            return obj

    def _block_descendants(self, failed_id):
        changed = True
        while changed:
            changed = False
            for name, task in self.graph.tasks.items():
                if self.graph.status[name] == "pending" and any(
                        self.graph.status[dep] in {"failed", "blocked"} for dep in task.dependencies):
                    self.graph.transition(name, "blocked")
                    self._event("dependency_blocked", name, failed_id)
                    changed = True

    def reconcile(self, task_id, outcome, *, result=None, verified=False):
        with checkpoint_lock(self.checkpoint):
            if self.graph.status.get(task_id) != "blocked":
                raise OrchestrationError("Task is not blocked")
            if outcome == "completed":
                if verified is not True or not isinstance(result, dict):
                    raise OrchestrationError("Completion requires independently verified result")
                self.graph.transition(task_id, "pending")
                self.graph.transition(task_id, "running")
                self.graph.transition(task_id, "completed")
                self.results[task_id] = result
            elif outcome == "retry":
                self.graph.transition(task_id, "pending")
            elif outcome == "failed":
                self.graph.transition(task_id, "pending")
                self.graph.transition(task_id, "running")
                self.graph.transition(task_id, "failed")
                self._block_descendants(task_id)
            else:
                raise OrchestrationError("Outcome must be completed, retry or failed")
            self._event("reconciled_" + outcome, task_id)
            self.status = "ready"
            self._save()
            return self.snapshot()

    def add_task(self, task):
        with checkpoint_lock(self.checkpoint):
            if self.status in {"running", "completed"}:
                raise OrchestrationError("Pause orchestration before changing its DAG")
            try:
                self.graph.add_task(task)
            except TaskGraphError as exc:
                raise OrchestrationError(str(exc)) from exc
            self.attempts[task.id] = 0
            self._event("task_added", task.id, task.goal)
            self.status = "ready"
            self._save()
            return self.snapshot()

    def remove_task(self, task_id):
        with checkpoint_lock(self.checkpoint):
            if self.status in {"running", "completed"}:
                raise OrchestrationError("Pause orchestration before changing its DAG")
            try:
                self.graph.remove_task(task_id)
            except TaskGraphError as exc:
                raise OrchestrationError(str(exc)) from exc
            self.attempts.pop(task_id, None)
            self.results.pop(task_id, None)
            self._event("task_removed", task_id)
            self.status = "ready"
            self._save()
            return self.snapshot()

    def replan_remaining(self, tasks, *, reason):
        with checkpoint_lock(self.checkpoint):
            if self.status == "running":
                raise OrchestrationError("Pause orchestration before replanning")
            if not isinstance(reason, str) or not reason.strip():
                raise OrchestrationError("A replan reason is required")
            proposed = list(tasks)
            old_ids = set(self.graph.tasks)
            try:
                self.graph.replace_pending(proposed)
            except TaskGraphError as exc:
                raise OrchestrationError(str(exc)) from exc
            new_ids = set(self.graph.tasks)
            self.attempts = {name: self.attempts.get(name, 0) for name in new_ids}
            self.results = {name: value for name, value in self.results.items() if name in new_ids}
            self._event("plan_replaced", "", reason + "; removed=" + ",".join(sorted(old_ids-new_ids)))
            self.status = "ready"
            self._save()
            return self.snapshot()

    def replan_with(self, planner, goal, criteria, *, context=None, reason):
        with checkpoint_lock(self.checkpoint):
            if self.status == "running":
                raise OrchestrationError("Pause orchestration before replanning")
            if not callable(getattr(planner, "replan_remaining", None)):
                raise OrchestrationError("A DynamicTaskPlanner is required")
            if not isinstance(reason, str) or not reason.strip():
                raise OrchestrationError("A replan reason is required")
            old_ids = set(self.graph.tasks)
            try:
                planner.replan_remaining(goal, criteria, self.graph, context=context)
            except (TaskGraphError, ValueError) as exc:
                raise OrchestrationError("Dynamic replan rejected: " + str(exc)) from exc
            new_ids = set(self.graph.tasks)
            self.attempts = {name: self.attempts.get(name, 0) for name in new_ids}
            self.results = {name: value for name, value in self.results.items() if name in new_ids}
            self._event("plan_replaced", "", reason + "; removed=" + ",".join(sorted(old_ids-new_ids)))
            self.status = "ready"
            self._save()
            return self.snapshot()

    def _execute_and_verify(self, task):
        started = time.monotonic()
        with self.resource_locks.acquire(task.resources):
            result = self.execute(task)
            if not isinstance(result, dict):
                raise OrchestrationError("Executor must return dict")
            json.dumps(result, ensure_ascii=False, allow_nan=False)
            verdict = self.verify(task, result)
            if isinstance(verdict, bool):
                passed, evidence = verdict, {}
            elif (isinstance(verdict, dict) and type(verdict.get("passed")) is bool
                  and isinstance(verdict.get("evidence", {}), dict)):
                passed, evidence = verdict["passed"], verdict.get("evidence", {})
            else:
                raise OrchestrationError("Verifier must return bool or passed:boolean with evidence object")
            json.dumps(evidence, ensure_ascii=False, allow_nan=False)
        return result, passed, evidence, round((time.monotonic()-started)*1000, 3)

    @staticmethod
    def _select_batch(ready, slots):
        selected, held = [], set()
        for task in ready:
            resources = set(task.resources)
            if resources & held:
                continue
            selected.append(task)
            held.update(resources)
            if len(selected) >= slots:
                break
        return selected

    def run(self, *, max_tasks=None, max_workers=None):
        with checkpoint_lock(self.checkpoint):
            return self._run_locked(max_tasks=max_tasks, max_workers=max_workers)

    def _run_locked(self, *, max_tasks=None, max_workers=None):
        if self.status in {"completed", "blocked"}:
            return self.snapshot()
        budget = self.max_tasks if max_tasks is None else max_tasks
        workers = self.max_workers if max_workers is None else max_workers
        if type(budget) is not int or not 1 <= budget <= 1000:
            raise OrchestrationError("max_tasks must be 1..1000")
        if type(workers) is not int or not 1 <= workers <= 32:
            raise OrchestrationError("max_workers must be 1..32")
        processed = 0
        self.status = "running"
        self._save()
        while processed < budget:
            ready = self.graph.ready()
            if not ready:
                states = set(self.graph.status.values())
                self.status = ("completed" if states == {"completed"} else
                               "blocked" if states & {"failed", "blocked"} else "paused")
                self._save()
                return self.snapshot()
            batch = self._select_batch(ready, min(workers, budget-processed))
            if not batch:
                raise OrchestrationError("Scheduler could not select a ready task")
            for task in batch:
                self.graph.transition(task.id, "running")
                self.attempts[task.id] += 1
                self._event("started", task.id, "attempt " + str(self.attempts[task.id]))
            self._save()
            with ThreadPoolExecutor(max_workers=len(batch), thread_name_prefix="praxis-task") as pool:
                futures = {pool.submit(self._execute_and_verify, task): task for task in batch}
                for future in as_completed(futures):
                    task = futures[future]
                    try:
                        result, passed, evidence, duration = future.result()
                        if passed:
                            self.results[task.id] = {"result": result, "verification": evidence}
                            self.graph.transition(task.id, "completed")
                            self._event("verified", task.id, "duration_ms=" + str(duration))
                        else:
                            self._event("verification_failed", task.id, "duration_ms=" + str(duration))
                            self._retry_or_fail(task.id)
                    except Exception as exc:
                        self._event("execution_error", task.id, type(exc).__name__)
                        self._retry_or_fail(task.id)
                    self._save()
                    processed += 1
        states = set(self.graph.status.values())
        if states == {"completed"}:
            self.status = "completed"
        elif not self.graph.ready() and states & {"failed", "blocked"}:
            self.status = "blocked"
        else:
            self.status = "paused"
            self._event("budget_exhausted", "")
        self._save()
        return self.snapshot()

    def _retry_or_fail(self, task_id):
        self.graph.transition(task_id, "failed")
        if self.attempts[task_id] < self.max_attempts:
            self.graph.transition(task_id, "pending")
            self._event("retry_scheduled", task_id)
        else:
            self._event("task_failed", task_id)
            self._block_descendants(task_id)

    def snapshot(self):
        counts = {status: sum(value == status for value in self.graph.status.values())
                  for status in ("pending", "running", "completed", "failed", "blocked")}
        return {"status": self.status, "tasks": dict(self.graph.status), "attempts": dict(self.attempts),
                "results": dict(self.results), "ready": [task.id for task in self.graph.ready()],
                "aggregate": {"total": len(self.graph.tasks), "counts": counts,
                              "attempts": sum(self.attempts.values())},
                "checkpoint": str(self.checkpoint)}
