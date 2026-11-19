# Task Orchestrator

The opt-in TaskOrchestrator executes a TaskGraph in deterministic topological order. Each task is persisted as running before dispatch; only independently verified success unlocks dependent tasks.

## Example

```python
from pathlib import Path
from praxis_agent.planning import Task, TaskGraph, TaskOrchestrator

graph = TaskGraph([Task("build", "Build"), Task("test", "Run tests", ("build",))])
runner = TaskOrchestrator(
    graph,
    execute=lambda task: {"artifact": task.id},
    verify=lambda task, result: {"passed": True, "evidence": {"checked": task.id}},
    checkpoint=Path("task-plan.json"),
    max_attempts=2,
)
print(runner.run())
```

## Safety and recovery

- Execution is sequential; parallel scheduling is deferred.
- A task is complete only after its verifier returns passed=true.
- Retries are bounded (1–5). Callbacks must be idempotent or perform preflight checks; the orchestrator cannot infer external side effects after a crash.
- Checkpoints are atomically replaced after transitions. A resumed running task becomes blocked and is never blindly replayed. Explicit reconciliation is required.
- Failed tasks block descendants. Independent branches can finish before the plan becomes blocked.
- Opt-in only; existing GUI checkpoints, CLI behavior, Nova permissions and model planner remain unchanged.
