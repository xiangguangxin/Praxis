# Praxis roadmap implementation status: Phases 1–4

## Phase 1 — Planning, tools and verification
- Task DAG and checkpointed sequential orchestrator are implemented.
- DynamicTaskPlanner validates model-proposed task graphs before execution. It accepts an injected proposal callback; provider-specific structured generation and closed-loop replanning remain integration work.
- ToolRegistry and independent VerificationRegistry are opt-in contracts. RegisteredToolsBackend dispatches low-impact tools only.

## Phase 2 — Safe execution and recovery
- Existing write-ahead action checkpoint, process lock, explicit crash reconciliation and action approval gates remain the primary safety boundary.
- Nova MCP and Playwright adapters provide structured UI/API-first execution with visual fallback in the desktop integration.
- Tool-specific enforced deadlines and distributed cancellation remain future work.

## Phase 3 — Context, memory and model selection
- ExperienceStore supports independently verified records, retrieval hints, decay, feedback and invalidation.
- ModelPlanner bounds observations and tracks usage; pack_context adds deterministic context packing that preserves goal and acceptance criteria.
- ModelRouter selects configured profiles by capabilities and risk. It does not hold credentials or call providers; callers resolve credentials and create provider clients.

## Phase 4 — Scheduling, observability and evaluation
- SQLite Scheduler persists local jobs and supports atomic due-job claiming, finish, fail and retry. It is a local queue, not a distributed worker service and does not launch arbitrary code.
- EventJournal records allowlisted operational events. It intentionally excludes prompts, screenshots, UI trees, credentials and raw action payloads.
- Existing benchmark provenance, synthetic scenarios, opt-in live browser/Nova runs, tool trajectory checks and trace comparison remain the evaluation foundation.

## Boundaries
All additions are opt-in and use Python standard library. Existing GUI checkpoint schemas and CLI commands are unchanged. Unit tests do not establish real Windows/macOS end-to-end reliability. See task-orchestrator.md, phase1.md and RUNTIME.md.
