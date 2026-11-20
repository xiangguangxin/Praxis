# Praxis Runtime — Detailed Guide

Standalone Python package extracted from OpenAS. Provides a bounded observe–decide–act loop, pluggable model, desktop and browser adapters, and resumable JSON checkpoints. It is **not** a daemon or independently verified autonomous agent.

```sh
python -m pip install -e .
praxis create ./goal-state.json --goal "Check authorized GUI" --criterion "Expected result visible"
praxis status ./goal-state.json
# After setting OPENAS_GUI_API_KEY and OPENAS_GUI_MODEL:
praxis run ./goal-state.json --nova /absolute/path/to/nova
praxis resume ./goal-state.json --nova /absolute/path/to/nova
```

Restart with the same checkpoint to resume. A crash during an in-flight action blocks until manual reconciliation; never blindly replays. Risky actions pause for explicit approval. Model evidence is **not independent verification**: inject a custom verifier when calling `run()` for reliable artifact checks. Existing checkpoint v1 remains readable. OpenAS integration scripts remain in the separate OpenAS repository and can delegate to the installed Praxis package. No Windows/macOS end-to-end test has been completed.

## Phase 3: recover safely

Version 2 checkpoints migrate version 1 on write. A process-level lock prevents concurrent runs on the same checkpoint. If a process dies after dispatching a potentially non-idempotent action, the next run becomes blocked. A terminated process may leave a running checkpoint with a pending action; after independently checking the target, reconcile it directly (the checkpoint lock must be available):

```sh
praxis reconcile ./goal-state.json --outcome executed --note "Checked the UI and confirmed the action happened"
praxis resume ./goal-state.json --nova /absolute/path/to/nova
```

`--outcome not-executed` is also supported. Reconciliation never replays an old action: the planner observes again and chooses a new step. This is not crash-proof external transactionality, and it does not automatically restart a process.

## Artifact verification

Pass `--verifier` on the first run and resume to make completion depend on independent local file checks. Rule keys match the checkpoint's criteria, and artifact paths are relative to the configuration's root. The root defaults to the configuration's directory.

| Rule | Checks |
| --- | --- |
| `path` | A regular file inside the verifier root |
| `min_bytes` and `contains` | Minimum size (default 1) and optional UTF-8 substring |
| `fresh: true` | File is new or its SHA-256 differs from the pre-action baseline stored in the checkpoint |
| `json_fields` | A map of JSON Pointers to exact expected values |
| `run_id_pointer` | A JSON Pointer whose value must equal the checkpoint's `id` |

Use an isolated output directory for identical reproducible builds, or have the application record a run ID. An unchanged file with identical bytes fails a freshness check. Resume with the same root and rules after a baseline is captured. For `run_id_pointer`, the application must receive and write the ID returned by `praxis create` or `status`.

```sh
praxis run ./goal-state.json --nova /absolute/path/to/nova --verifier ./coverage-rules.json
praxis verify ./goal-state.json --verifier ./coverage-rules.json
```

`verify` uses no backend or model. It outputs one check per criterion and exits 1 for a failed check. [Legacy text rules](examples/coverage-verifier.json) and [structured coverage rules](examples/coverage-artifact-verifier.json) are available. With no verifier, the compatibility mode uses planner evidence.

## Browser goals

Install the optional browser dependencies and run the local fixture from the repository root. The fixture imitates a coverage GUI and produces a sample report.

```sh
python -m pip install -e 'packages/gui-agent-runtime[browser]'
python -m playwright install chromium
python -m http.server 8080 --bind 127.0.0.1 --directory examples/coverage-web
```

In another shell, configure `OPENAS_GUI_API_KEY` and `OPENAS_GUI_MODEL`, then run:

```sh
work="$(mktemp -d)"
cp examples/coverage-artifact-verifier.json "$work/rules.json"
praxis create "$work/state.json" \
  --goal "Build the instrumented SIL fixture, run cases, wait for success, and download the MC/DC report as coverage.json." \
  --criterion "New instrumented MC/DC report for the passed SIL cases"
praxis run "$work/state.json" --browser-url http://127.0.0.1:8080/ \
  --download-dir "$work" --verifier "$work/rules.json"
praxis verify "$work/state.json" --verifier "$work/rules.json"
```

The backend observes the current page's accessible tree. It offers semantic clicks and fills, bounded text waits, same-origin navigation and explicit downloads. Use authorized test pages and test data. The runtime uses a single Chromium page and an isolated, nonpersistent context. Browser request routing provides a same-origin filter for routed requests. It does not substitute for isolation from untrusted sites. Redirects can affect which requests the browser routes. Downloads require an existing directory, a new filename, and filesystem hard-link support for atomic publication.

Resume with the same URL, download directory, and verifier. Cookies, tabs and in-page form state are recreated. The checkpoint and downloaded artifacts survive. On an uncertain action, check its outcome and use `reconcile` before resuming. `--browser-executable` is available for a locally installed compatible Chromium.

## LiteLLM gateway

Use an existing LiteLLM Proxy or start a local instance with the [example configuration](examples/litellm.yaml). Configure `OPENAI_API_KEY` and a local `LITELLM_MASTER_KEY` in the gateway process, then start the gateway.

```sh
litellm --config examples/litellm.yaml --host 127.0.0.1 --port 4000
```

Set `LITELLM_API_KEY` in the OpenAS process to a key accepted by the gateway. Use a virtual key for a shared gateway. For the local example, use the local instance's master key. Point the runtime at the gateway and use the alias in `model_list`.

```sh
praxis run ./goal-state.json --browser-url http://127.0.0.1:8080/ \
  --litellm-base-url http://127.0.0.1:4000 --model openas-planner

praxis-benchmark examples/live-coverage-suite.json \
  --browser-url http://127.0.0.1:8080/ --litellm-base-url http://127.0.0.1:4000 \
  --model openas-planner
```

The same options work with `resume` and `--nova`. `--litellm-base-url` accepts the gateway URL with an optional `/v1` suffix. You can also set `OPENAS_LITELLM_BASE_URL` and `OPENAS_GUI_MODEL`. Explicit flags select the route if both gateway and direct endpoint variables are configured. `--endpoint` continues to take a full Chat Completions URL and uses `OPENAS_GUI_API_KEY`.

Choose a gateway deployment that supports Chat Completions JSON mode. Desktop tasks that pass screenshots also need image input support. The benchmark records the connection kind and the requested alias. To compare concrete models, use aliases pinned to one deployment each and control the gateway's fallback policy. See [LiteLLM Proxy setup](https://docs.litellm.ai/docs/proxy/docker_quick_start).

## Token-efficient mode

The planner now sends a bounded tool summary (name, short description and argument schema), short UI summaries and a limited number of recent MCP text/image outputs. It does **not** cache schemas across stateless API requests, and it does **not** blindly batch GUI clicks: each action still requires a fresh observation and safety gating.

```sh
praxis run ./goal-state.json --nova /absolute/path/to/nova \
  --max-model-calls 30 --max-model-tokens 50000 \
  --max-ui-text-chars 3000 --max-images 1
```

`--max-model-calls` and `--max-model-tokens` are optional (0 means no limit). If a budget is reached, the agent **pauses** instead of retrying indefinitely. Model usage is saved in checkpoint `model_usage` and carries over to `resume`. When the provider reports `usage.prompt_tokens` and `usage.completion_tokens`, those are tracked as actual usage. If it omits usage, input is roughly estimated by character count, tracked separately, and output tokens are unknown. Token budgets are best-effort guards, not strict billing caps: a single request can overshoot a remaining budget, images have provider-specific costs, and requests that fail before returning usage may be unaccounted for. The model endpoint may bill for attempted requests.

The planner receives a paged tool directory and selected complete schemas; use internal discovery to load tools beyond the current page. For accurate before/after savings, compare `model_usage` across equivalent runs with the same model, UI, and goal. Real Nova Windows/macOS E2E testing is still outstanding.

## Agent Benchmark

```sh
praxis-benchmark examples/benchmark-suite.json \
  --repeat 3 --output ./offline-results.json
praxis-benchmark examples/live-coverage-suite.json \
  --browser-url http://127.0.0.1:8080/ --repeat 3 --output ./browser-results.json
praxis-benchmark examples/live-coverage-suite.json \
  --nova /absolute/path/to/nova --case produce-mcdc-report --output ./nova-results.json
```

Offline mode uses scripted actions, observations and token counts. The supplied suite also interrupts a run after writing its artifact and verifies that reconciliation resumes without another write. Live mode uses the configured model API and the selected backend. The Nova example requires an authorized, resettable desktop fixture with a matching workflow that can export to the directory in the goal. The included web fixture is for the browser command. Each trial starts with a new checkpoint and artifact directory. In a case's `goal`, use `{output_dir}` for that directory. The suite can set `initial_files` to seed stale artifacts, `expected_status` for the final outcome, and `expected_checks` for required Runtime check reasons.

`success_rate` is the fraction of cases matching their expected outcome. `completion_rate` counts independently verified completions among cases intended to finish. Expected rejections pass when their status and required check reasons match. The CLI returns 1 if any case fails. Live reports record calls, time, provider-reported tokens, missing usage and estimates. Scripted token counts serve only as fixtures.

Add `--experience-store PATH --experience-environment TAG` to the live command for paired baseline and experience-enabled trials. Keep the same application and model, make sure the store has relevant verified experiences, and reset any application state between trials. Trials report each variant and the number of model calls that received experience hints. Use isolated trial data and an application that resets between invocations. You can run individual cases with `--case`. Windows/macOS plus Nova verification requires a real interactive desktop.

### Tool trajectories

Add `expected_trajectory` to a case to check which adapter calls occurred. For a browser-specific suite, the example requires two clicks before the download and permits an intermediate wait.

```json
{
  "expected_trajectory": {
    "match": "in_order",
    "tools": ["browser_click", "browser_click", "browser_download"],
    "max_counts": {"browser_download": 1}
  }
}
```

| Rule | Checks |
| --- | --- |
| `match: "exact"` | The complete attempted sequence equals `tools`, and every call returned success or was reconciled as executed |
| `match: "in_order"` | The executed sequence contains `tools` in order, including repeated names. Additional calls are permitted |
| `match: "any_order"` | The executed sequence contains the required count of each item in `tools` |
| `forbidden`, `max_counts` | Blocks specified names or excessive attempts, including calls that returned errors |

Trajectory and artifact checks both affect `success_rate`. Reports keep artifact completion and `trajectory_pass_rate` visible individually. A missing, late, truncated, or unresolved trace fails a trajectory check. Use artifact rules for business results and for properties that depend on tool arguments.

For standalone runs, add `--trace-tools` to `praxis run` and inspect `tool_trace` with `praxis status`. It records advertised tool names, adapter outcomes and duration locally, with a maximum of 128 retained events. It stays enabled on resume. An interrupted call remains unknown until the operator reconciles it. The trace omits tool arguments, results, screenshots and model messages. Duration covers the adapter call only.

### Reproducible evaluation

The report's `provenance` object identifies the selected suite and Runtime source by SHA-256. It also records the Runtime package version when installed, Python and OS, repeat count, and live model budgets. Playwright and browser versions are recorded for browser trials. Nova records the version advertised by MCP when available. The existing model field names the provider configuration's requested model. Run comparisons with matching suite fingerprints and controlled application state, and retain both reports when changing the Runtime. Fingerprints document inputs and source identity. Application state and model provider behavior still need to be controlled during evaluation.

#### Upstream methods

- Tool sequence matching from [Google ADK trajectory evaluation](https://github.com/google/adk-python/blob/90c3a29412d33e4221fa998600d763173eee6349/src/google/adk/evaluation/trajectory_evaluator.py).
- Tool spans and sensitive-data handling from [OpenAI Agents SDK tracing](https://github.com/openai/openai-agents-python/blob/9e05bd5bf299a5ddbb25f7fec8da10bf2baf6133/docs/tracing.md).
- Study version metadata from [ServiceNow AgentLab](https://github.com/ServiceNow/AgentLab/blob/8488c564eea0525aaff5dea189bb03a9b529546e/README.md#reproducibility-features).

## Engineering feedback

Compare two checkpoints recorded with `--trace-tools` to locate the first visible semantic difference.

```sh
praxis compare-traces ./passing.json ./failing.json
```

The comparison checks tool names and outcomes first, then artifact status and final Runtime status. It ignores elapsed time. An incomplete or unresolved trace yields `inconclusive`.

From the repository root, check whether regressions catch the removal of selected Runtime guards or reduce a JSON array that reproduces a local failure.

```sh
python scripts/check_runtime_mutations.py
python scripts/minimize_repro.py events.json --output reduced.json \
  --failure-marker TARGET_FAILURE -- python check_events.py '{input}'
```

Use a deterministic local check. The reducer looks for the configured exit status and optional marker, retains input order, and refuses overwriting the output file. See the [engineering methods](https://github.com/xiangguangxin/OpenAS/blob/main/plugins/openas/references/engineering-methods.en.md) and [model gateway ADR](https://github.com/xiangguangxin/OpenAS/blob/main/docs/adr/0001-runtime-model-gateway.md).

## Experience Memory (first stage)

Experiences are **explicitly curated** from completed checkpoints only after a trusted local file verifier passes again. They contain a short goal, criteria, environment tag and human note; **no raw screenshots, model prompts, tool payloads, credentials or action replay scripts**. Search returns hints, never commands for automatic execution.

```sh
praxis-experience --store ./experiences.json add ./goal-state.json \
  --verifier ./coverage-verifier.json --environment win11-coverage-v1 \
  --note "Coverage report independently inspected"
praxis-experience --store ./experiences.json search "coverage report" \
  --environment win11-coverage-v1
```

Only use verifier configurations you trust. The store is a local file with process-serialized writes but no access control or encryption; keep it in a trusted directory and do not share it with untrusted users. Environment tags are exact matches and retrieval is simple word overlap (not embeddings). By default experiences are **not injected into the planner**, and success in one environment does not authorize replay in another.

## Opt-in experience-assisted planning and lifecycle

```sh
praxis run ./goal-state.json --nova /absolute/path/to/nova \
  --experience-store ./experiences.json --experience-environment win11-coverage-v1
```

When both flags are supplied, the planner automatically searches the **local** store for up to two relevant, independently verified experiences in the exact environment. Hints are bounded and explicitly marked as **untrusted historical context**; the model must reobserve and independently verify current outcomes. No actions are replayed from memory. Omitting the flags disables retrieval.

Experience confidence decays exponentially (default 30-day half-life), and experiences become ineligible after 90 days. Search automatically excludes expired, invalidated, or low-confidence records; `expire` persists their invalidated status without deleting audit history. These are conservative policy defaults, not measured reliability probabilities. Two **explicitly recorded, independently verified** failures invalidate an experience; runtime planner errors alone do not count as verified failures.

```sh
praxis-experience --store ./experiences.json feedback EXPERIENCE_ID \
  --outcome failure --note "Independent acceptance failed in matching environment"
praxis-experience --store ./experiences.json invalidate EXPERIENCE_ID \
  --reason "GUI version changed"
praxis-experience --store ./experiences.json expire
```

The CLI cannot independently establish that feedback is true: operators must check their evidence before submitting feedback. Environment tags should include relevant OS, app and workflow versions. The store remains local JSON; process locks serialize writers on one host. Hints may include operator notes, so do not store secrets. This is a first-stage retrieval/lifecycle implementation, not autonomous experience learning or real-world success-rate proof.


## Reviewable approval and action policy

The host classifies both tool names and locator/command arguments. Generic clicks
on controls such as `Send`, `Delete account`, `确认付款` and `发送消息` require review
regardless of the planner's low-impact classification. Executable payloads and
ambiguous submission shortcuts also require review. Heuristics cannot determine
all effects of arbitrary controls: use an operator-owned policy for critical workflows.

```bash
praxis run goal.json --browser-url https://example.com --policy examples/action-policy.json
praxis status goal.json
praxis resume goal.json --browser-url https://example.com --policy examples/action-policy.json \
  --approve-pending --approval-id FINGERPRINT_FROM_PENDING_REVIEW
```

A policy rule matches one tool and its **entire** argument object. Extra arguments
or another target do not match. Effects are `allow`, `approve` and `deny`; deny wins
over conflicting matches. Policies bind to the checkpoint and cannot silently
change on resume. An allow is explicit operator authorization, so use exact test
fixture targets instead of broad production actions.

`pending_review` contains the tool, bounded target locators, reason, request ID,
context fingerprint and a ten-minute expiry. Text values, executable payloads and
credentials are redacted; URL credentials, queries and fragments are omitted.
Raw executable arguments are not stored. After approval the planner reobserves and
reproposes an action. Only an identical action in the same observed view/backend
can consume the grant, and only on the immediately following decision. A changed
view or action produces a new request. Dynamic pages may therefore require a new
review. Legacy hash-only approvals require a new review as well.

## Completion, diagnostics and recovery

| `verification_level` | Meaning |
| --- | --- |
| `unverified` | No successful completion check |
| `model_evidence` | Model-supplied criterion strings; not independent proof |
| `independent_artifact` | Bound local file verifier passed; checks and identity recorded |
| `custom_verifier` | Caller-supplied verifier returned true; independence is caller-defined |
| `legacy_unspecified` | Older completed checkpoint has no verification provenance |

A final read-only observation and artifact check remain available after the last
allowed action. No additional modifying action is permitted. With model-only
verification, one final completion decision is permitted and still consumes the
configured model budget. File-verifier identity binds even without freshness rules.

Execution commands (`run`, `resume`, `demo`) use exit codes 0 completed, 3 paused,
4 blocked, 5 failed. Invalid CLI/configuration uses 2. `verify` uses 0/1 for
pass/fail; `doctor` uses 0/1 for healthy/unhealthy. Ctrl+C exits with 130 after persisting interruption state. `status`, `retry` and `reconcile`
report their operation's result without treating a paused checkpoint as a CLI error.

`last_error` records a stable code, phase, possible action dispatch and recovery
instruction. Raw exceptions/provider messages are not checkpointed. Recent actions
retain up to eight known tool names and outcomes, without raw arguments or UI trees.
Three identical actions on an unchanged observed view pause with `no_progress`.

```bash
# After correcting a known-outcome failure, explicitly reopen the goal.
praxis retry goal.json --note "Provider connectivity restored"
praxis resume goal.json --nova /absolute/path/to/nova

# Extend an exhausted action budget without resetting the action counter.
praxis retry goal.json --max-steps 60 --note "Authorized remaining work"
```

`retry` refuses in-flight uncertain actions, pending approvals and denied goals.
Use `reconcile` only after independently checking an uncertain action's outcome.

## Tool discovery and observation feedback

The model receives a paged tool directory (24 entries per page) and at most six
selected complete schemas. It may choose `{"type":"discover","query":"download",
"offset":0,"tools":["actual_advertised_tool"]}` to load any advertised tool,
including tools after position 12. Discovery is internal and never dispatches a
backend action. Four consecutive discovery requests are allowed before pausing.
Schemas too large for the context limit are explicitly marked unavailable. The
planner must pause for assistance rather than invent parameters. Context shrinking
operates on whole fields before JSON serialization and preserves goal/criteria.

Nova automatically refreshes observations only using advertised observation tools
with `readOnlyHint=true` and no required arguments. Backends without such a tool
retain the explicit model-driven observation path. Screenshot/tool content remains
transient. Nova tool-list pagination is supported. Browser observations use the
current accessibility tree, and recent tool outcomes return to the planner.

## Curated experience explanations

```bash
praxis-experience --store experiences.json add goal.json --verifier rules.json \
  --environment win11-app-v1 --note "Output independently inspected" \
  --precondition "Instrumented project is loaded" \
  --key-step "Build before running SIL cases" \
  --failure-mode "Wait for test completion before downloading"
```

Each repeated note option accepts at most eight entries of 200 characters. Store
records include the checkpoint ID, verifier identity, check time, count and available
artifact hashes. These references explain provenance; they do not authorize replay.
Preconditions are hints, not automatically established truths. Existing version-1
stores remain readable. Writers acquire a process lock, preventing lost updates
between concurrent local writers; this does not provide distributed locking.

For readiness checks, complete examples, CI artifacts and separately labelled
real-provider acceptance, see [the acceptance guide](docs/ACCEPTANCE.md).


## Dispatch contracts and process interruption

Tool actions are validated against the complete advertised JSON Schema before
approval evaluation, tracing and write-ahead dispatch. Unknown or duplicate tool
names, missing/extra arguments, invalid types/enums and unsupported/unresolvable
schemas never reach the backend. Schema references resolve locally only; remote
`$ref` resources are not fetched. `jsonschema` is now a runtime dependency.
Format annotations without assertion semantics are not an additional authorization
or sandbox boundary. Custom dictionary adapters that omit a `tools` directory
retain the legacy contract and have no advertised schema to check.

Rejected actions produce a payload-free validation error. The planner can correct
the action without spending an action step. Three consecutive invalid decisions
block for operator diagnosis. Operator policy equality preserves JSON types:
`true` does not match `1`, and the policy copies supplied rules on construction.

The CLI uses `run_session()` to hold the checkpoint lock and validate backend,
policy, verifier and approval bindings **before** launching Nova or opening a
browser. Terminal, pending-approval and uncertain-action checkpoints do not start
a new session merely to return their status. Startup errors are recorded as
known-outcome session errors. Library callers with an already-open adapter can
continue using `run()`.

Ctrl+C before dispatch pauses the goal. During a dispatched call it immediately
records `blocked` plus the uncertain pending action. No interrupted action is
replayed automatically. Hard termination may leave `running` on disk; `reconcile`
accepts that state when there is a pending action and it can acquire the lock.
It refuses reconciliation while another cooperating executor holds the lock.

## Durable publication and transient evidence

Checkpoint creation publishes a temporary file without replacing an existing
path, including a file created concurrently after the initial existence check.
Checkpoint and experience writes reject NaN/Infinity, flush and fsync the file,
and atomically publish it. On POSIX, parent directory entries are fsynced as well;
Windows uses atomic publication and file fsync without a directory-fsync claim.
Actual power-loss guarantees still depend on the filesystem and operating system.

Nova manual observation results are now part of the next normalized observation,
so their content contributes to approval/no-progress view identities. An empty
result after another action clears the previous screenshot. Automatic fresh
observations keep bounded previous-action text separately; old result images do
not replace a fresh screenshot. These payloads remain transient. EOF wakes a
pending MCP request immediately, and cleanup is safe after a child exits.

Browser resume still creates an isolated context: it does not restore cookies,
form fields or a previously open tab. The checkpoint records execution progress,
not a full browser-session snapshot. Reobserve and verify the application state
on resume; nontrivial stateful browser recovery and native Nova acceptance need
separate real-backend validation.


## Integrated planning and model routing

When using the standard CLI, ModelPlanner proposes a DAG before its first GUI action. If an independent `FileVerifier` or `CriteriaVerifier` is configured and each task maps its acceptance entries to the goal's exact criteria (covering all criteria), the GUI loop enforces tasks in dependency order. The model uses `task_complete` to request completion; the configured verifier, not model text, decides whether the task passes. Failed checks are retried within a bounded per-task budget, and exhausted failures block dependents. Task status and per-task fresh-artifact baselines are stored in the goal checkpoint. Plans without a valid acceptance mapping, or runs without an independent verifier, remain advisory for backward compatibility. Existing checkpoints without a plan acquire one on their next run.

Pass `--model-profiles profiles.json` to enable capability-aware routing. Each profile declares `name`, `model`, `endpoint`, `api_key_env`, `capabilities` and optional `max_cost_tier`. A screenshot request requires a profile with `vision`; a failed action prefers a higher cost tier. API keys are read from the named environment variables, not from the JSON file.

## Local scheduled worker

```sh
praxis schedule --database ./jobs.sqlite --goal "Inspect report" --criterion "Report verified"
praxis worker --database ./jobs.sqlite --checkpoint-dir ./checkpoints --max-jobs 1 --nova /path/to/nova
```

The worker claims due jobs atomically and runs at most the configured count before exiting. Each claim receives an owner and an unguessable lease token. A heartbeat thread renews the lease while the injected executor is active; completion/failure is accepted only from the current unexpired owner/token pair. A stale worker cannot overwrite the result after another worker reclaims the job. Expired jobs are requeued while attempts remain and become failed after the configured limit. Explicit `praxis retry` resets the attempt budget. Worker execution requires the same model credentials and optional verifier configuration as a direct run.

Configure the lease when starting the worker:

```sh
praxis worker --database ./jobs.sqlite --checkpoint-dir ./checkpoints \\
  --max-jobs 2 --lease-seconds 90 --heartbeat-interval 20 --nova /path/to/nova
```

The SQLite queue supports multiple cooperating local processes sharing the same database file. It is not a distributed queue across hosts or a substitute for the goal checkpoint's action reconciliation. If a worker dies during an uncertain GUI action, the recovered job resumes from its checkpoint; Praxis still blocks and requires explicit reconciliation rather than replaying that action.


## Phase 5: autonomous DAG orchestration

The reusable `planning` package provides a checkpointed DAG orchestrator for workflows that need independently verified subtasks:

- Each node has its own ID, goal, dependencies, observable acceptance checks, status, attempt count and verification evidence.
- Only pending nodes whose dependencies are independently verified as completed can start.
- The verifier callback is invoked separately for every node. A passing model statement alone is not an independent verifier; use a trusted artifact or domain-specific verifier.
- Failed nodes retry only within the configured attempt budget. Exhausted failures block descendants while independent branches can continue.
- A running node found after process interruption becomes blocked and requires explicit reconciliation. The orchestrator never blindly replays an interrupted node.
- Pending nodes can be added or removed, and unfinished work can be replaced by a newly proposed DAG. Completed nodes and their definitions are immutable across replans.
- Independent ready nodes can execute concurrently with a bounded thread pool. Tasks declaring the same resource name are serialized; resource locks are shared across processes through lock files.
- Each task result and verifier evidence is aggregated in the orchestration snapshot, including status counts and attempt totals.
- The orchestrator checkpoint is process-locked during run, mutation and recovery, preventing two orchestrators from dispatching the same plan concurrently.
- Every transition, retry, verification, mutation and replan is recorded in the atomic orchestration checkpoint.

Library example:

```python
from pathlib import Path
from praxis_agent.planning import Task, TaskGraph, TaskOrchestrator, DynamicTaskPlanner

graph = TaskGraph([
    Task("build", "Build the instrumented application", (),
         ("Fresh binary exists", "Build log has no errors"), ("build-output",)),
    Task("test", "Run the SIL suite", ("build",),
         ("All required SIL cases passed",), ("test-environment",)),
    Task("report", "Export the MC/DC report", ("test",),
         ("New report exists and parses",), ("report-output",)),
])

orchestrator = TaskOrchestrator(
    graph,
    execute=execute_task,       # Your authorized task adapter
    verify=verify_task,         # Independent verifier for this task
    checkpoint=Path("workflow.json"),
    max_attempts=2,
    max_workers=3,
)
result = orchestrator.run(max_tasks=10, max_workers=3)

# After a pause, load the exact checkpoint and resume.
orchestrator = TaskOrchestrator.resume(
    "workflow.json", execute_task, verify_task
)

# If the workflow changes, replace only unfinished work.
planner = DynamicTaskPlanner(propose_task_dag)
orchestrator.replan_with(
    planner, goal, criteria, context=latest_observation,
    reason="The original report export path is unavailable",
)
```

The parallel `TaskOrchestrator` remains a library-level orchestration primitive with its own checkpoint and injected task adapter. The GUI `praxis run` loop uses the integrated `TaskPlanRuntime` serially for one shared desktop session; it does not dispatch concurrent clicks against the same live desktop. Per-task GUI acceptance still requires an independent verifier.


### GUI task-DAG execution (Phase 5.2)

The standard `praxis run` / `praxis resume` loop now supports verified task execution without bypassing the existing GUI safety pipeline:

1. The model proposes a DAG whose task acceptance entries are exact strings from the Goal criteria. Every Goal criterion must be covered.
2. With an independent `--verifier` configuration, Praxis activates one ready task at a time and includes its goal, acceptance criteria, and attempt count in the next model context.
3. The model performs normal observed GUI actions. It may request `task_complete`, but that request is not proof.
4. Praxis evaluates only that task's configured verifier rules. File verification captures a separate fresh-artifact baseline at task start. Only a passing independent check unlocks dependent tasks.
5. Failed checks trigger a bounded task retry. Exhausted failures block dependent tasks while independent branches remain eligible.
6. Once all tasks pass, the ordinary Goal-level verifier runs again before the Goal is marked completed.

The task lifecycle is checkpointed in the same Goal file, so a paused run resumes the active task instead of starting the DAG over. A run with no independent verifier, or a legacy plan that does not map its acceptance entries to the Goal criteria, remains advisory rather than claiming verified orchestration. Model-generated plans never bypass action schema validation, operator policy, approval gates, write-ahead action records, or uncertain-action reconciliation.

Task acceptance entries currently reuse the Goal's configured criterion names; separate task-specific verifier-rule files and automatic model-assisted replanning of a failed DAG remain future work.


## Worker leases and crash recovery (Phase 5.3)

The SQLite scheduler migrates older job databases in place by adding lease metadata. The lifecycle is:

- `queued → running`: atomic claim under `BEGIN IMMEDIATE`, incrementing attempts and issuing a fresh lease token.
- `running → running`: owner/token-bound heartbeat extends `lease_expires_at`.
- `running → completed/failed`: only the current, unexpired lease owner/token may publish the terminal result.
- Expired `running` jobs return to `queued` while attempts remain; otherwise they become `failed`.
- Explicit retry of a failed job clears prior results and resets its attempt budget.

The worker renews leases on a daemon heartbeat thread while the injected job executor runs. Lease loss prevents that worker from publishing a result. The underlying GUI goal checkpoint lock and write-ahead action recovery remain the authority for safe resume; queue lease recovery never implies that an uncertain GUI action is safe to replay.


### Parallel task execution and resource locks (Phase 5.4)

The library-level `TaskOrchestrator` can execute independent DAG branches concurrently:

- Set `max_workers` to a value from 1 to 32. The default remains 1 for backward compatibility.
- Add `resources=("desktop:fixture", "output:/reports")` to a task to declare shared mutable resources. Tasks in the same DAG that share any resource are never scheduled in the same concurrent batch.
- A `TaskLockManager` also acquires cross-process file locks for declared resources. The default lock directory is `.praxis-resource-locks` beside the task checkpoint; use the same explicit `resource_lock_dir` when separate orchestrators must coordinate across different checkpoint directories.
- Resource names are opaque coordination keys, not access-control policies. They do not replace the GUI action policy, backend permissions, or filesystem sandboxing.
- The injected `execute(task)` callback defines how a task is delegated. It can route work to separate model/backend instances or a worker pool; the orchestrator does not silently create additional model sessions.
- The GUI `TaskPlanRuntime` remains single-task-at-a-time for one shared desktop session. Parallelism is intended for independent task adapters and separate execution contexts, not concurrent clicks in one desktop session.

A snapshot includes an `aggregate` object with total tasks, counts by status and total attempts, alongside each task's result and verification evidence. The plan checkpoint is locked while it runs or is mutated; after a process interruption, every node recorded as running becomes blocked and requires explicit reconciliation.


## Phase 6: evidence-gated self-evolution

Praxis separates **experience memory** from **strategy promotion**. A completed, independently file-verified run may be curated into the existing ExperienceStore. A strategy proposal must cite an active verified experience, is stored as an inert candidate, and is never sent to the planner unless explicitly selected for a paired candidate evaluation. Normal runs retrieve only promoted strategies.

### Propose and evaluate a strategy

```sh
# Curate a source run first (requires independent file verification).
praxis-experience --store ./experiences.json add ./goal-state.json \
  --verifier ./rules.json --environment win11-app-v1 \
  --note "Artifact independently checked"

# Create an inert strategy candidate grounded in that experience.
praxis-evolution --store ./evolution.json propose \
  --experience-store ./experiences.json \
  --source-experience-id <experience-id> \
  --goal "Generate the coverage report" --environment win11-app-v1 \
  --instruction "Check the build summary before opening the report." \
  --rationale "The verified run consistently exposed build status first."

# Run paired baseline and candidate variants against the same live suite.
praxis-benchmark ./suite.json --browser-url http://127.0.0.1:8080/ \
  --repeat 10 --evolution-store ./evolution.json \
  --evolution-environment win11-app-v1 --strategy-candidate-id <strategy-id> \
  --allow-failures --output ./paired-evaluation.json

# Evaluate, then explicitly promote only if the evidence gate passes.
praxis-evolution --store ./evolution.json evaluate <strategy-id> \
  --report ./paired-evaluation.json
praxis-evolution --store ./evolution.json promote <strategy-id> \
  --experience-store ./experiences.json
```

The paired evaluator requires identical scenario/iteration keys and matching provenance (suite hash, runtime source hash/version, Python/OS/architecture, repeat count, model, connection kind and model budget), at least 10 paired runs, at least 10 percentage points of success-rate improvement, more paired wins than losses, no regression in independently verified completions, and no net regression in recorded trajectories. A strategy that fails any gate remains a candidate and cannot be promoted. These conservative thresholds are an initial engineering gate, not a statistical proof of generalization; use larger, held-out suites before production promotion.

After promotion, normal runs may retrieve matching strategies as bounded, **untrusted planning hints**:

```sh
praxis run ./goal-state.json --browser-url http://127.0.0.1:8080/ \
  --evolution-store ./evolution.json --evolution-environment win11-app-v1
```

A strategy never grants tool permissions, bypasses action validation, or supplies evidence of completion. To remove a strategy from retrieval, explicitly roll it back:

```sh
praxis-evolution --store ./evolution.json rollback <strategy-id> \
  --reason "Target application changed"
praxis-evolution --store ./evolution.json list --status promoted
```

Insight-grounded candidate generation is also available after producing the deterministic learning reports. Mine repeated failures and cluster active verified experiences first, then explicitly select one cluster, one repeated failure pattern and a source experience that belongs to that cluster:

```sh
praxis-learning mine-failures --checkpoints ./checkpoints --min-occurrences 2 \
  --output ./reports/failure-patterns.json
praxis-learning cluster-experiences --store ./experiences.json \
  --environment win11-app-v1 --min-similarity 0.35 \
  --output ./reports/experience-clusters.json
praxis-evolution --store ./evolution.json generate-from-insights \
  --experience-store ./experiences.json \
  --cluster-report ./reports/experience-clusters.json \
  --failure-report ./reports/failure-patterns.json \
  --cluster-id <cluster-id> --pattern-id <pattern-id> \
  --source-experience-id <verified-experience-id>
```

Generation requires at least two observations for the failure signature and at least two active, independently verified cluster members in the same environment. It only reuses operator-curated steps shared by every cluster member; recovery guidance is selected from fixed runtime policy text, not arbitrary report prose. The resulting strategy stores its cluster/pattern provenance and is idempotently deduplicated. It remains an inert candidate and must pass the same paired benchmark gate above before promotion.

Candidates are not auto-generated from model self-critique. Operators must ground proposals in verified experiences and explicitly promote after paired evaluation. The evolution store records report hashes, decision metrics, promotion time and rollback reason. No source code is self-modified.


### Phase 6.2: Failure diagnosis and bounded plan repair

The GUI runtime now classifies failures from host-owned signals before asking the planner to recover. The deterministic taxonomy includes uncertain in-flight actions, operator-policy denial, task/artifact verification failure, transient backend failure, ordinary action failure and exhausted budgets. The diagnosis contains only allowlisted codes, phases and failed-check statuses; it does not copy raw page text, model explanations or artifact contents into the recovery prompt.

For a task-level independent-verification failure, the planner may propose a replacement DAG for unfinished work (maximum three revisions per Goal). The runtime validates the replacement against the original Goal criteria, preserves completed task definitions and results, and resets only the unfinished graph. A failed replan leaves the prior checkpointed graph intact and falls back to its existing bounded retry behavior.

Important boundaries:

- An action with uncertain external outcome is never automatically replanned or replayed; reconciliation remains mandatory.
- A policy denial is not treated as a recoverable planner error and cannot be bypassed by replanning.
- The model proposes a new DAG, but local graph validation and independent per-task verification decide whether it is accepted.
- Completed tasks are immutable during repair. Every replacement task must use exact Goal criterion strings, and the new DAG must cover all Goal criteria.
- Action failures are diagnosed and exposed to the next planning decision; the runtime does not assume that a model-generated explanation is the cause.


### Phase 6.3: Task-aware model routing

Model profiles may now declare optional `task_types` specializations. The planner classifies the active task using deterministic goal text signals (debugging, visual inspection, code/build, verification, research, execution or planning) and routes the request to a compatible specialized profile. Capability requirements (chat/vision), risk preference and cost tier remain enforced. If no specialized profile matches, the router falls back to a generic profile; older profile files without `task_types` remain valid.

Example configuration: [examples/model-profiles-task-aware.json](examples/model-profiles-task-aware.json).

```sh
praxis run ./goal-state.json --browser-url http://127.0.0.1:8080/ \
  --model-profiles ./examples/model-profiles-task-aware.json
```

Set each profile's API key environment variable before running. The example model names and endpoint are placeholders. Route counts are recorded in model usage so benchmark runs can show which task types used each route. Task classification is a routing hint, not an authorization decision; every route still obeys capability, budget and action-policy checks.


### Phase 6.4: Verified strategy extraction and drift guard

Operator-curated `key_steps` and `preconditions` on an independently verified ExperienceStore record can now be converted into an inert strategy candidate:

```sh
praxis-evolution --store ./evolution.json extract \
  --experience-store ./experiences.json --experience-id <experience-id>
```

Extraction does not infer hidden tool arguments or replay raw traces. It turns the operator-reviewed sequence into a bounded natural-language hint, and the resulting candidate still needs paired benchmark evaluation and explicit promotion.

Promoted strategies can be reassessed against a fresh paired benchmark report:

```sh
praxis-evolution --store ./evolution.json reassess <strategy-id> \
  --report ./fresh-paired-evaluation.json
```

If a promoted strategy fails the same paired evaluation gate, it is automatically suspended and disappears from planner retrieval. A passing reassessment refreshes its evaluation evidence. Suspended strategies can be rolled back for audit, but are never silently reactivated; a new candidate and promotion decision are required.
