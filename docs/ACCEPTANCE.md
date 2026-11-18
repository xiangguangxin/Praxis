# Reproducible acceptance

Praxis reports three distinct kinds of execution. Keep their results separate.

| Level | Backend | Planner | What it establishes |
| --- | --- | --- | --- |
| Unit / synthetic | Fake adapters | Deterministic | Runtime invariants and injected failure handling |
| Browser integration | Real Chromium | Scripted local endpoint or fixture state machine | Browser transport, downloads, CLI and independent artifact checks |
| Live acceptance | Real Chromium or authorized Nova desktop | Real configured model provider | Observed performance of the named model, fixture and backend combination |

## Run the browser demonstration

```bash
python -m pip install -e ".[browser]"
python -m playwright install chromium
praxis doctor --browser
praxis demo --output ./demo-run
```

Use a new output directory on each run. With a system Chromium installation, add
`--browser-executable /absolute/path/to/chromium` to both commands.

Open `demo-run/index.html` to review the actual final screenshot, `coverage.json`
and `report.json`. The fixture planner chooses from observed UI states. It does
not contact a model provider. A successful run has:

```json
{
  "kind": "deterministic-browser-demo",
  "real_model": false,
  "status": "completed",
  "verification_level": "independent_artifact"
}
```

CI runs this demonstration and uploads the complete output as the
`deterministic-browser-demo` artifact. This output is separate from real-model
acceptance reports and is not a model success-rate measurement.

## Automatic browser integration

```bash
OPENAS_TEST_BROWSER=1 python -m unittest discover -s tests -p test_browser_goal.py -v
```

The historical environment variable remains supported. On Windows PowerShell,
set `$env:OPENAS_TEST_BROWSER = "1"` before running the Python command.
`OPENAS_TEST_BROWSER_EXECUTABLE` selects a system Chromium executable. The optional
LiteLLM Proxy integration test additionally needs `OPENAS_TEST_LITELLM_EXECUTABLE`.

The default CI matrix runs Python 3.10 and 3.12 on Linux, Windows and macOS. A
separate Ubuntu job installs Chromium and enables browser integration tests.
Cross-platform unit checks do not establish native desktop E2E reliability.

## Real provider and desktop acceptance

The **Live model acceptance** workflow is manual and never runs on pull requests.
Configure repository variables `PRAXIS_MODEL`, `PRAXIS_API_URL` and the secret
`PRAXIS_API_KEY`. Select `browser` for the included local fixture. The benchmark
records the requested model, source/suite fingerprints, backend version, token
usage, status, independent checks and verification level.

For Nova, prepare an operator-managed Windows/macOS runner labelled
`praxis-desktop`, configure `NOVA_EXECUTABLE` and `NOVA_SUITE` as absolute paths,
and authorize Nova on that machine. `NOVA_SUITE` must refer to an operator-owned,
resettable application fixture with declarative artifact rules. Select `nova`
only after that setup. The repository does not assume that the HTML fixture
represents a native desktop application. Each trial must reset the application
or use a fixture that returns to its initial state after producing an artifact.

Live tests consume provider quota. No real-model or Nova desktop success is
claimed merely because the workflow exists or browser integration passes.
