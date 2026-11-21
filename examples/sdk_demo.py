"""Run a verified configured-tool goal without API credentials."""
import argparse
import json
from pathlib import Path
from praxis_agent import Runtime
from praxis_agent.policy import ActionPolicy
from praxis_agent.verification import FileVerifier


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    config = root / "tools.json"
    config.write_text(json.dumps({"files": {"write_json": True}}), encoding="utf-8")
    arguments = {"path": "report.json", "data": {"kind": "sdk-demo", "verified": True}}
    class FixturePlanner:
        def decide(self, state, observation):
            if state["steps"]: return {"type": "complete"}
            return {"type": "act", "action": {"name": "file_write_json", "arguments": arguments, "impact": "low"}}
    verifier = FileVerifier({"report valid": {"path": "report.json", "fresh": True,
                                             "json_fields": {"/kind": "sdk-demo", "/verified": True}}}, root)
    policy = ActionPolicy([{"tool": "file_write_json", "arguments": arguments, "effect": "allow"}])
    runtime = Runtime.tools(config, FixturePlanner(), verifier=verifier, policy=policy, journal=root / "events.jsonl")
    result = runtime.execute(root / "goal.json", goal="Create the authorized SDK fixture report", criteria=["report valid"])
    print(json.dumps({"kind": "deterministic-sdk-demo", "real_model": False,
                      "status": result["status"], "verification_level": result["verification_level"]}, indent=2))
    raise SystemExit(0 if result["status"] == "completed" else 1)


if __name__ == "__main__": main()
