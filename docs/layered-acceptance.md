# Intermediate and final acceptance

A Goal's criteria describe the final deliverable. Task acceptance entries can
also name intermediate checks from the trusted verifier's rules. The model may
select existing rule names; it cannot create or alter verifier rules.

Create a Goal with only `report valid`, while supplying rules named `app open`,
`configured`, and `report valid`. The plan can verify opening the app, configuring
it, then exporting the report. Every final Goal criterion must appear in the plan.
After all tasks finish, the runtime independently rechecks final Goal criteria.

The acceptance catalog is checkpointed and checked on resume and replacement of
unfinished tasks. Completed task definitions and evidence are preserved. Existing
plans that use only Goal criteria remain compatible. File rules use `--verifier`;
application checks use the programmatic `CriteriaVerifier` interface.

Only configure intermediate checks that independently establish the relevant
state. Without intermediate checks, combine actions into a verifiable task.
