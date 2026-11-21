# Durable context for longer tasks

The runtime derives compact checkpoint memory from its own execution data:

- Task progress and the active task.
- References to independently verified task results, labelled historical.
- Failed tool/view fingerprints and runtime error categories with deduplicated counts.
- A deterministic progress summary and final-check reference.

No additional model call generates this memory. It contains no screenshots, tool
arguments, provider bodies or model-proposed facts. Historical checks do not prove
the current application still has the same state. Fresh observations, task gates
and final verification remain authoritative.

Failure signatures and evidence references survive history truncation and resume.
Retention keeps the latest 24 of each; model context uses up to 12. Repacking does
not count the same recorded failure again.

Packing prioritizes the complete Goal, criteria and active task, and condenses
runtime progress instead of copying the entire DAG and evidence. Optional
observations, completed-plan detail, older memory and tool-directory entries are
removed as needed. Deferred schemas remain discoverable. Planning/replanning JSON
is serialized after whole-field compaction, never cut in the middle. Standalone
`pack_context` also preserves input goals and criteria without silently truncating.

If required contract text or the acceptance catalog still exceeds the budget,
execution pauses with `context_budget_exhausted` before model dispatch. Split the
goal or reduce the configured catalog. The journal and timeline provide longer
execution history beyond bounded model memory.
