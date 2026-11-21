"""Exercise the actual HTTP request construction for each supported model route."""
import argparse
import io
import json
import os
import unittest
from unittest.mock import patch

from praxis_agent.model_connection import add_model_arguments, resolve_model_connection
from praxis_agent.model_router import ModelProfile, ModelRouter
from praxis_agent.planner import ModelPlanner


class RequestAuthenticationTests(unittest.TestCase):
    def test_direct_gateway_and_routed_requests_preserve_credentials(self):
        parser = argparse.ArgumentParser()
        add_model_arguments(parser)
        routes = [([], {"PRAXIS_MODEL": "direct", "PRAXIS_API_KEY": "fixture-direct"}, None),
                  (["--litellm-base-url", "https://fixture.test", "--model", "alias"],
                   {"LITELLM_API_KEY": "fixture-gateway"}, None),
                  ([], {"PRAXIS_MODEL": "base", "PRAXIS_API_KEY": "fixture-base",
                        "ROUTED_KEY": "fixture-routed"},
                   ModelRouter([ModelProfile("vision", "routed", "https://routed.test/v1/chat/completions",
                                             "ROUTED_KEY", frozenset({"chat", "vision"}))], "vision"))]
        for flags, environment, router in routes:
            with self.subTest(flags=flags, routed=router is not None), patch.dict(os.environ, environment, clear=True):
                connection = resolve_model_connection(parser.parse_args(flags))
                planner = ModelPlanner(connection.model, connection.api_key, connection.endpoint, model_router=router)
                def respond(request, **kwargs):
                    expected = environment["ROUTED_KEY"] if router else connection.api_key
                    self.assertEqual(request.get_header("Authorization"), "Bearer " + expected)
                    self.assertEqual(request.full_url, router.profiles["vision"].endpoint if router else connection.endpoint)
                    payload = json.loads(request.data)
                    self.assertEqual(payload["model"], "routed" if router else connection.model)
                    return io.StringIO(json.dumps({"choices": [{"message": {"content": '{"type":"pause"}'}}],
                                                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))
                with patch("urllib.request.urlopen", side_effect=respond):
                    self.assertEqual(planner._request([], vision=True)["type"], "pause")
                self.assertEqual(planner.usage["calls"], 1)
                self.assertEqual(planner.usage["route_task_general_calls"], 1)
