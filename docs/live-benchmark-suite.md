# Real-model acceptance suite

`examples/live-acceptance-suite.json` contains twelve independent, resettable
browser tasks: text entry, Chinese input, numeric calculation, whitespace cleanup,
uppercase conversion, consent, region choice, a two-step workflow, result selection,
asynchronous readiness, transient application failure, and validation recovery.
Each fresh browser context resets its task. Every output must be newly produced
and match both the task identity and expected JSON value.

Start the fixture in one terminal:

```bash
python -m http.server 8765 --bind 127.0.0.1 --directory examples/acceptance-web
```

Then configure your model credentials and run:

```bash
praxis-benchmark examples/live-acceptance-suite.json \
  --browser-url http://127.0.0.1:8765/ --repeat 3 \
  --max-model-calls 40 --max-model-tokens 100000 \
  --policy examples/acceptance-policy.json \
  --allow-failures --output real-model-report.json
```

The manual live-acceptance workflow runs this suite. Reports include runtime and
suite fingerprints, model identity, token usage, independent checks, Wilson 95%
intervals for scenario pass rates, p50/p95 elapsed time and false completion rate
(unverified completions divided by all claimed completions). A scenario pass rate
includes expected negative outcomes; completion rate covers positive scenarios.
Reconciled-run count describes interruption recovery, not automatic recovery rate.

The CI fixture test runs real Chromium with deterministic actions. It validates
the fixture and transport; it does not establish real-model task success rates.
Real-provider runs require credentials and consume quota. No results are claimed
until that workflow or the command has actually executed. Native desktop testing
still requires an operator-owned resettable Nova fixture.

The fixture policy authorizes only the exact local export button and report
download arguments. It does not allow other export targets or filenames.
