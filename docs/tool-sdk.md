# Configured tools and Python SDK

Use the normal execution loop with an operator-owned tool configuration:

```bash
praxis run goal.json --tool-config examples/tool-backend.json --verifier verifier.json
praxis resume goal.json --tool-config examples/tool-backend.json --verifier verifier.json
```

The same `--tool-config` backend is available to the local worker. Model credentials
are still required by the CLI model planner. Custom Python planners do not require
a model endpoint. A complete, independently verified deterministic example runs with:

```bash
python examples/sdk_demo.py --output ./sdk-demo-run
praxis timeline sdk-demo-run/goal.json --output sdk-demo-run/timeline.html
```

This fixture produces an actual JSON artifact through the SDK and verifies its
freshness and contents. Its exact operator policy authorizes only that fixture
write. It is not a real-model performance claim.

## Configuration

Paths resolve relative to the configuration's directory. All tool types can be
combined in one session:

```json
{
  "root": ".",
  "files": {"read": true, "write_json": true},
  "http": {
    "allowed_origins": ["https://api.example"],
    "tools": [{"name":"read_status","method":"GET","url":"https://api.example/status",
               "headers_env":{"Authorization":"APP_AUTH_HEADER"}}]
  },
  "commands": [{"name":"run_tests","argv":["python","-m","unittest","discover","-s","tests"],"timeout_seconds":60}],
  "mcp": [{"name":"reports","command":["/absolute/path/to/mcp-server","--stdio"],
           "tools":["read_report","create_report"],"readonly_tools":["read_report"]}]
}
```

| Tool type | Behavior |
| --- | --- |
| Files | Root-constrained UTF-8 reads and create-only JSON writes, up to 1 MB. No overwrite/delete tool. |
| HTTP | Static allowlisted origin/endpoint; GET arguments become scalar query values, other methods use JSON bodies. No redirects. Credentials come from named environment variables. |
| Commands | Operator-fixed argv, no shell, no model-supplied command arguments; explicit timeout up to 300 seconds and bounded returned output. |
| MCP | Explicit stdio command and per-server tool allowlist; names are `mcp.NAMESPACE.TOOL`. No automatic observation calls. Read-only declarations come from the operator. |

HTTP tools can specify an `inputSchema` for their arguments; the default accepts
an empty object only. Network reads have a 15-second timeout and 1 MB response
limit. Headers supplied through `headers_env` contain the complete value, such as
`Bearer ...`; they are not copied into observations or checkpoints.

Host tool declarations determine impact. Writes, commands and non-read-only MCP
tools require approval or an exact operator allow rule even if a model labels them
low impact. Argument schemas and semantic preflight checks run before write-ahead
dispatch. Normal view-bound approval fingerprints still apply on resume.

Configuration changes are rejected before backend startup. Approval view identity
also includes the advertised schemas and MCP versions. An MCP error after a
potential mutation leaves the action uncertain and requires reconciliation.
Tool idempotence metadata does not enable automatic replay.

These controls scope available operations; this is not a general-purpose security
sandbox. Operator-selected commands and MCP processes run with the operator's host
permissions. Fixed commands must enforce any application-specific constraints.

## Embedding

```python
from pathlib import Path
from praxis_agent import Runtime
from praxis_agent.planner import ModelPlanner
from praxis_agent.verification import FileVerifier

runtime = Runtime.tools(
    Path("tools.json"),
    ModelPlanner("configured-model", "api-key", "https://provider.example/v1/chat/completions"),
    verifier=FileVerifier.from_json(Path("verifier.json")),
)
result = runtime.execute("goal.json", goal="Inspect the report", criteria=["report valid"])
# Resume the same checkpoint:
result = runtime.execute("goal.json")
```

`Runtime` also accepts a custom context-managed backend factory, backend identity,
planner, verifier and policy. Existing checkpoints are never overwritten by goal
creation. Use the same runtime configuration for resume; reconciliation and known
failure retry are available in `praxis_agent.recovery` and the CLI.

For in-process extensions, register a `ToolSpec` with a Python handler, JSON schema
or simple parameter types, declared impact and optional side-effect-free preflight.
`RegisteredToolsBackend.act` blocks unapproved writes. The runtime alone invokes
`act_authorized` after its approval gates. Direct SDK callers are trusted Python
code, not a separate permission boundary. Custom handlers execute synchronously
and must enforce their own timeout; ToolSpec timeout metadata cannot kill Python
callbacks. File/HTTP/command backends apply the concrete limits described above.
