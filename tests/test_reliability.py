"""Behavioral regressions for review, budgets, discovery, diagnostics and curation."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from praxis_agent.agent import run
from praxis_agent.cli import main
from praxis_agent.policy import ActionPolicy, needs_approval
from praxis_agent.planner import ModelPlanner, _context
from praxis_agent.state import create, load, persist
from praxis_agent.verification import FileVerifier
from praxis_agent.recovery import retry
from praxis_agent.experience import ExperienceStore
from praxis_agent.adapters.nova import NovaMCP


class Backend:
    def __init__(self, root, action=None):
        self.root, self.actions, self.action = root, [], action
        self.view = "same view"
    def observe(self):
        return {"summary": self.view, "action_scope": "fixture", "tools": [{"name": "click"}]}
    def act(self, action):
        self.actions.append(action)
        if self.action:
            return self.action(action)
        (self.root / "report.txt").write_text("PASS", encoding="utf-8")
        return {"ok": True}


class OneAction:
    def __init__(self, action=None):
        self.action = action or {"name": "click", "impact": "low"}
    def decide(self, state, observation):
        return {"type": "complete", "evidence": {"report": "seen"}} if state["steps"] else {"type": "act", "action": self.action}


class Response:
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def read(self):
        return json.dumps({"choices": [{"message": {"content": '{"type":"pause"}'}}],
                           "usage": {"prompt_tokens": 20, "completion_tokens": 5}}).encode()


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "state.json"
        self.backend = Backend(self.root)

    def goal(self, steps=4):
        return create(self.path, "Create report", ["report"], max_steps=steps)

    def verifier(self, fresh=False):
        return FileVerifier({"report": {"path": "report.txt", "contains": "PASS", "fresh": fresh}}, self.root)

    def test_last_action_still_gets_independent_verification(self):
        self.goal(1)
        state = run(self.path, self.backend, OneAction(), verifier=self.verifier(True))
        self.assertEqual((state["status"], state["steps"]), ("completed", 1))
        self.assertEqual(state["verification_level"], "independent_artifact")
        self.assertEqual(state["verification"]["checks"]["report"]["status"], "passed")

    def test_budget_never_permits_an_extra_mutation(self):
        self.goal(1)
        class AlwaysAct:
            def decide(self, *_): return {"type": "act", "action": {"name": "click", "impact": "low"}}
        state = run(self.path, self.backend, AlwaysAct())
        self.assertEqual(state["stop_reason"], "step_budget_exhausted")
        self.assertEqual(len(self.backend.actions), 1)
        self.assertEqual(run(self.path, self.backend, AlwaysAct())["status"], "paused")
        self.assertEqual(len(self.backend.actions), 1)

    def test_model_claim_is_explicitly_labelled(self):
        self.goal()
        state = run(self.path, self.backend, OneAction())
        self.assertEqual(state["verification_level"], "model_evidence")
        self.assertEqual(state["verification"]["source"], "model_claim")

    def test_generic_click_sensitive_targets_need_review_in_both_languages(self):
        for name in ("Send", "Delete account", "确认付款", "发送消息", "Publish changes"):
            self.assertTrue(needs_approval({"name": "browser_click", "impact": "low", "arguments": {"name": name}}))
        self.assertFalse(needs_approval({"name": "browser_fill", "impact": "low", "arguments": {"label": "Note", "text": "send a test message"}}))

    def test_review_is_redacted_exact_and_one_action_only(self):
        self.goal()
        action = {"name": "click", "impact": "high", "arguments": {"name": "Send", "text": "PRIVATE_TOKEN"}}
        planner = OneAction(action)
        first = run(self.path, self.backend, planner)
        self.assertEqual(first["stop_reason"], "approval_required")
        self.assertEqual(first["pending_review"]["arguments"]["text"], "[redacted]")
        self.assertNotIn("PRIVATE_TOKEN", self.path.read_text())
        with self.assertRaises(ValueError):
            run(self.path, self.backend, planner, approval=True, approval_id="wrong")
        done = run(self.path, self.backend, planner, approval=True, approval_id=first["pending_approval"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(len(self.backend.actions), 1)

    def test_approval_rejected_when_view_or_arguments_change(self):
        for change in ("view", "arguments", "expiry"):
            with self.subTest(change=change):
                path = self.root / (change + ".json")
                create(path, "goal", ["report"])
                action = {"name": "click", "impact": "high", "arguments": {"name": "Send"}}
                planner = OneAction(action)
                first = run(path, self.backend, planner)
                if change == "view": self.backend.view = "different view"
                if change == "arguments": planner.action = {**action, "arguments": {"name": "Delete"}}
                if change == "expiry":
                    first["pending_review"]["expires_at"] = 0
                    persist(path, first)
                state = run(path, self.backend, planner, approval=True)
                self.assertEqual(state["stop_reason"], "approval_required")
                self.assertEqual(self.backend.actions, [])

    def test_exact_operator_policy_can_authorize_only_the_matching_target(self):
        action = {"name": "click", "impact": "high", "arguments": {"name": "Submit test fixture"}}
        policy = ActionPolicy([{"tool": "click", "arguments": action["arguments"], "effect": "allow"}])
        self.assertEqual(policy.classify(action)[0], "allow")
        self.assertEqual(policy.classify({**action, "arguments": {"name": "Submit production"}})[0], "approve")
        denied = ActionPolicy([*policy.rules, {"tool": "click", "arguments": action["arguments"], "effect": "deny"}])
        self.assertEqual(denied.classify(action)[0], "deny")

    def test_denied_action_does_not_dispatch_and_cannot_be_retried(self):
        self.goal()
        policy = ActionPolicy([{"tool": "click", "arguments": {}, "effect": "deny"}])
        state = run(self.path, self.backend, OneAction(), policy=policy)
        self.assertEqual(state["stop_reason"], "policy_denied")
        self.assertEqual(self.backend.actions, [])
        with self.assertRaises(ValueError): retry(self.path, note="bypass")

    def test_unchanged_view_repetition_pauses(self):
        self.goal(10)
        class AlwaysAct:
            def decide(self, *_): return {"type": "act", "action": {"name": "click", "impact": "low"}}
        state = run(self.path, self.backend, AlwaysAct())
        self.assertEqual(state["stop_reason"], "no_progress")
        self.assertEqual(len(self.backend.actions), 3)

    def test_uncertain_action_has_payload_free_diagnostics_and_requires_reconcile(self):
        self.goal()
        def fail(_): raise TimeoutError("password=PRIVATE_TOKEN https://secret.invalid/?token=PRIVATE_TOKEN")
        self.backend.action = fail
        state = run(self.path, self.backend, OneAction())
        self.assertEqual(state["last_error"]["phase"], "action")
        self.assertTrue(state["last_error"]["may_have_executed"])
        self.assertNotIn("PRIVATE_TOKEN", self.path.read_text())
        with self.assertRaises(ValueError): retry(self.path, note="retry")

    def test_known_failure_can_be_retried_without_replaying_an_old_action(self):
        self.goal()
        class FailPlanning:
            def decide(self, *_): raise TimeoutError("provider failure")
        state = run(self.path, self.backend, FailPlanning())
        self.assertFalse(state["last_error"]["may_have_executed"])
        retry(self.path, note="provider restored")
        self.assertEqual(run(self.path, self.backend, OneAction())["status"], "completed")

    def test_nonfresh_verifier_cannot_be_dropped_on_resume(self):
        self.goal()
        class Pause:
            def decide(self, *_): return {"type": "pause"}
        run(self.path, self.backend, Pause(), verifier=self.verifier())
        with self.assertRaises(ValueError): run(self.path, self.backend, OneAction())

    def test_tool_after_twelfth_can_be_discovered_with_full_schema(self):
        state = self.goal()
        tools = [{"name": f"tool_{i}", "description": "fixture", "inputSchema": {"required": ["target"]}} for i in range(50)]
        first = json.loads(_context(state, {"tools": tools}, 5000))
        self.assertEqual(first["tool_catalog"]["total"], 50)
        self.assertEqual(first["tool_catalog"]["next_offset"], 24)
        state["tool_selection"] = {"query": "tool_49", "offset": 0, "names": ["tool_49"]}
        found = json.loads(_context(state, {"tools": tools}, 5000))["tool_catalog"]
        self.assertEqual(found["schemas"][0]["name"], "tool_49")
        self.assertEqual(found["schemas"][0]["inputSchema"], {"required": ["target"]})

    def test_large_tool_schemas_and_notes_preserve_valid_json(self):
        state = self.goal()
        tools = [{"name": f"tool_{i}", "inputSchema": {"description": "x" * 6000}} for i in range(30)]
        context = _context(state, {"tools": tools, "ui_tree": "x" * 10000}, 5000)
        self.assertLessEqual(len(context), 18000)
        parsed = json.loads(context)
        self.assertEqual(parsed["criteria"], ["report"])
        self.assertIn("schema_unavailable", parsed["tool_catalog"]["schemas"][0])

    def test_discovery_is_internal_and_does_not_spend_an_action(self):
        self.goal()
        class Discover(OneAction):
            def decide(self, state, observation):
                if "tool_selection" not in state:
                    return {"type": "discover", "query": "click", "tools": ["click"]}
                return super().decide(state, observation)
        state = run(self.path, self.backend, Discover())
        self.assertEqual((state["status"], state["steps"]), ("completed", 1))

    def test_recent_actions_reach_model_without_argument_payloads(self):
        state = self.goal()
        state["recent_actions"] = [{"tool": "click", "ok": True, "repeat_id": "private hash"}]
        captured = []
        def model(req, timeout): captured.append(json.loads(req.data)); return Response()
        with patch("praxis_agent.planner.urllib.request.urlopen", side_effect=model):
            ModelPlanner("fake", "fake", "https://invalid").decide(state, {"summary": "fixture"})
        context = json.loads(captured[0]["messages"][1]["content"])
        self.assertEqual(context["recent_actions"], [{"tool": "click", "ok": True}])

    def test_new_experience_contains_explanation_and_verified_provenance(self):
        self.goal()
        run(self.path, self.backend, OneAction(), verifier=self.verifier(True))
        item = ExperienceStore(self.root / "memory.json").add_verified(self.path, verifier=self.verifier(True),
            environment="fixture", note="checked", preconditions=["Fixture ready"],
            key_steps=["Build before running"], failure_modes=["Wait for completion"])
        self.assertEqual(item["key_steps"], ["Build before running"])
        self.assertEqual(item["evidence"]["passed_checks"], 1)
        self.assertEqual(len(item["evidence"]["artifact_hashes"][0]), 64)
        self.assertNotIn(str(self.root), json.dumps(item))

    def test_nova_auto_observes_only_advertised_readonly_noarg_tools(self):
        nova = NovaMCP.__new__(NovaMCP)
        nova.backend_version = "fixture"
        nova.tools = [{"name": "screenshot", "inputSchema": {}, "annotations": {"readOnlyHint": True}}]
        nova.call = lambda name, args: {"content": [{"type": "text", "text": "fixture window"}]}
        self.assertEqual(nova.observe()["ui_tree"], "fixture window")
        nova.tools[0]["annotations"]["readOnlyHint"] = False
        nova.call = lambda *_: self.fail("Must not invoke an unclassified observation tool")
        self.assertNotIn("content", nova.observe())

    def test_cli_exit_codes_distinguish_completion_pause_and_block(self):
        for status, code in (("completed", 0), ("paused", 3), ("blocked", 4), ("failed", 5)):
            with self.subTest(status=status), patch("sys.argv", ["praxis", "run", "state.json", "--nova", "fixture"]), \
                    patch("praxis_agent.cli._execute", return_value={"status": status}), patch("sys.stdout", io.StringIO()):
                with self.assertRaises(SystemExit) as raised: main()
                self.assertEqual(raised.exception.code, code)

    def test_create_rejects_silent_loss_of_goal_criteria(self):
        for criteria in ([""], ["same", "same"], ["x"] * 21):
            with self.assertRaises(ValueError): create(self.path, "goal", criteria)


if __name__ == "__main__": unittest.main()
