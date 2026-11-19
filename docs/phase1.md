# Phase 1 — Planning, Tools and Verification

Praxis now includes three **opt-in, backwards-compatible** primitives:

- `praxis_agent.planning.TaskGraph`: serializable DAG with dependency validation, explicit state transitions, ready-task selection and pending-only goal revision.
- `praxis_agent.tools.ToolRegistry`: explicit allowlist of handlers, typed arguments, risk metadata and conservative dispatch. `RegisteredToolsBackend` adapts registered **low-impact** tools to the existing observe/act protocol. Medium/high impact tools are **not dispatched** by this adapter; a future integration must pass the runtime's exact-action approval token safely. Timeout is metadata, not an enforced kill switch.
- `praxis_agent.verifiers.VerificationRegistry` and `CriteriaVerifier`: independently supplied acceptance callbacks. `CriteriaVerifier` integrates with `agent.run(..., verifier=...)`, without trusting model completion claims.

Example:

```python
from praxis_agent.planning import Task, TaskGraph
from praxis_agent.tools import ToolRegistry, ToolSpec, RegisteredToolsBackend
from praxis_agent.verifiers.registry import VerificationRegistry, CriteriaVerifier

graph = TaskGraph([Task("build", "Build project"), Task("test", "Run tests", ("build",))])
assert [task.id for task in graph.ready()] == ["build"]

tools = ToolRegistry()
tools.register(ToolSpec("read_status", lambda args: {"ok": True}, {}, impact="low"))
backend = RegisteredToolsBackend(tools, lambda: {"summary": "ready"})

checks = VerificationRegistry()
checks.register("trusted_status", lambda params: {"passed": True, "evidence": {"test_exit": 0}})
verifier = CriteriaVerifier(checks, {"tests passed": {"verifier": "trusted_status"}})
# Pass verifier=verifier into praxis_agent.agent.run(...).
```

**Scope:** TaskGraph is a reusable planning primitive, not yet a fully automatic multi-task orchestrator. Callers must persist graph state themselves and choose when to dispatch each ready task. No arbitrary shell/API tools are enabled by default. Existing checkpoint JSON v1/v2, CLI commands, FileVerifier and ExperienceStore remain unchanged. Future phases can add graph-aware orchestration and enforceable per-tool deadlines without expanding the default permission surface.
