"""Checkpoint-friendly task lifecycle embedded in the GUI goal runtime."""
from .task_graph import TaskGraph, TaskGraphError

class TaskRuntimeError(ValueError):
    pass

class TaskPlanRuntime:
    VERSION = 1

    def __init__(self, plan, criteria, *, max_attempts=2, data=None):
        if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
            raise TaskRuntimeError("max_attempts must be 1..5")
        source = data.get("graph") if isinstance(data, dict) else plan
        try:
            self.graph = TaskGraph.from_dict(source) if isinstance(source, dict) else TaskGraph(source)
        except (TaskGraphError, TypeError) as exc:
            raise TaskRuntimeError("Invalid task plan: " + str(exc)) from exc
        self.criteria = list(criteria)
        self.max_attempts = data.get("max_attempts", max_attempts) if data else max_attempts
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 5:
            raise TaskRuntimeError("Invalid task retry budget")
        if data is None:
            self.attempts = {name: 0 for name in self.graph.tasks}
            self.results, self.baselines, self.events = {}, {}, []
        else:
            if data.get("version") != self.VERSION:
                raise TaskRuntimeError("Unsupported task runtime checkpoint")
            self.attempts, self.results = data.get("attempts"), data.get("results")
            self.baselines, self.events = data.get("baselines"), data.get("events")
            if (not isinstance(self.attempts, dict) or set(self.attempts) != set(self.graph.tasks)
                    or any(type(value) is not int or value < 0 for value in self.attempts.values())
                    or not isinstance(self.results, dict) or set(self.results) - set(self.graph.tasks)
                    or not isinstance(self.baselines, dict) or set(self.baselines) - set(self.graph.tasks)
                    or not isinstance(self.events, list)):
                raise TaskRuntimeError("Corrupt task runtime checkpoint")
        self._validate_acceptance()

    def _validate_acceptance(self):
        allowed, covered = set(self.criteria), set()
        for task in self.graph.tasks.values():
            if not task.acceptance or not set(task.acceptance).issubset(allowed):
                raise TaskRuntimeError("Every task must declare acceptance criteria from the goal criteria")
            covered.update(task.acceptance)
        if covered != allowed:
            raise TaskRuntimeError("Task plan must cover every goal acceptance criterion")

    def _event(self, event, task_id, detail=""):
        self.events.append({"event": event, "task_id": task_id, "detail": str(detail)[:200]})
        self.events = self.events[-100:]

    def active(self):
        running = [task for task in self.graph.tasks.values() if self.graph.status[task.id] == "running"]
        if len(running) > 1:
            raise TaskRuntimeError("Only one GUI task may be active at a time")
        if running:
            return running[0]
        ready = self.graph.ready()
        if not ready:
            return None
        task = ready[0]
        self.graph.transition(task.id, "running")
        self.attempts[task.id] += 1
        self._event("task_started", task.id, "attempt " + str(self.attempts[task.id]))
        return task

    def complete(self, task_id, verification):
        if self.graph.status.get(task_id) != "running":
            raise TaskRuntimeError("Only the active task can complete")
        if not isinstance(verification, dict) or verification.get("passed") is not True:
            raise TaskRuntimeError("Task completion requires independent passing verification")
        self.graph.transition(task_id, "completed")
        self.results[task_id] = verification
        self._event("task_verified", task_id)

    def reject(self, task_id, verification):
        if self.graph.status.get(task_id) != "running":
            raise TaskRuntimeError("Only the active task can fail")
        self.graph.transition(task_id, "failed")
        self._event("task_verification_failed", task_id, verification)
        if self.attempts[task_id] < self.max_attempts:
            self.graph.transition(task_id, "pending")
            self._event("task_retry_scheduled", task_id)
        else:
            self._block_descendants(task_id)

    def _block_descendants(self, failed_id):
        changed = True
        while changed:
            changed = False
            for name, task in self.graph.tasks.items():
                if self.graph.status[name] == "pending" and any(
                        self.graph.status[dep] in {"failed", "blocked"} for dep in task.dependencies):
                    self.graph.transition(name, "blocked")
                    self._event("task_dependency_blocked", name, failed_id)
                    changed = True

    @property
    def status(self):
        states = set(self.graph.status.values())
        if states == {"completed"}:
            return "completed"
        if states & {"failed", "blocked"} and not self.graph.ready() and not any(s == "running" for s in states):
            return "blocked"
        return "running"

    def to_dict(self):
        return {"version": self.VERSION, "graph": self.graph.to_dict(), "attempts": dict(self.attempts),
                "results": dict(self.results), "baselines": dict(self.baselines),
                "events": list(self.events[-100:]), "max_attempts": self.max_attempts}
