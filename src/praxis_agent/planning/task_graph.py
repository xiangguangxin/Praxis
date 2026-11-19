"""Deterministic DAG planning with serializable state and guarded transitions."""
from dataclasses import dataclass

class TaskGraphError(ValueError):
    pass

@dataclass(frozen=True)
class Task:
    id: str
    goal: str
    dependencies: tuple[str, ...] = ()

class TaskGraph:
    def __init__(self, tasks, status=None):
        self.tasks = {t.id: t for t in tasks}
        if not tasks or len(self.tasks) != len(tasks):
            raise TaskGraphError("Tasks must have unique IDs")
        for task in tasks:
            if not isinstance(task.id, str) or not task.id.strip() or not isinstance(task.goal, str) or not task.goal.strip():
                raise TaskGraphError("Task ID and goal required")
            if not isinstance(task.dependencies, tuple) or len(set(task.dependencies)) != len(task.dependencies) or any(d not in self.tasks or d == task.id for d in task.dependencies):
                raise TaskGraphError("Invalid dependencies")
        visited, visiting = set(), set()
        def visit(name):
            if name in visiting:
                raise TaskGraphError("Dependency cycle")
            if name in visited:
                return
            visiting.add(name)
            for dep in self.tasks[name].dependencies:
                visit(dep)
            visiting.remove(name)
            visited.add(name)
        for name in self.tasks:
            visit(name)
        self.status = dict(status) if status is not None else {name: "pending" for name in self.tasks}
        if set(self.status) != set(self.tasks) or any(s not in {"pending", "running", "completed", "failed", "blocked"} for s in self.status.values()):
            raise TaskGraphError("Invalid task statuses")
        for name, s in self.status.items():
            if s in {"running", "completed"} and any(self.status[d] != "completed" for d in self.tasks[name].dependencies):
                raise TaskGraphError("Unsatisfied dependency")

    def ready(self):
        return [t for t in self.tasks.values() if self.status[t.id] == "pending" and all(self.status[d] == "completed" for d in t.dependencies)]

    def transition(self, name, next_status):
        if name not in self.tasks:
            raise TaskGraphError("Unknown task")
        old = self.status[name]
        allowed = {"pending": {"running", "blocked"}, "running": {"completed", "failed", "blocked"},
                   "failed": {"pending"}, "blocked": {"pending"}, "completed": set()}
        if next_status not in allowed[old]:
            raise TaskGraphError("Invalid task transition")
        if next_status == "running" and self.tasks[name] not in self.ready():
            raise TaskGraphError("Dependencies not complete")
        self.status[name] = next_status

    def replan(self, name, goal):
        if self.status.get(name) != "pending" or not isinstance(goal, str) or not goal.strip():
            raise TaskGraphError("Only pending tasks can be replanned")
        task = self.tasks[name]
        self.tasks[name] = Task(name, goal, task.dependencies)

    def to_dict(self):
        return {"version": 1, "tasks": [{"id": t.id, "goal": t.goal, "dependencies": list(t.dependencies)}
                                        for t in self.tasks.values()], "status": dict(self.status)}

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("tasks"), list):
            raise TaskGraphError("Invalid graph schema")
        try:
            return cls([Task(t["id"], t["goal"], tuple(t.get("dependencies", []))) for t in data["tasks"]], data.get("status"))
        except (TypeError, KeyError) as exc:
            raise TaskGraphError("Invalid graph data") from exc
