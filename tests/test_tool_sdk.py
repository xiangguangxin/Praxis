import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from praxis_agent import Runtime
from praxis_agent.adapters.tool_backend import open_tool_backend, config_identity
from praxis_agent.policy import ActionPolicy
from praxis_agent.tools import ToolError
from praxis_agent.verification import FileVerifier

FIXTURE = Path(__file__).parent / "fixtures/mcp_echo.py"


class ToolSDKTests(unittest.TestCase):
    def config(self, root, data):
        path = root / "tools.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_host_declared_impact_requires_approval_even_if_planner_labels_write_low(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = self.config(root, {"files": {"write_json": True}})
            arguments = {"path": "report.json", "data": {"status": "done"}}
            class Planner:
                def decide(self, state, observation):
                    if state["steps"]: return {"type": "complete"}
                    return {"type": "act", "action": {"name": "file_write_json", "arguments": arguments, "impact": "low"}}
            verifier = FileVerifier({"report": {"path": "report.json", "fresh": True, "json_fields": {"/status": "done"}}}, root)
            runtime = Runtime.tools(config, Planner(), verifier=verifier)
            checkpoint = root / "state.json"
            paused = runtime.execute(checkpoint, goal="Produce report", criteria=["report"])
            self.assertEqual(paused["stop_reason"], "approval_required")
            self.assertFalse((root / "report.json").exists())
            result = runtime.execute(checkpoint, approve_pending=True, approval_id=paused["pending_approval"])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(json.loads((root / "report.json").read_text(encoding="utf-8")), arguments["data"])

    def test_file_preflight_rejects_escape_wrong_types_and_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = self.config(root, {"files": {"read": True, "write_json": True}})
            with open_tool_backend(config) as backend:
                for args in ({"path": "../outside.json", "data": {}}, {"path": str(root / "outside.json"), "data": {}},
                             {"path": "tools.json", "data": {}}):
                    with self.assertRaises(ValueError): backend.validate_action({"name": "file_write_json", "arguments": args})
                with self.assertRaises(ValueError): backend.validate_action({"name": "file_read_text", "arguments": {"path": 7}})
                with self.assertRaises(PermissionError):
                    backend.act({"name": "file_write_json", "arguments": {"path": "new.json", "data": {}}})
                self.assertTrue(all("inputSchema" in tool for tool in backend.observe()["tools"]))

    def test_http_has_static_origin_no_redirects_and_transient_results(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = {"http": {"allowed_origins": ["https://api.fixture.test"], "tools": [
                {"name": "read_status", "url": "https://api.fixture.test/status", "method": "GET"}]}}
            config = self.config(root, data)
            response = MagicMock()
            response.read.return_value = b'{"ready":true}'
            opener = MagicMock()
            opener.open.return_value.__enter__.return_value = response
            with patch("urllib.request.build_opener", return_value=opener), open_tool_backend(config) as backend:
                backend.act({"name": "read_status", "arguments": {}})
                self.assertEqual(json.loads(backend.observe()["content"][0]["text"]), {"ready": True})
                self.assertEqual(opener.open.call_args.args[0].get_method(), "GET")
            data["http"]["tools"][0]["url"] = "https://outside.test/status"
            self.config(root, data)
            with self.assertRaises(ValueError):
                with open_tool_backend(config): pass

    def test_fixed_command_runs_without_shell_and_rejects_model_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = self.config(root, {"commands": [{"name": "version", "argv": [sys.executable, "-c", "print('fixture-version')"]}]})
            with open_tool_backend(config) as backend:
                with self.assertRaises(ValueError): backend.validate_action({"name": "version", "arguments": {"command": "other"}})
                with self.assertRaises(PermissionError): backend.act({"name": "version", "arguments": {}})
                self.assertTrue(backend.act_authorized({"name": "version", "arguments": {}})["ok"])
                self.assertIn("fixture-version", backend.observe()["content"][0]["text"])

    def test_real_stdio_mcp_exposes_only_operator_allowlisted_tools_without_auto_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            trace = root / "calls.log"
            config = self.config(root, {"mcp": [{"name": "fixture", "command": [sys.executable, str(FIXTURE), str(trace)],
                                               "tools": ["echo", "mutate"], "readonly_tools": ["echo"]}]})
            with open_tool_backend(config) as backend:
                self.assertEqual(len(backend.observe()["tools"]), 2)
                self.assertFalse(trace.exists())
                result = backend.act({"name": "mcp.fixture.echo", "arguments": {"message": "hello"}})
                self.assertEqual(result["content"][0]["text"], "echo:hello")
                with self.assertRaises(PermissionError):
                    backend.act({"name": "mcp.fixture.mutate", "arguments": {"message": "hello"}})
                with self.assertRaises(RuntimeError):
                    backend.act_authorized({"name": "mcp.fixture.mutate", "arguments": {"message": "hello"}})
            self.assertEqual(trace.read_text(encoding="utf-8").splitlines(), ["echo", "mutate"])

    def test_changed_config_is_rejected_before_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = self.config(root, {"files": {"read": True}})
            identity = config_identity(config)
            self.config(root, {"files": {"read": True, "write_json": True}})
            with self.assertRaises(ValueError):
                with open_tool_backend(config, expected_identity=identity): pass

    def test_sdk_demo_runs_as_a_real_process_with_independent_artifact_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "demo"
            demo = Path(__file__).resolve().parents[1] / "examples/sdk_demo.py"
            result = subprocess.run([sys.executable, str(demo), "--output", str(output)],
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["verification_level"], "independent_artifact")
            self.assertFalse(report["real_model"])
            self.assertEqual(json.loads((output / "report.json").read_text(encoding="utf-8"))["kind"], "sdk-demo")

    def test_reusing_runtime_starts_a_fresh_per_goal_model_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "report.txt").write_text("PASS", encoding="utf-8")
            config = self.config(root, {"files": {"read": True}})
            class Planner:
                def __init__(self): self.usage = {"calls": 0}
                def decide(self, state, observation):
                    self.usage["calls"] += 1
                    return {"type": "complete"}
            runtime = Runtime.tools(config, Planner(), verifier=FileVerifier({"report": {"path": "report.txt"}}, root))
            first = runtime.execute(root / "one.json", goal="Inspect", criteria=["report"])
            second = runtime.execute(root / "two.json", goal="Inspect", criteria=["report"])
            self.assertEqual(first["model_usage"]["calls"], 1)
            self.assertEqual(second["model_usage"]["calls"], 1)

    def test_command_timeout_preserves_unknown_outcome_for_reconciliation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = self.config(root, {"commands": [{"name": "slow", "argv": [sys.executable, "-c", "import time; time.sleep(2)"],
                                                      "timeout_seconds": .05}]})
            class Planner:
                def decide(self, state, observation):
                    return {"type": "act", "action": {"name": "slow", "arguments": {}, "impact": "low"}}
            policy = ActionPolicy([{"tool": "slow", "arguments": {}, "effect": "allow"}])
            result = Runtime.tools(config, Planner(), policy=policy).execute(root / "state.json", goal="Run command", criteria=["done"])
            self.assertEqual(result["stop_reason"], "reconciliation_required")
            self.assertIsNotNone(result["pending_action"])
