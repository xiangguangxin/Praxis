"""Dispatch contracts, interrupted recovery, preflight and durable creation regressions."""
from contextlib import contextmanager
import io
import json
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from praxis_agent.agent import run, run_session
from praxis_agent.state import create, load, persist
from praxis_agent.policy import ActionPolicy
from praxis_agent.recovery import reconcile
from praxis_agent.storage import atomic_json_write
from praxis_agent.validation import InvalidToolAction, validate_action
from praxis_agent.executor import LiveDesktop
from praxis_agent.observations import normalize
from praxis_agent.planner import ModelPlanner
from praxis_agent.adapters.nova import NovaMCP

SCHEMA = {"type": "object", "properties": {"target": {"type": "string", "enum": ["fixture"]}},
          "required": ["target"], "additionalProperties": False}
TOOLS = [{"name": "click", "inputSchema": SCHEMA}]
GOOD = {"name": "click", "impact": "low", "arguments": {"target": "fixture"}}


class Backend:
    def __init__(self): self.calls = []
    def observe(self): return {"summary": "fixture", "tools": TOOLS}
    def act(self, action): self.calls.append(action); return {"ok": True}


class Planner:
    def __init__(self, action=GOOD): self.action = action
    def decide(self, state, observation):
        return {"type": "complete", "evidence": {"done": "fixture observed"}} if state["steps"] else {"type": "act", "action": self.action}


class DispatchRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "state.json"
        create(self.path, "Inspect fixture", ["done"])

    def test_invalid_tool_arguments_never_dispatch_or_require_reconciliation(self):
        for arguments in ({}, {"target": 17}, {"target": "wrong"}, {"target": "fixture", "secret": "PRIVATE"}):
            with self.subTest(arguments=arguments):
                path = self.root / (str(len(list(self.root.iterdir()))) + ".json")
                create(path, "goal", ["done"])
                backend = Backend()
                state = run(path, backend, Planner({**GOOD, "arguments": arguments}))
                self.assertEqual(backend.calls, [])
                self.assertEqual(state["last_error"]["code"], "tool_arguments_mismatch")
                self.assertFalse(state["last_error"]["may_have_executed"])
                self.assertIsNone(state["pending_action"])
                self.assertNotIn("PRIVATE", path.read_text())

    def test_planner_can_correct_a_rejected_action_without_spending_a_step(self):
        class Correcting(Planner):
            def decide(self, state, observation):
                if not state.get("last_result"):
                    return {"type": "act", "action": {**GOOD, "arguments": {"target": 17}}}
                return super().decide(state, observation)
        backend = Backend()
        result = run(self.path, backend, Correcting())
        self.assertEqual((result["status"], result["steps"], len(backend.calls)), ("completed", 1, 1))
        self.assertEqual(backend.calls[0]["arguments"], {"target": "fixture"})

    def test_unknown_tool_and_ambiguous_directory_are_rejected(self):
        for tools in ([], TOOLS + TOOLS):
            with self.assertRaises(InvalidToolAction): validate_action(GOOD, tools)

    def test_local_schema_refs_and_oneof_are_validated(self):
        schema = {"type": "object", "$defs": {"target": {"oneOf": [{"const": "fixture"}, {"type": "integer"}]}},
                  "properties": {"target": {"$ref": "#/$defs/target"}}, "required": ["target"]}
        tools = [{"name": "click", "inputSchema": schema}]
        validate_action(GOOD, tools)
        with self.assertRaises(InvalidToolAction): validate_action({**GOOD, "arguments": {"target": True}}, tools)

    def test_external_schema_references_never_fetch_network(self):
        tools = [{"name": "click", "inputSchema": {"$ref": "https://invalid.example/private-schema"}}]
        with patch("urllib.request.urlopen", side_effect=AssertionError("Network access forbidden")):
            with self.assertRaises(InvalidToolAction) as error: validate_action(GOOD, tools)
        self.assertEqual(error.exception.code, "invalid_or_unresolvable_tool_schema")

    def test_nonfinite_action_is_rejected(self):
        with self.assertRaises(InvalidToolAction): validate_action({**GOOD, "arguments": {"target": float("nan")}}, TOOLS)

    def test_schema_with_unsupported_dialect_is_rejected(self):
        with self.assertRaises(InvalidToolAction):
            validate_action(GOOD, [{"name": "click", "inputSchema": {"$schema": "https://invalid.example/dialect"}}])

    def test_policy_boolean_allow_does_not_match_numeric_argument(self):
        policy = ActionPolicy([{"tool": "confirm", "arguments": {"enabled": True}, "effect": "allow"}])
        self.assertEqual(policy.classify({"name": "confirm", "impact": "high", "arguments": {"enabled": 1}})[0], "approve")

    def test_policy_copies_operator_rules(self):
        rule = {"tool": "click", "arguments": {"target": "fixture"}, "effect": "deny"}
        policy = ActionPolicy([rule])
        rule["effect"] = "allow"
        self.assertEqual(policy.classify(GOOD)[0], "deny")

    def test_interrupt_during_dispatch_immediately_records_uncertain_outcome(self):
        class Interrupted(Backend):
            def act(self, action): raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): run(self.path, Interrupted(), Planner())
        state = load(self.path)
        self.assertEqual((state["status"], state["stop_reason"]), ("blocked", "reconciliation_required"))
        self.assertIsNotNone(state["pending_action"])
        self.assertEqual(reconcile(self.path, outcome="not-executed", note="Target independently checked")["status"], "paused")

    def test_interrupt_before_dispatch_pauses_without_uncertain_action(self):
        class Interrupted(Planner):
            def decide(self, *_): raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): run(self.path, Backend(), Interrupted())
        state = load(self.path)
        self.assertEqual((state["status"], state["stop_reason"]), ("paused", "interrupted"))
        self.assertIsNone(state["pending_action"])

    def test_hard_crash_running_checkpoint_can_be_reconciled_directly(self):
        state = load(self.path)
        state["status"], state["pending_action"] = "running", {"name": "click", "since": 1}
        persist(self.path, state)
        result = reconcile(self.path, outcome="executed", note="Inspected target after process exit")
        self.assertEqual((result["status"], result["steps"]), ("paused", 1))

    def test_preflight_rejects_wrong_backend_before_session_launch(self):
        state = load(self.path)
        state["backend_scope"] = "original"
        persist(self.path, state)
        factory = MagicMock(side_effect=AssertionError("Backend must not launch"))
        with self.assertRaises(ValueError): run_session(self.path, factory, Planner(), backend_scope="different")
        factory.assert_not_called()

    def test_completed_and_pending_goals_do_not_launch_backend(self):
        factory = MagicMock(side_effect=AssertionError("Backend must not launch"))
        state = load(self.path)
        state["status"] = "completed"
        persist(self.path, state)
        self.assertEqual(run_session(self.path, factory, Planner())["status"], "completed")
        factory.assert_not_called()

    def test_startup_failure_is_a_known_outcome_with_diagnostics(self):
        def factory(): raise OSError("token=PRIVATE")
        state = run_session(self.path, factory, Planner())
        self.assertEqual((state["status"], state["last_error"]["phase"]), ("blocked", "session"))
        self.assertFalse(state["last_error"]["may_have_executed"])
        self.assertNotIn("PRIVATE", self.path.read_text())

    def test_session_lock_covers_startup_and_dispatch(self):
        @contextmanager
        def factory():
            from praxis_agent.locking import checkpoint_lock
            with self.assertRaises(RuntimeError):
                with checkpoint_lock(self.path): pass
            yield Backend()
        self.assertEqual(run_session(self.path, factory, Planner())["status"], "completed")

    def test_manual_observation_content_changes_view_and_is_cleared_after_write(self):
        nova, planner = MagicMock(), MagicMock()
        nova.observe.return_value = {"summary": "manual observation backend"}
        nova.act.side_effect = [{"ok": True, "content": [{"type": "image", "data": "FIRST"}]}, {"ok": True, "content": []}]
        backend = LiveDesktop(nova, planner)
        before = normalize(backend.observe())
        backend.act({"name": "screenshot"})
        observed = normalize(backend.observe())
        self.assertEqual(observed["content"][0]["data"], "FIRST")
        self.assertNotEqual(before["observation_id"], observed["observation_id"])
        backend.act({"name": "click"})
        self.assertEqual(backend.observe()["content"], [])

    def test_automatic_observation_keeps_last_action_text_separate(self):
        nova, planner = MagicMock(), MagicMock()
        nova.act.return_value = {"ok": True, "content": [{"type": "text", "text": "result detail"}]}
        nova.observe.return_value = {"summary": "fresh", "content": [{"type": "image", "data": "NEW"}]}
        backend = LiveDesktop(nova, planner)
        backend.act({"name": "inspect"})
        observation = backend.observe()
        self.assertEqual(observation["content"][0]["data"], "NEW")
        self.assertEqual(observation["tool_result_content"][0]["text"], "result detail")

    def test_create_only_publication_never_overwrites_competing_file(self):
        target = self.root / "race.json"
        original_link = __import__("os").link
        def race(source, destination):
            target.write_text("COMPETING_WRITER", encoding="utf-8")
            return original_link(source, destination)
        with patch("praxis_agent.storage.os.link", side_effect=race):
            with self.assertRaises(FileExistsError): atomic_json_write(target, {"ours": True}, create_only=True)
        self.assertEqual(target.read_text(), "COMPETING_WRITER")
        self.assertEqual(list(self.root.glob(".praxis-*.tmp")), [])

    def test_nonfinite_storage_write_keeps_original_checkpoint(self):
        original = self.path.read_bytes()
        with self.assertRaises(ValueError): atomic_json_write(self.path, {"value": float("inf")})
        self.assertEqual(self.path.read_bytes(), original)

    def test_corrupt_checkpoint_fields_are_rejected_before_backend_launch(self):
        original = load(self.path)
        for changed in ({"version": True}, {"status": []}, {"pending_action": []}, {"criteria": ["done", "done"]},
                        {"recent_actions": [None]}, {"pending_review": 17}, {"model_usage": {"calls": -1}},
                        {"artifact_baseline": {"verifier": "id"}}):
            with self.subTest(changed=changed):
                self.path.write_text(json.dumps({**original, **changed}), encoding="utf-8")
                with self.assertRaises(ValueError): load(self.path)

    def test_nova_eof_wakes_rpc_without_waiting_for_timeout(self):
        nova = NovaMCP.__new__(NovaMCP)
        nova.next_id, nova.process, nova.responses = 0, MagicMock(), queue.Queue()
        nova.process.stdin = io.BytesIO()
        nova.process.poll.return_value = None
        nova.responses.put(None)
        with self.assertRaisesRegex(RuntimeError, "stream closed"): nova._rpc("test", {})

    def test_nova_cleanup_is_idempotent_after_process_exit(self):
        nova = NovaMCP.__new__(NovaMCP)
        nova.process, nova._reader = MagicMock(), MagicMock()
        nova.process.poll.return_value = 0
        nova.process.stdin, nova.process.stdout = io.BytesIO(), io.BytesIO()
        nova.close(); nova.close()
        nova.process.terminate.assert_not_called()
        nova._reader.join.assert_called_once()


if __name__ == "__main__": unittest.main()
