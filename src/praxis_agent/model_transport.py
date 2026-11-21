"""Bounded provider retries and endpoint circuit breakers; never retries tools."""
from dataclasses import dataclass
import json
import math
import time
import urllib.error
import urllib.request


class RequestBudgetExceeded(RuntimeError):
    pass


class ModelRequestError(RuntimeError):
    def __init__(self, code, *, retryable=False, status=None):
        self.code, self.retryable, self.status = code, retryable, status
        super().__init__(code)


@dataclass(frozen=True)
class RequestPolicy:
    max_attempts: int = 3
    timeout_seconds: float = 90
    base_delay: float = 0.25
    max_delay: float = 10
    breaker_threshold: int = 3
    breaker_cooldown: float = 30

    def __post_init__(self):
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 5:
            raise ValueError("Model attempts must be 1..5")
        if type(self.breaker_threshold) is not int or self.breaker_threshold < 1:
            raise ValueError("Invalid circuit threshold")
        for name in ("timeout_seconds", "base_delay", "max_delay", "breaker_cooldown"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("Invalid request timing policy")
        if self.timeout_seconds <= 0: raise ValueError("Positive model timeout required")


class ModelTransport:
    def __init__(self, policy=None):
        self.policy = policy or RequestPolicy()
        self.circuits = {}

    def request(self, request, *, before_attempt, failed_attempt, retry, deadline=None):
        endpoint = request.full_url
        failures, blocked_until = self.circuits.get(endpoint, (0, 0))
        if blocked_until > time.monotonic():
            raise ModelRequestError("provider_circuit_open", retryable=True)
        for attempt in range(self.policy.max_attempts):
            remaining = deadline-time.monotonic() if deadline is not None else self.policy.timeout_seconds
            if remaining <= 0: raise RequestBudgetExceeded("Run deadline exhausted")
            before_attempt()
            if attempt: retry()
            try:
                with urllib.request.urlopen(request, timeout=min(self.policy.timeout_seconds, remaining)) as response:
                    payload = response.read(4_000_001)
                if len(payload) > 4_000_000: raise ValueError("Model response exceeds size limit")
                result = json.loads(payload)
                self.circuits[endpoint] = (0, 0)
                return result
            except urllib.error.HTTPError as exc:
                transient = exc.code in {408, 429, 500, 502, 503, 504}
                error = ModelRequestError("provider_rate_limited" if exc.code == 429 else
                                          "provider_http_error", retryable=transient, status=exc.code)
                try: delay = float(exc.headers.get("Retry-After", 0)) if exc.headers else 0
                except (TypeError, ValueError): delay = 0
                exc.close()
            except (OSError, TimeoutError):
                error, delay = ModelRequestError("provider_transport_error", retryable=True), 0
            except (ValueError, TypeError):
                failed_attempt()
                raise ModelRequestError("invalid_model_response") from None
            failed_attempt()
            if not error.retryable: raise error from None
            failures += 1
            blocked = time.monotonic() + self.policy.breaker_cooldown if failures >= self.policy.breaker_threshold else 0
            self.circuits[endpoint] = (failures, blocked)
            if blocked or attempt + 1 == self.policy.max_attempts: raise error from None
            delay = min(self.policy.max_delay, max(delay if math.isfinite(delay) else 0,
                                                  self.policy.base_delay * 2**attempt))
            if deadline is not None and time.monotonic() + delay >= deadline:
                raise RequestBudgetExceeded("Run deadline prevents provider retry")
            time.sleep(delay)
