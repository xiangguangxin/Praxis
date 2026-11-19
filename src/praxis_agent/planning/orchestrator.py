"""Checkpointed sequential DAG orchestration with independent acceptance."""
import json, os, tempfile, time
from pathlib import Path
from .task_graph import TaskGraph, TaskGraphError

class OrchestrationError(ValueError): pass

class TaskOrchestrator:
    """Execute ready DAG nodes; only verified success unlocks dependents."""
    VERSION = 1
    def __init__(self, graph, execute, verify, checkpoint, *, max_attempts=1, max_tasks=100):
        if not isinstance(graph, TaskGraph) or not callable(execute) or not callable(verify):
            raise OrchestrationError("Graph, execute and verify callbacks required")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
            raise OrchestrationError("max_attempts must be 1..5")
        if type(max_tasks) is not int or not 1 <= max_tasks <= 1000:
            raise OrchestrationError("max_tasks must be 1..1000")
        self.graph, self.execute, self.verify = graph, execute, verify
        self.checkpoint = Path(checkpoint)
        if self.checkpoint.exists(): raise FileExistsError("Refusing to overwrite task checkpoint; use resume()")
        self.max_attempts, self.max_tasks = max_attempts, max_tasks
        self.attempts = {n: 0 for n in graph.tasks}
        self.results, self.events, self.status = {}, [], "ready"
        self._save()
    def _payload(self):
        return {"version": 1, "graph": self.graph.to_dict(), "attempts": self.attempts,
                "results": self.results, "events": self.events[-200:], "status": self.status,
                "max_attempts": self.max_attempts, "max_tasks": self.max_tasks}
    def _save(self):
        self.checkpoint.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".praxis-plan-", suffix=".tmp", dir=self.checkpoint.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._payload(), f, ensure_ascii=False, indent=2)
                f.write("\n"); f.flush(); os.fsync(f.fileno())
            os.replace(tmp, self.checkpoint)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
    def _event(self, event, task_id, detail=""):
        self.events.append({"time": time.time(), "event": event, "task_id": task_id, "detail": str(detail)[:240]})
        self.events = self.events[-200:]
    @classmethod
    def resume(cls, checkpoint, execute, verify):
        data = json.loads(Path(checkpoint).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1: raise OrchestrationError("Unsupported checkpoint")
        graph = TaskGraph.from_dict(data["graph"])
        obj = cls.__new__(cls)
        obj.graph, obj.execute, obj.verify = graph, execute, verify
        obj.checkpoint = Path(checkpoint)
        obj.max_attempts, obj.max_tasks = data["max_attempts"], data["max_tasks"]
        obj.attempts, obj.results = data["attempts"], data["results"]
        obj.events, obj.status = data["events"], data["status"]
        if set(obj.attempts) != set(graph.tasks) or set(obj.results) - set(graph.tasks):
            raise OrchestrationError("Corrupt task checkpoint")
        interrupted = [n for n, s in graph.status.items() if s == "running"]
        for n in interrupted:
            graph.transition(n, "blocked")
            obj._event("interrupted", n, "Explicit reconciliation required; never blindly replay")
        if interrupted:
            obj.status = "blocked"; obj._save()
        return obj
    def reconcile(self, task_id, outcome, *, result=None, verified=False):
        if self.graph.status.get(task_id) != "blocked": raise OrchestrationError("Task is not blocked")
        if outcome == "completed":
            if verified is not True or not isinstance(result, dict):
                raise OrchestrationError("Completion requires independently verified result")
            self.results[task_id] = result
            self.graph.transition(task_id, "pending")
            self.graph.transition(task_id, "running")
            self.graph.transition(task_id, "completed")
        elif outcome == "retry":
            self.graph.transition(task_id, "pending")
        elif outcome == "failed":
            self.graph.transition(task_id, "pending")
            self.graph.transition(task_id, "running")
            self.graph.transition(task_id, "failed")
            self._block_descendants(task_id)
        else: raise OrchestrationError("Outcome must be completed, retry or failed")
        self._event("reconciled_" + outcome, task_id)
        self.status = "ready"; self._save()
    def _block_descendants(self, failed_id):
        changed = True
        while changed:
            changed = False
            for name, task in self.graph.tasks.items():
                if self.graph.status[name] == "pending" and any(self.graph.status[d] in {"failed", "blocked"} for d in task.dependencies):
                    self.graph.transition(name, "blocked")
                    self._event("dependency_blocked", name, failed_id); changed = True
    def run(self, *, max_tasks=None):
        if self.status in {"completed", "blocked"}: return self.snapshot()
        budget = self.max_tasks if max_tasks is None else max_tasks
        if type(budget) is not int or not 1 <= budget <= 1000: raise OrchestrationError("max_tasks must be 1..1000")
        processed = 0; self.status = "running"; self._save()
        while processed < budget:
            ready = self.graph.ready()
            if not ready:
                states = set(self.graph.status.values())
                self.status = "completed" if states == {"completed"} else ("blocked" if states & {"failed", "blocked"} else "paused")
                self._save(); return self.snapshot()
            task = ready[0]
            self.graph.transition(task.id, "running")
            self.attempts[task.id] += 1
            self._event("started", task.id, "attempt " + str(self.attempts[task.id])); self._save()
            try:
                result = self.execute(task)
                if not isinstance(result, dict): raise OrchestrationError("Executor must return dict")
                verdict = self.verify(task, result)
                if isinstance(verdict, bool): passed, evidence = verdict, {}
                elif isinstance(verdict, dict) and type(verdict.get("passed")) is bool:
                    passed, evidence = verdict["passed"], verdict.get("evidence", {})
                else: raise OrchestrationError("Verifier must return bool or passed:boolean")
                if passed:
                    self.results[task.id] = {"result": result, "verification": evidence}
                    self.graph.transition(task.id, "completed"); self._event("verified", task.id)
                else:
                    self._event("verification_failed", task.id); self._retry_or_fail(task.id)
            except Exception as exc:
                self._event("execution_error", task.id, type(exc).__name__); self._retry_or_fail(task.id)
            self._save(); processed += 1
            if self.status == "blocked": return self.snapshot()
        self.status = "paused"; self._event("budget_exhausted", ""); self._save()
        return self.snapshot()
    def _retry_or_fail(self, task_id):
        self.graph.transition(task_id, "failed")
        if self.attempts[task_id] < self.max_attempts:
            self.graph.transition(task_id, "pending"); self._event("retry_scheduled", task_id)
        else:
            self._event("task_failed", task_id); self._block_descendants(task_id)
    def snapshot(self):
        return {"status": self.status, "tasks": dict(self.graph.status), "attempts": dict(self.attempts),
                "results": dict(self.results), "checkpoint": str(self.checkpoint)}
