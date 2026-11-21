import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from praxis_agent.agent import run
from praxis_agent.state import create
from praxis_agent.verifiers.configured import ConfiguredVerifier, NoRedirect, load_verifier
from praxis_agent.verifiers.registry import CriteriaVerifier, VerificationRegistry


class ConfiguredVerifierTests(unittest.TestCase):
    def test_file_database_and_application_checks_complete_through_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "report.txt").write_text("PASS", encoding="utf-8")
            with sqlite3.connect(root / "app.db") as db:
                db.execute("create table jobs(status text)")
                db.execute("insert into jobs values ('complete')")
            rules = {"file": {"path": "report.txt", "contains": "PASS"},
                     "db": {"kind": "sqlite", "path": "app.db", "query": "SELECT status FROM jobs", "rows": [["complete"]]},
                     "app": {"kind": "app_state", "json_fields": {"/ready": True}}}
            config = root / "rules.json"
            config.write_text(json.dumps({"criteria": rules}), encoding="utf-8")
            class Backend:
                def observe(self): return {"summary": "ready"}
                def verification_state(self): return {"ready": True}
            class Planner:
                def decide(self, *args): return {"type": "complete"}
            path = root / "state.json"
            create(path, "Check output", list(rules))
            result = run(path, Backend(), Planner(), verifier=load_verifier(config))
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["verification_level"], "custom_verifier")
            verifier = ConfiguredVerifier({"bad": {"kind": "sqlite", "path": "app.db",
                                                      "query": "DELETE FROM jobs", "rows": []}}, root)
            self.assertFalse(verifier.evaluate({"criteria": ["bad"]})["passed"])

    def test_dom_checks_require_unique_visible_target_and_exact_value(self):
        with tempfile.TemporaryDirectory() as folder:
            verifier = ConfiguredVerifier({"ui": {"kind": "dom", "role": "textbox", "name": "Value", "value": "7"}}, Path(folder))
            page = Mock()
            locator = page.get_by_role.return_value
            locator.count.return_value, locator.is_visible.return_value, locator.input_value.return_value = 1, True, "7"
            verifier.bind_backend(Mock(page=page))
            self.assertTrue(verifier.evaluate({"criteria": ["ui"]})["passed"])
            locator.count.return_value = 2
            self.assertFalse(verifier.evaluate({"criteria": ["ui"]})["passed"])

    def test_http_origin_allowlist_and_redirects(self):
        with tempfile.TemporaryDirectory() as folder:
            rule = {"kind": "http_json", "url": "https://app.test/result", "json_fields": {"/status": "done"}}
            verifier = ConfiguredVerifier({"api": rule}, Path(folder), allowed_http_origins=["https://app.test"])
            response = Mock(status=200)
            response.read.return_value = b'{"status":"done"}'
            opener = Mock()
            opener.open.return_value.__enter__ = Mock(return_value=response)
            opener.open.return_value.__exit__ = Mock(return_value=False)
            with patch("urllib.request.build_opener", return_value=opener):
                self.assertTrue(verifier.evaluate({"criteria": ["api"]})["passed"])
            denied = ConfiguredVerifier({"api": rule}, Path(folder))
            with patch("urllib.request.build_opener") as factory:
                self.assertFalse(denied.evaluate({"criteria": ["api"]})["passed"])
                factory.assert_not_called()
            with self.assertRaises(ValueError): NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.test")

    def test_screenshot_region_detects_changed_pixels(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("visual extra required")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image = Image.new("RGB", (8, 8), "white")
            image.save(root / "baseline.png")
            data = io.BytesIO()
            image.save(data, format="PNG")
            page = Mock()
            page.screenshot.return_value = data.getvalue()
            verifier = ConfiguredVerifier({"pixels": {"kind": "screenshot_region", "baseline": "baseline.png", "region": [0, 0, 4, 4]}}, root)
            verifier.bind_backend(Mock(page=page))
            self.assertTrue(verifier.evaluate({"criteria": ["pixels"]})["passed"])
            image.putpixel((1, 1), (0, 0, 0))
            data = io.BytesIO()
            image.save(data, format="PNG")
            page.screenshot.return_value = data.getvalue()
            self.assertFalse(verifier.evaluate({"criteria": ["pixels"]})["passed"])

    def test_criteria_verifier_cannot_be_dropped_or_changed_on_resume(self):
        registry = VerificationRegistry()
        registry.register("ready", lambda params: {"passed": True}, revision="v1")
        verifier = CriteriaVerifier(registry, {"ready": {"verifier": "ready"}})
        class Backend:
            def observe(self): return {"summary": "ready"}
        class Planner:
            def decide(self, *args): return {"type": "pause"}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            create(path, "Inspect", ["ready"])
            run(path, Backend(), Planner(), verifier=verifier)
            with self.assertRaises(ValueError): run(path, Backend(), Planner())
            changed = CriteriaVerifier(registry, {"ready": {"verifier": "ready", "parameters": {"different": True}}})
            with self.assertRaises(ValueError): run(path, Backend(), Planner(), verifier=changed)
