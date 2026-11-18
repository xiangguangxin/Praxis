# Validation record

Recorded on 2026-11-18. These results describe the named fixtures and environments,
not a general desktop autonomy success rate.

[CI run 44266745141](https://github.com/xiangguangxin/Praxis/actions/runs/44266745141)
passed all seven jobs:

| Check | Result |
| --- | --- |
| Linux, Python 3.10 / 3.12 | Passed |
| Windows, Python 3.10 / 3.12 | Passed |
| macOS, Python 3.10 / 3.12 | Passed |
| Chromium integration, scripted local model | Four tests passed; optional LiteLLM Proxy test skipped |
| Targeted mutation checks | Six guards removed in isolation; all six regressions detected |
| Reproducible browser demonstration | Completed with independent artifact verification |

The actual demonstration output included:

```json
{
  "kind": "deterministic-browser-demo",
  "planner": "scripted-fixture-state-machine",
  "real_model": false,
  "status": "completed",
  "verification_level": "independent_artifact",
  "actions": 4,
  "browser_version": "159.0.8154.6",
  "playwright_version": "1.65.2"
}
```

[Download the recorded demonstration artifact](https://github.com/xiangguangxin/Praxis/actions/runs/44266745141/artifacts/14410108353)
for the screenshot, checkpoint, fixture-generated coverage JSON, verification rules,
report and HTML result page. The coverage JSON is fixture data; no real SIL engine
or production MC/DC measurement is implied.

The local execution environment could not launch Chromium because its socket
permissions prevented browser startup. Browser results above come from the actual
GitHub-hosted runner. Local synthetic benchmarking also passed 12 trials (four
cases repeated three times), including trajectory and interruption/reconciliation
checks. Synthetic results are kept separate from browser integration.

Real model-provider acceptance, optional LiteLLM Proxy acceptance and native Nova
Windows/macOS E2E acceptance were **not run**. The manual acceptance workflow and
[setup guide](ACCEPTANCE.md) describe their required credentials, backends and fixtures.

Subsequent PR checks validate the current head; this dated record retains the
specific original run and artifact instead of silently relabelling them.
