import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from praxis_agent.agent import run
from praxis_agent.model_router import ModelProfile, ModelRouter
from praxis_agent.model_transport import ModelRequestError
from praxis_agent.planner import ModelPlanner, TokenBudgetExceeded
from praxis_agent.state import create


def response():
    return io.BytesIO(json.dumps({"choices": [{"message": {"content": '{"type":"pause"}'}}],
                                  "usage": {"prompt_tokens": 10, "completion_tokens": 2}}).encode())


class ModelResilienceTests(unittest.TestCase):
    def planner(self, **kwargs): return ModelPlanner("fixture", "fixture-key", "https://fixture.test/v1/chat/completions", **kwargs)

    def test_transient_request_retries_are_charged_and_preserve_auth(self):
        planner = self.planner()
        with patch("urllib.request.urlopen", side_effect=[urllib.error.URLError("private detail"), response()]) as call, patch("time.sleep"):
            self.assertEqual(planner._request([])["type"], "pause")
        self.assertEqual(planner.usage["calls"], 2)
        self.assertEqual(planner.usage["retries"], 1)
        self.assertEqual(planner.usage["usage_missing_calls"], 1)
        self.assertGreater(planner.usage["estimated_output_tokens"], 0)
        for args in call.call_args_list:
            self.assertEqual(args.args[0].get_header("Authorization"), "Bearer fixture-key")

    def test_auth_failure_stops_after_one_actual_request_in_runtime(self):
        class Backend:
            def observe(self): return {"summary": "ready"}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            create(path, "Inspect", ["ready"])
            error = urllib.error.HTTPError("https://fixture.test", 401, "private provider detail", {}, None)
            with patch("urllib.request.urlopen", side_effect=error) as request:
                result = run(path, Backend(), self.planner())
            self.assertEqual(request.call_count, 1)
            self.assertEqual(result["status"], "blocked")
            self.assertFalse(result["last_error"]["retryable"])
            self.assertNotIn("private provider detail", json.dumps(result))

    def test_call_budget_prevents_a_retry(self):
        planner = self.planner(max_calls=1)
        with patch("urllib.request.urlopen", side_effect=TimeoutError) as request, patch("time.sleep"):
            with self.assertRaises(TokenBudgetExceeded): planner._request([])
        self.assertEqual(request.call_count, 1)

    def test_circuit_blocks_subsequent_requests_without_dispatch(self):
        planner = self.planner()
        with patch("urllib.request.urlopen", side_effect=TimeoutError) as request, patch("time.sleep"):
            with self.assertRaises(ModelRequestError): planner._request([])
            self.assertEqual(request.call_count, 3)
            with self.assertRaisesRegex(ModelRequestError, "provider_circuit_open"): planner._request([])
            self.assertEqual(request.call_count, 3)

    def test_failover_preserves_required_vision_capability(self):
        profiles = [ModelProfile(name, name, f"https://{name}.test/v1/chat/completions", "FIXTURE_KEY",
                                 frozenset(caps), tier) for name, caps, tier in
                    [("a", {"chat", "vision"}, 1), ("b", {"chat"}, 1), ("c", {"chat", "vision"}, 2)]]
        planner = self.planner(model_router=ModelRouter(profiles, "a"), model_attempts=1)
        with patch.dict(os.environ, {"FIXTURE_KEY": "route-key"}), patch("urllib.request.urlopen", side_effect=[TimeoutError(), response()]) as request:
            planner._request([], vision=True)
        self.assertEqual([call.args[0].full_url for call in request.call_args_list],
                         [profiles[0].endpoint, profiles[2].endpoint])
        self.assertEqual(planner.usage["route_failovers"], 1)

    def test_cost_budget_requires_prices_and_records_known_cost(self):
        with patch("urllib.request.urlopen") as request:
            with self.assertRaises(TokenBudgetExceeded): self.planner(max_cost_usd=1)._request([])
            request.assert_not_called()
        planner = self.planner(max_cost_usd=.001, input_price_per_million=10, output_price_per_million=10)
        with patch("urllib.request.urlopen", return_value=response()) as request:
            planner._request([])
        self.assertEqual(planner.usage["cost_microusd"], 120)
        self.assertLess(json.loads(request.call_args.args[0].data)["max_tokens"], 100)
        planner.usage["cost_microusd"] = 1000
        with self.assertRaises(TokenBudgetExceeded): planner._request([])

    def test_deadline_never_dispatches_tool_after_slow_observation(self):
        clock = [100.0]
        class Backend:
            def observe(self):
                clock[0] += .1
                return {"summary": "ready"}
            def act(self, action): raise AssertionError("Tool must not dispatch")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            create(path, "Inspect", ["ready"])
            with patch("time.monotonic", side_effect=lambda: clock[0]):
                result = run(path, Backend(), self.planner(max_run_seconds=.05))
            self.assertEqual(result["stop_reason"], "run_budget_exhausted")
            self.assertIsNone(result["pending_action"])
            self.assertGreater(result["elapsed_run_ms"], 0)

    def test_invalid_model_json_is_classified_without_retry(self):
        planner = self.planner()
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b'not json')) as request:
            with self.assertRaisesRegex(ModelRequestError, "invalid_model_response"): planner._request([])
        self.assertEqual(request.call_count, 1)

    def test_nonfinite_budgets_are_rejected(self):
        for settings in ({"max_run_seconds": float("nan")}, {"max_cost_usd": float("inf")},
                         {"model_attempts": 0}, {"model_fallbacks": -1}):
            with self.assertRaises(ValueError): self.planner(**settings)
