# Inspect and replay a run's recorded history

```bash
praxis run goal.json --browser-url http://127.0.0.1:8765/ \
  --trace-tools --journal run-events.jsonl
praxis timeline goal.json --output timeline.html
praxis timeline goal.json --output timeline.json
```

Open the HTML to filter observation, planning, action, verification and recovery
events. Panels show model request batches (including route counts and latency),
task states, tool outcomes and usage. This is a read-only history viewer: opening
or exporting it never calls a model or reexecutes a tool. Choose a new output path;
existing files, including the checkpoint itself, are never overwritten.

Model records aggregate each planning/decision/replanning invocation, including
any provider retries and failover. Observations are represented by fingerprints;
screenshots, prompts, action arguments and provider bodies are not included.
The optional journal retains payload-free event metadata beyond the checkpoint's
100-event history. Sequence IDs deduplicate entries after interrupted persistence.
Corrupt journal records are skipped; sequence gaps mark the report as partial.
Tool trace retention limits remain visible.

The checkpoint remains execution authority. Journal failures are reported without
preventing checkpoint persistence or changing action recovery. Shared worker
journals identify runs by checkpoint ID. Keep checkpoints and journals together
for inspection; missing earlier history cannot be reconstructed.
