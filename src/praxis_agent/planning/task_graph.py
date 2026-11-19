"""Validated mutable DAG with guarded node transitions and backward-compatible snapshots."""
from dataclasses import dataclass

class TaskGraphError(ValueError):
    pass

@dataclass(frozen=True)
class Task:
    id: str
    goal: str
    dependencies: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    resources: tuple[str, ...] = ()

class TaskGraph:
    STATUSES = {"pending", "running", "completed", "failed", "blocked"}

    def __init__(self, tasks, status=None):
        tasks = list(tasks)
        if not tasks or any(not isinstance(task, Task) for task in tasks):
            raise TaskGraphError("Tasks must be nonempty Task objects")
        if any(not isinstance(task.id, str) or not task.id.strip() for task in tasks):
            raise TaskGraphError("Task IDs must be nonempty strings")
        self.tasks = {task.id: task for task in tasks}
        if len(self.tasks) != len(tasks):
            raise TaskGraphError("Tasks must have unique IDs")
        for task in tasks:
            if (not isinstance(task, Task) or not isinstance(task.id, str) or not task.id.strip()
                    or len(task.id) > 80 or not isinstance(task.goal, str) or not task.goal.strip()
                    or len(task.goal) > 1000):
                raise TaskGraphError("Task ID and goal required")
            if (not isinstance(task.dependencies, tuple)
                    or any(not isinstance(d, str) or d not in self.tasks or d == task.id for d in task.dependencies)
                    or len(set(task.dependencies)) != len(task.dependencies)):
                raise TaskGraphError("Invalid dependencies")
            if (not isinstance(task.acceptance, tuple) or len(task.acceptance) > 10
                    or any(not isinstance(c, str) or not c.strip() or len(c) > 300 for c in task.acceptance)):
                raise TaskGraphError("Invalid task acceptance criteria")
            if (not isinstance(task.resources, tuple) or len(task.resources) > 16
                    or any(not isinstance(r, str) or not r.strip() or len(r) > 128 for r in task.resources)
                    or len(set(task.resources)) != len(task.resources)):
                raise TaskGraphError("Task resources must be unique nonempty strings of at most 128 characters")
        self._validate_acyclic()
        self.status = dict(status) if status is not None else {name: "pending" for name in self.tasks}
        if set(self.status) != set(self.tasks) or any(s not in self.STATUSES for s in self.status.values()):
            raise TaskGraphError("Invalid task statuses")
        for name, value in self.status.items():
            if value in {"running", "completed"} and any(self.status[d] != "completed" for d in self.tasks[name].dependencies):
                raise TaskGraphError("Unsatisfied dependency")

    def _validate_acyclic(self):
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

    def ready(self):
        return [task for task in self.tasks.values()
                if self.status[task.id] == "pending"
                and all(self.status[dep] == "completed" for dep in task.dependencies)]

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
        self.tasks[name] = Task(name, goal, task.dependencies, task.acceptance, task.resources)

    def add_task(self, task):
        if not isinstance(task, Task) or task.id in self.tasks:
            raise TaskGraphError("New task must have a unique ID")
        candidate = list(self.tasks.values()) + [task]
        validated = TaskGraph(candidate, {**self.status, task.id: "pending"})
        self.tasks, self.status = validated.tasks, validated.status

    def remove_task(self, name):
        if name not in self.tasks or self.status[name] != "pending":
            raise TaskGraphError("Only existing pending tasks can be removed")
        if any(name in task.dependencies for task in self.tasks.values()):
            raise TaskGraphError("Cannot remove a task with dependents")
        if len(self.tasks) == 1:
            raise TaskGraphError("A plan must retain at least one task")
        del self.tasks[name]
        del self.status[name]

    def replace_pending(self, tasks):
        """Replace the unfinished subgraph while preserving completed nodes verbatim."""
        if any(value == "running" for value in self.status.values()):
            raise TaskGraphError("Cannot replan while a task is running")
        proposed = list(tasks)
        if not proposed:
            raise TaskGraphError("Replacement plan cannot be empty")
        completed = [task for task in self.tasks.values() if self.status[task.id] == "completed"]
        completed_ids = {task.id for task in completed}
        if any(not isinstance(task, Task) or task.id in completed_ids for task in proposed):
            raise TaskGraphError("Replacement cannot overwrite completed tasks")
        merged = completed + proposed
        statuses = {task.id: "completed" for task in completed}
        statuses.update({task.id: "pending" for task in proposed})
        validated = TaskGraph(merged, statuses)
        self.tasks, self.status = validated.tasks, validated.status

    def to_dict(self):
        return {"version": 1,
                "tasks": [{"id": t.id, "goal": t.goal, "dependencies": list(t.dependencies),
                           "acceptance": list(t.acceptance), "resources": list(t.resources)} for t in self.tasks.values()],
                "status": dict(self.status)}

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("tasks"), list):
            raise TaskGraphError("Invalid graph schema")
        try:
            tasks = []
            for item in data["tasks"]:
                if not isinstance(item, dict) or set(item) - {"id", "goal", "dependencies", "acceptance", "resources"}:
                    raise TaskGraphError("Invalid task fields")
                tasks.append(Task(item["id"], item["goal"], tuple(item.get("dependencies", [])),
                                  tuple(item.get("acceptance", [])), tuple(item.get("resources", []))))
            return cls(tasks, data.get("status"))
        except (TypeError, KeyError) as exc:
            raise TaskGraphError("Invalid graph data") from exc
