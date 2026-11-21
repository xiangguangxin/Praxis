"""Planner request accounting, compatible-profile failover and explicit budgets."""
import json
import math
import time
import urllib.request
from .model_transport import ModelTransport, RequestPolicy, ModelRequestError, RequestBudgetExceeded


class TokenBudgetExceeded(RequestBudgetExceeded):
    pass


def add_request_arguments(parser):
    parser.add_argument("--model-attempts", type=int, default=3)
    parser.add_argument("--model-timeout", type=float, default=90)
    parser.add_argument("--model-fallbacks", type=int, default=1)
    parser.add_argument("--max-run-seconds", type=float, default=0)
    parser.add_argument("--max-cost-usd", type=float, default=0)
    parser.add_argument("--input-price-per-million", type=float)
    parser.add_argument("--output-price-per-million", type=float)


def request_options(args):
    return {name: getattr(args, name, default) for name, default in {
        "model_attempts": 3, "model_timeout": 90, "model_fallbacks": 1, "max_run_seconds": 0,
        "max_cost_usd": 0, "input_price_per_million": None, "output_price_per_million": None}.items()}


class ResilientRequests:
    def configure_requests(self, *, model_attempts=3, model_timeout=90, model_fallbacks=1,
                           max_run_seconds=0, max_cost_usd=0, input_price_per_million=None,
                           output_price_per_million=None):
        for value in (max_run_seconds, max_cost_usd):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("Budgets must be finite and nonnegative")
        if type(model_fallbacks) is not int or not 0 <= model_fallbacks <= 4:
            raise ValueError("Model fallbacks must be 0..4")
        prices = (input_price_per_million, output_price_per_million)
        if (prices[0] is None) != (prices[1] is None): raise ValueError("Both token prices are required")
        if any(p is not None and (type(p) not in (int, float) or not math.isfinite(p) or p < 0) for p in prices):
            raise ValueError("Token prices must be finite and nonnegative")
        self.transport = ModelTransport(RequestPolicy(max_attempts=model_attempts, timeout_seconds=model_timeout))
        self.model_fallbacks, self.max_run_seconds = model_fallbacks, max_run_seconds
        self.max_cost_microusd, self.request_prices = int(max_cost_usd * 1_000_000), prices
        if max_cost_usd and not self.max_cost_microusd:
            raise ValueError("Minimum cost budget is one microdollar")
        self.request_deadline = None

    def set_run_deadline(self, seconds):
        self.request_deadline = time.monotonic() + max(0, seconds)

    def _request(self, messages, *, vision=False, risk="low", max_tokens=None, task_type="general"):
        excluded = set()
        estimate = max(1, len(json.dumps(messages, ensure_ascii=False)) // 4)
        for fallback in range(self.model_fallbacks + 1):
            model, api_key, endpoint, route = self._route(vision, risk, task_type, exclude=excluded)
            prices = self.request_prices
            if self.model_router is not None:
                profile = self.model_router.profiles[route]
                if profile.input_price_per_million is not None:
                    prices = (profile.input_price_per_million, profile.output_price_per_million)
            if self.max_cost_microusd and prices[0] is None:
                raise TokenBudgetExceeded("Cost budget requires explicit prices for every selected model")
            output_limit = min(max_tokens or 2048, 2048)
            if self.max_tokens:
                spent = sum(self.usage.get(k, 0) for k in ("input_tokens", "output_tokens", "estimated_input_tokens", "estimated_output_tokens"))
                output_limit = min(output_limit, self.max_tokens - spent - estimate)
            if self.max_cost_microusd and prices[1]:
                available = self.max_cost_microusd - self.usage.get("cost_microusd", 0) - estimate*prices[0]
                output_limit = min(output_limit, int(available // prices[1]))
            if output_limit < 1: raise TokenBudgetExceeded("Insufficient remaining request budget")
            payload = {"model": model, "messages": messages, "temperature": 0,
                       "response_format": {"type": "json_object"}, "max_tokens": output_limit}
            request = urllib.request.Request(endpoint, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key}, method="POST")
            def bump(name, count=1): self.usage[name] = self.usage.get(name, 0) + count
            def charge(inputs, outputs, estimated=False):
                if prices[0] is None:
                    bump("cost_unknown_calls")
                else:
                    bump("cost_microusd", math.ceil(inputs*prices[0] + outputs*prices[1]))
                    if estimated: bump("cost_estimated_calls")
            reserve = math.ceil(estimate*prices[0] + output_limit*prices[1]) if prices[0] is not None else 0
            def before_attempt():
                spent = sum(self.usage.get(k, 0) for k in ("input_tokens", "output_tokens", "estimated_input_tokens", "estimated_output_tokens"))
                if self.max_calls and self.usage.get("calls", 0) >= self.max_calls:
                    raise TokenBudgetExceeded("Model call budget exhausted")
                if self.max_tokens and spent + estimate + output_limit > self.max_tokens:
                    raise TokenBudgetExceeded("Token budget prevents another attempt")
                if self.max_cost_microusd and self.usage.get("cost_microusd", 0) + reserve > self.max_cost_microusd:
                    raise TokenBudgetExceeded("Cost budget exhausted")
                bump("calls")
                for label in ("task_" + task_type, "profile_" + route): bump("route_" + label + "_calls")
            def failed_attempt():
                bump("usage_missing_calls")
                bump("estimated_input_tokens", estimate)
                bump("estimated_output_tokens", output_limit)
                charge(estimate, output_limit, True)
            started = time.monotonic()
            try:
                result = self.transport.request(request, before_attempt=before_attempt,
                    failed_attempt=failed_attempt, retry=lambda: bump("retries"), deadline=self.request_deadline)
            except RequestBudgetExceeded as exc:
                raise TokenBudgetExceeded(str(exc)) from None
            except ModelRequestError as exc:
                if not exc.retryable or self.model_router is None or fallback == self.model_fallbacks: raise
                excluded.add(route)
                try:
                    self.model_router.select(task_type=task_type, requires_vision=vision, risk=risk, exclude=excluded)
                except LookupError:
                    raise exc from None
                bump("route_failovers")
                continue
            finally:
                bump("model_elapsed_ms", max(0, round((time.monotonic() - started) * 1000)))
            usage = result.get("usage", {}) if isinstance(result, dict) else {}
            if (isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0
                                              for k in ("prompt_tokens", "completion_tokens"))):
                bump("input_tokens", usage["prompt_tokens"])
                bump("output_tokens", usage["completion_tokens"])
                charge(usage["prompt_tokens"], usage["completion_tokens"])
            else:
                failed_attempt()
            try:
                decision = json.loads(result["choices"][0]["message"]["content"])
                if not isinstance(decision, dict): raise ValueError("Expected decision object")
                return decision
            except (ValueError, TypeError, KeyError, IndexError):
                raise ModelRequestError("invalid_model_response") from None
