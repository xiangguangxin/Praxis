import functools
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from praxis_agent.benchmark import _case_url, _metrics, _validate

ROOT = Path(__file__).resolve().parents[1]


class AcceptanceSuiteTests(unittest.TestCase):
    def test_suite_has_twelve_unique_independently_checked_tasks(self):
        cases = json.loads((ROOT / "examples/live-acceptance-suite.json").read_text())["scenarios"]
        _validate(cases, 3)
        self.assertEqual(len(cases), 12)
        for case in cases:
            self.assertTrue(case["rules"]["Task report verified"]["fresh"])
            self.assertIn("/value", case["rules"]["Task report verified"]["json_fields"])
            self.assertTrue(_case_url("http://localhost:8765/", case).startswith("http://localhost:8765/"))

    def test_scenario_cannot_replace_backend_origin(self):
        for path in ["https://other.test/", "//other.test/", 42]:
            with self.assertRaises(ValueError):
                _case_url("https://fixture.test/", {"start_path": path})

    def test_metrics_distinguish_false_completion_and_report_uncertainty(self):
        def row(passed, verified, elapsed):
            return {"passed": passed, "completed_and_verified": verified, "elapsed_seconds": elapsed,
                    "status": "completed", "expected_status": "completed", "usage_missing_calls": 0,
                    "model_calls": 1, "input_tokens": 10, "output_tokens": 2, "experience_hint_calls": 0}
        report = _metrics([row(True, True, 1), row(False, False, 9)], False)
        self.assertEqual(report["false_completion_rate"], .5)
        self.assertEqual(report["p95_seconds"], 9)
        low, high = report["success_rate_95ci"]
        self.assertLess(low, .5)
        self.assertGreater(high, .5)


@unittest.skipUnless(os.environ.get("OPENAS_TEST_BROWSER") == "1", "opt-in real Chromium fixture test")
class AcceptanceBrowserTests(unittest.TestCase):
    def test_all_twelve_fixtures_with_deterministic_actions(self):
        from praxis_agent.adapters.browser import open_browser
        from praxis_agent.agent import run
        from praxis_agent.state import create
        from praxis_agent.verification import FileVerifier
        from praxis_agent.policy import ActionPolicy
        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *args): pass
        handler = functools.partial(QuietHandler, directory=str(ROOT / "examples/acceptance-web"))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cases = json.loads((ROOT / "examples/live-acceptance-suite.json").read_text())["scenarios"]
        values = {"customer": "Alice", "unicode": "西安研发", "quantity": "7", "trim": "  Alpha  ",
                  "uppercase": "praxis", "confirm": "approved", "region": "report", "wizard": "step-one",
                  "filter": "delta", "validation": "0"}
        def click(name): return {"name": "browser_click", "arguments": {"role": "button", "name": name}, "impact": "low"}
        def fill(value): return {"name": "browser_fill", "arguments": {"label": "Value", "text": value}, "impact": "low"}
        try:
            for case in cases:
                task = case["name"]
                with self.subTest(task=task), tempfile.TemporaryDirectory() as folder:
                    root = Path(folder)
                    actions = [fill(values[task])] if task in values else []
                    if task in {"confirm", "region", "wizard", "filter"}:
                        actions.append(click({"confirm": "Confirm consent", "region": "Region West",
                                              "wizard": "Next step", "filter": "Select delta"}[task]))
                    if task in {"delay", "retry"}:
                        actions.append(click("Prepare report"))
                        if task == "retry": actions.append(click("Prepare report"))
                        actions.append({"name": "browser_wait_for_text", "arguments": {"text": "Report ready"}, "impact": "low"})
                    if task == "validation": actions.extend([click("Export report"), fill("7")])
                    actions.append({"name": "browser_download", "arguments": {"role": "button", "name": "Export report",
                                                                              "filename": "report.json"}, "impact": "low"})
                    class Planner:
                        def decide(self, state, observation):
                            return {"type": "act", "action": actions.pop(0)} if actions else {"type": "complete"}
                    path = root / "state.json"
                    create(path, case["goal"], list(case["rules"]))
                    url = _case_url(f"http://127.0.0.1:{server.server_port}/", case)
                    with open_browser(url, root, os.environ.get("OPENAS_TEST_BROWSER_EXECUTABLE")) as browser:
                        state = run(path, browser, Planner(), verifier=FileVerifier(case["rules"], root),
                                    policy=ActionPolicy.from_json(ROOT / "examples/acceptance-policy.json"))
                    self.assertEqual(state["status"], "completed", state)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
