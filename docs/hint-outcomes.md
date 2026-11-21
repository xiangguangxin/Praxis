# Track experience and strategy exposure

Runtime checkpoints record which hint IDs, environments and source-store identities
were actually supplied to model requests, with step and call ranges. Retrieval that
fails its budget check before dispatch is not recorded as exposure. Hint text and
operator notes are not copied into the exposure ledger. The run timeline and paired
benchmark records include these IDs.

```bash
praxis-learning hint-outcomes --checkpoints ./checkpoints --output hint-outcomes.json
```

The report deduplicates checkpoints and repeated exposures within a run. It groups
recorded verified completions, blocked/failed runs and inconclusive outcomes by
hint and environment. These are observational associations: exposure does not prove
that the model followed the hint, or that it caused success or failure. Paired
baseline/candidate benchmarks remain the evidence for efficacy and promotion.

After independently inspecting a run, explicitly attribute feedback:

```bash
praxis-experience --store experiences.json feedback-run EXPERIENCE_ID goal.json \
  --verifier verifier.json --outcome success --note "Observed why this hint helped"
```

This checks the original file verifier, actual exposure, environment and current
artifact result. Success requires independently verified completion; failure requires
a blocked/failed run with failed checks. Pauses, unknown action outcomes, mismatched
stores and unexposed hints are rejected. A note records operator attribution; no
automatic causal judgement is made. Each checkpoint/experience pair updates scores
once, with an atomic receipt. Conflicting repeat feedback is rejected. Submit feedback
after final investigation; retries of the same goal do not create a new feedback ID.
Two explicitly attributed verified failures retain the existing invalidation behavior.
