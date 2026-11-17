# Migrating from OpenAS GUI Agent Runtime

Praxis is extracted from [OpenAS](https://github.com/xiangguangxin/OpenAS/tree/main/packages/gui-agent-runtime). The agent loop, browser/Nova adapters, verifier, benchmark, checkpoint and experience lifecycle have been moved without changing the checkpoint schema.

## Installation

```bash
python -m pip uninstall openas-gui-agent
python -m pip install -e .
```

Python module imports migrate from `openas_gui_agent.*` to `praxis_agent.*`; use `praxis`, `praxis-benchmark`, and `praxis-experience` as the preferred commands. Legacy **CLI entry points** `openas-gui`, `openas-benchmark`, and `openas-experience` remain registered as aliases for now. These are CLI aliases, **not** old Python import aliases.

Existing v1/v2 checkpoints and local ExperienceStore JSON stay readable; retain safe backup copies of production state before migration. The independent runtime does not inherit any Claude Code/Codex account or MCP configuration.

## Environment variables

Prefer `PRAXIS_MODEL`, `PRAXIS_API_KEY`, `PRAXIS_API_URL`, and `PRAXIS_LITELLM_BASE_URL`. For compatibility, the legacy `OPENAS_GUI_MODEL`, `OPENAS_GUI_API_KEY`, `OPENAS_GUI_API_URL`, and `OPENAS_LITELLM_BASE_URL` names are also accepted. LiteLLM continues to use `LITELLM_API_KEY`.

## Compatibility boundary

OpenAS remains a skills-and-workflows plugin and may integrate Praxis as an optional installed runtime. Praxis does not embed OpenAS skills, Claude plugin metadata or CLI workflows.

This extraction changes package/import and entry-point names, but it does **not** add automatic real-world desktop verification or unattended execution.
