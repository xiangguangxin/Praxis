<div align="center">

# Praxis

**Goal-Driven Autonomous Agent Runtime**

**Plan. Act. Verify. Learn. Improve.**

[![CI](https://github.com/xiangguangxin/Praxis/actions/workflows/ci.yml/badge.svg)](https://github.com/xiangguangxin/Praxis/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-059669)](LICENSE)

[Demo & acceptance](docs/ACCEPTANCE.md) · [Quick start](#quick-start) · [Architecture](#architecture) · [Verification](#independent-verification) · [Experience](#experience-memory) · [Self-evolution](#self-evolution) · [Benchmarks](#benchmark--evaluation) · [中文](#中文介绍)

</div>

Praxis is a **goal-driven agent execution runtime** extracted from [OpenAS](https://github.com/xiangguangxin/OpenAS). It runs bounded observe → plan → act → verify loops against **Windows/macOS desktop tools (Nova MCP)** or an **isolated Chromium browser**, with resumable JSON checkpoints, approval gates, artifact verification, and opt-in experience retrieval.

> **Status:** Early-stage research and engineering runtime. Synthetic tests and opt-in browser fixtures are not proof of reliable real-world desktop autonomy. Praxis is not a daemon and does not run unattended in the background.

## Why Praxis?

| Principle | Current mechanism |
| --- | --- |
| **Plan** | Multimodal planner plus validated, mutable DAG orchestration with per-node acceptance checks |
| **Act** | Tool adapters for authorized Nova MCP desktops or Playwright browser sessions |
| **Verify** | Optional independent allowlisted file checks, provenance, freshness, and tool trajectory rules |
| **Learn** | Curated verified experiences plus benchmark-gated, versioned strategy promotion and rollback; task-aware model routing |
| **Recover** | Durable checkpoint, process lock, manual reconciliation after uncertain actions |

## Quick start

Requires **Python 3.10+**, an authorized tool backend, and a separately configured multimodal model endpoint for live execution.

```bash
git clone https://github.com/xiangguangxin/Praxis.git
cd Praxis
python -m pip install -e .
praxis create ./goal-state.json --goal "Inspect an authorized app" --criterion "Expected output verified"
praxis status ./goal-state.json
praxis doctor
```

Set `PRAXIS_MODEL` and `PRAXIS_API_KEY` (or the backwards-compatible `OPENAS_GUI_MODEL` and `OPENAS_GUI_API_KEY`). For a Nova desktop session:

```bash
praxis run ./goal-state.json --nova /absolute/path/to/nova
praxis resume ./goal-state.json --nova /absolute/path/to/nova
```

The Nova executable must be installed and authorized separately. See [OpenAS Nova MCP integration](https://github.com/xiangguangxin/OpenAS/blob/main/plugins/openas/integrations/computer-use/USER-GUIDE.md).

For browser goals:

```bash
python -m pip install -e ".[browser]"
python -m playwright install chromium
praxis run ./goal-state.json --browser-url https://example.com/
```

Only test web apps you are authorized to automate. Browser execution uses an isolated, nonpersistent context and is not a security sandbox for hostile websites.

## Try a complete browser goal

```bash
python -m pip install -e ".[browser]"
python -m playwright install chromium
praxis doctor --browser
praxis demo --output ./demo-run
```

Open `demo-run/index.html` for the actual final screenshot, downloaded report and
independent verification results. This is a **real Chromium integration demo with
a deterministic fixture planner**, requiring no API key. It is not a real-model
success-rate claim. [Reproduce it or run live acceptance](docs/ACCEPTANCE.md).
See the [recorded validation and downloadable demo](docs/VALIDATION.md).

## Architecture

```mermaid
flowchart TD
    G["Goal + acceptance criteria"] --> P["Model planner"]
    P --> O["Observe"]
    O --> A{"Approval policy"}
    A -->|authorized| T["Tool adapter"]
    A -->|sensitive action| H["Pause for approval"]
    T --> N["Nova MCP · Desktop"]
    T --> B["Playwright · Browser"]
    N --> C["Checkpoint + tool trace"]
    B --> C
    C --> V["Independent verifier (opt-in)"]
    V -->|not satisfied| P
    V -->|satisfied| D["Verified deliverable"]
    E["Local ExperienceStore (opt-in)"] -.-> P
```

Completion records a `verification_level`: `model_evidence`,
`independent_artifact`, or `custom_verifier`. Legacy completed checkpoints without
provenance read as `legacy_unspecified`. The CLI exits with 0 for completed, 3 for
paused, 4 for blocked and 5 for failed execution.

The planner may assert completion; **without an independent verifier**, this is model-evidence compatibility mode rather than independently established success. Task-level verification failures can trigger a bounded replacement of unfinished DAG work. Uncertain actions still require explicit reconciliation. If an action's outcome is uncertain after a crash, Praxis blocks replay until the operator reconciles it.

## Autonomous DAG orchestration (Phase 5)\n\nThe reusable `planning` package supports per-node dependencies, acceptance checks, independent verification evidence, bounded retries, checkpoint/resume, explicit interruption reconciliation, and safe replacement of unfinished work while preserving completed nodes. It also supports adding/removing pending nodes. See [Phase 5 orchestration guide](RUNTIME.md#phase-5-autonomous-dag-orchestration). The GUI runtime enforces the validated task plan with independent per-task acceptance checks when a verifier is configured.\n\n## Independent verification

Tasks can use trusted intermediate checks while final Goal criteria are verified
separately. See [layered acceptance](docs/layered-acceptance.md).

[Configured verifiers](docs/configured-verifiers.md) support DOM, HTTP JSON,
read-only SQLite, application state, screenshot regions and local artifact checks.

Use a trusted verifier rules file, mapped to the checkpoint criteria:

```bash
praxis run ./goal-state.json --nova /absolute/path/to/nova --verifier ./rules.json
praxis verify ./goal-state.json --verifier ./rules.json
praxis reconcile ./goal-state.json --outcome executed --note "Independently checked the desktop"
```

The local file verifier supports safe root-constrained paths, minimum size, content checks, JSON fields, changed-artifact freshness and run IDs. It does **not** prove visual UI correctness by itself. See [verifier fixtures](examples/coverage-artifact-verifier.json) and the [full runtime guide](RUNTIME.md).

## Review and recovery

Use the [read-only run timeline](docs/run-timeline.md) to inspect observation IDs,
model batches, tool outcomes, task states and recovery events as HTML or JSON.

[Model request controls](docs/model-resilience.md) add bounded provider retries,
capability-preserving failover, endpoint circuits, cost accounting and resumable
run deadlines. Tool reconciliation and approval gates remain enforced.

Advertised tool schemas are validated locally before dispatch; unknown tools or
invalid arguments never reach the backend. Remote schema references are not fetched.

Sensitive tool names, control targets and executable payloads trigger review even
when a planner labels them low impact. `praxis status` exposes a redacted
`pending_review` with its exact fingerprint and expiry. Review the actual target
before resuming with `--approve-pending --approval-id <fingerprint>`. A changed
observed view, action, policy or backend requires a new review. Keep passing the
same `--policy` and `--verifier` on resume.

Trusted operator policies support exact argument matches with `allow`, `approve`
and `deny` effects. See [policy example](examples/action-policy.json) and
[approval and recovery details](RUNTIME.md#reviewable-approval-and-action-policy).
Policies aid authorization; they are not a security sandbox and cannot infer all
UI effects. `praxis retry` reopens known-outcome failures; uncertain in-flight
actions still require `praxis reconcile`. Ctrl+C persists a paused or blocked state;
backend bindings are checked before a new session starts.

## Experience memory

Successful runs can become **curated** experiences only after trusted file verification, with operator-provided notes. Prior experiences are *untrusted planning hints* and are **never automatically replayed**.

```bash
praxis-experience --store ./experiences.json add ./goal-state.json \
  --verifier ./rules.json --environment win11-app-v1 \
  --note "Output inspected"
praxis run ./goal-state.json --nova /absolute/path/to/nova \
  --experience-store ./experiences.json --experience-environment win11-app-v1
```

Matching uses simple lexical overlap and exact environment tags, not embeddings.

### Failure mining and experience clustering

Praxis includes offline, deterministic learning analytics. Failure mining scans saved checkpoints and groups trusted runtime failure signatures; it reports recurrence and source checkpoint IDs, but does not claim that frequency proves a root cause. Experience clustering considers only active, independently verified entries and keeps environments separate. It uses weighted lexical Jaccard similarity with complete-link assignment, so every pair in a cluster meets the configured threshold. No embedding provider or model call is required.

```bash
# Mine current failure snapshots; add --recursive for nested checkpoint folders.
praxis-learning mine-failures --checkpoints ./checkpoints --min-occurrences 2 \
  --output ./reports/failure-patterns.json

# Cluster curated experiences for one runtime environment.
praxis-learning cluster-experiences --store ./experiences.json \
  --environment win11-app-v1 --min-similarity 0.35 \
  --output ./reports/experience-clusters.json
```

These reports are analysis artifacts, not trusted strategies. Pattern frequency is not root-cause confidence; clusters are lexical similarity groups, not semantic equivalence. Candidate generation and benchmark-gated promotion remain separate explicit steps. Default confidence half-life: 30 days; retention eligibility: 90 days; two explicitly recorded verified failures invalidate an experience. Experience writes use a process lock. The store is neither encrypted nor
access-controlled. Curated entries can include preconditions, key steps, failure
modes and independent verification references; these remain untrusted planning hints.

## Benchmark & evaluation

The [twelve-task live acceptance suite](docs/live-benchmark-suite.md) includes
resettable browser tasks and independently checked outputs, with latency,
false-completion and statistical uncertainty metrics.

```bash
praxis-benchmark examples/benchmark-suite.json
python -m unittest discover -s tests -v
python scripts/check_runtime_mutations.py
```

Offline fixtures are synthetic and must not be presented as real desktop success rates. Live Nova and browser benchmarks require real backends and independent acceptance checks; see [benchmark guide](RUNTIME.md#agent-benchmark).

## Extended runtime primitives\n\nPraxis also provides opt-in building blocks for model-proposed DAG validation (DynamicTaskPlanner), deterministic context packing, capability-aware model profile selection, a durable local SQLite job queue, and an allowlisted operational event journal. These are composable library APIs; they do not automatically launch background workers or change the existing CLI agent loop. See [Phases 1–4 implementation status](docs/phases-1-to-4.md).\n\n## Boundaries and roadmap

**Included now:** bounded agent loop, Nova/browser adapters, model gateway selection, restart-based resume and reconciliation, approval policy, local artifact verification, trace comparison, experience lifecycle, offline/live evaluation harnesses.

**Not guaranteed:** continuous background execution, remote distributed scheduling, zero-configuration providers, general-purpose sandboxing, unsupervised experience learning, real Windows/macOS E2E reliability, or permissionless tool access.

**Validation:** CI runs Linux/Windows/macOS unit checks and real Chromium
integration with a scripted model. A separate manual workflow supports real-model
browser acceptance and configured Nova desktop runners. See the
[acceptance guide](docs/ACCEPTANCE.md). General desktop reliability still requires
measured native application trials. API compatibility is not yet frozen.

The project was extracted from [OpenAS](https://github.com/xiangguangxin/OpenAS). See [migration and compatibility notes](MIGRATION.md), [detailed documentation](RUNTIME.md) and the [MIT license](LICENSE).

---

<a id="中文介绍"></a>

## 中文介绍

**Praxis — 目标驱动的自主 Agent Runtime**，以 **规划 → 行动 → 验证 → 经验复用** 为核心闭环。

Praxis 从 OpenAS 的 GUI Agent Runtime 独立而来，目前支持 **Nova MCP 桌面操作**与 **Playwright 浏览器操作**，提供 checkpoint 恢复、敏感操作审批、可选的独立文件验收、Benchmark 及经验检索与失效机制。**模拟测试不代表真实桌面端到端验证通过**，经验提示也不会直接变成自动执行命令。

使用 `pip install -e .` 安装，运行 `praxis --help`，完整参数参考 [RUNTIME.md](RUNTIME.md)。OpenAS 继续维护 Agents & Skills，与 Praxis 以工具集成形式协作。

## Integrated planning, verified task execution, model routing and scheduled jobs

The GUI execution loop can call `ModelPlanner.prepare_plan()` before its first action and checkpoint the task DAG. With an independent `--verifier` configuration and task acceptance entries mapped exactly to Goal criteria, Praxis activates tasks in dependency order and independently verifies each `task_complete` request. Failed checks retry within a bounded budget; exhausted failures block dependents. Task progress and per-task fresh-artifact baselines are stored in the Goal checkpoint. Plans without an independent verifier or valid acceptance mapping remain advisory. Every GUI action still uses fresh observation, schema validation, operator policy and approval gates.

Optional model routing uses a JSON profile file:

```json
{
  "default": "economy",
  "profiles": [
    {"name":"economy","model":"small-model","endpoint":"https://provider.example/v1/chat/completions","api_key_env":"SMALL_MODEL_KEY","capabilities":["chat"],"max_cost_tier":1},
    {"name":"vision","model":"vision-model","endpoint":"https://provider.example/v1/chat/completions","api_key_env":"VISION_MODEL_KEY","capabilities":["chat","vision"],"max_cost_tier":3}
  ]
}
```

Use `--model-profiles ./models.json` with `praxis run` or `resume`. The router selects a vision-capable profile when screenshots are present and prefers a higher cost tier after failed actions. Set each profile's API key environment variable before running. Routing never bypasses action approvals.

Queue a goal for a bounded local worker:

```bash
praxis schedule --database ./praxis-jobs.sqlite --goal "Inspect the report" --criterion "Report was verified"
praxis worker --database ./praxis-jobs.sqlite --checkpoint-dir ./job-checkpoints --max-jobs 1 --nova /path/to/nova --model-profiles ./models.json
```

The worker exits after at most `--max-jobs` and marks only an agent result with status `completed` as a completed job. Each claimed job has a renewable lease and fencing token. Its checkpoint path and checkpoint ID are durably bound to the job under that lease, while the checkpoint also records its scheduled job ID. A retry reuses the same checkpoint instead of creating a fresh one; if a checkpoint that was already bound disappears or conflicts with its job, Praxis fails closed rather than silently losing execution history. If the process crashes between checkpoint publication and SQLite binding, the next lease can adopt the deterministic checkpoint after validating its goal and criteria. Use `praxis audit-state --database ./praxis-jobs.sqlite --checkpoint-dir ./job-checkpoints` for a read-only consistency report. Paused or blocked goals become failed queue entries for operator follow-up; explicit retry preserves their checkpoint and resets the attempt budget. This is a foreground local worker backed by SQLite, not a remote distributed scheduler.

Verified scheduled completions are published to a SQLite transactional outbox in the same transaction that finishes the job. Only results carrying passing independent artifact-verification evidence publish learning events. Delivery is at-least-once with per-consumer leases, fencing, bounded exponential retry and dead-letter status; consumers are idempotent to tolerate a crash after their side effect but before acknowledgement.

Deliver a verified checkpoint into Experience Store with an explicit operator-curated note and independent verifier:

```bash
praxis outbox-dispatch --database ./praxis-jobs.sqlite --consumer experience \
  --store ./experiences.json --verifier ./verifier.json \
  --environment linux-ci --note "Operator-reviewed verified workflow"
```

Record verified source availability for Evolution (this records provenance only; it does not create or promote a strategy):

```bash
praxis outbox-dispatch --database ./praxis-jobs.sqlite --consumer evolution \
  --store ./evolution.json
```

Experience and Evolution deliveries have independent status and retry budgets. Strategy creation and promotion remain explicit and require paired benchmark evidence.

The library-level DAG orchestrator can run independent task callbacks concurrently with `max_workers`, serialize tasks that share declared resource names, and aggregate per-task results and verification evidence. The integrated GUI runtime remains serial for one shared desktop session. See the Phase 5.4 guide in [RUNTIME.md](RUNTIME.md#parallel-task-execution-and-resource-locks-phase-54).


## Self-evolution

Praxis keeps learning conservative and auditable: a strategy candidate must cite an active independently verified experience, pass paired baseline-vs-candidate benchmark evaluation with matching provenance, and be explicitly promoted before normal planners can retrieve it. Candidates are never silently activated. Promoted strategies remain untrusted hints; they cannot authorize tools or satisfy acceptance criteria. Rollback immediately removes a strategy from retrieval; a failed reassessment automatically suspends a regressing strategy.

See the [Phase 6 self-evolution guide](RUNTIME.md#phase-6-evidence-gated-self-evolution) for the full proposal → paired evaluation → promotion → rollback workflow. This phase does not self-modify runtime source code.
Insight-grounded generation is an explicit opt-in step. It requires a repeated failure pattern (at least two observations), a multi-member experience cluster, and a source experience that remains independently verified and active. Generated candidates reuse only operator-curated steps shared by every cluster member; recovery wording comes from fixed runtime guidance, not report-provided text. Identical insight inputs are idempotently deduplicated. This creates an inert candidate only—paired benchmark evaluation and explicit promotion are still required.

```bash
praxis-evolution --store ./evolution.json generate-from-insights \
  --experience-store ./experiences.json \
  --cluster-report ./reports/experience-clusters.json \
  --failure-report ./reports/failure-patterns.json \
  --cluster-id <cluster-id> --pattern-id <pattern-id> \
  --source-experience-id <verified-experience-id>
```

