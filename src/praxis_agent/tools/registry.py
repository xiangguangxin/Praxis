"""Allowlisted tool contracts; runtime approval is separate from registration."""
from dataclasses import dataclass
from typing import Callable

class ToolError(ValueError):
    pass

@dataclass(frozen=True)
class ToolSpec:
    name: str
    handler: Callable
    parameters: dict
    impact: str = "high"
    timeout_seconds: float = 30.0
    idempotent: bool = False

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip() or not callable(self.handler):
            raise ToolError("Invalid tool name or handler")
        if self.impact not in {"low", "medium", "high"} or not 0 < self.timeout_seconds <= 3600:
            raise ToolError("Invalid tool policy")
        if not isinstance(self.parameters, dict) or any(not isinstance(k, str) or not isinstance(v, type) for k, v in self.parameters.items()):
            raise ToolError("Invalid parameter types")

class ToolRegistry:
    def __init__(self):
        self._tools = {}

    def register(self, spec):
        if spec.name in self._tools:
            raise ToolError("Duplicate tool")
        self._tools[spec.name] = spec

    def resolve(self, name):
        if name not in self._tools:
            raise ToolError("Unknown tool")
        return self._tools[name]

    def describe(self):
        return [{"name": s.name, "impact": s.impact, "idempotent": s.idempotent,
                 "timeout_seconds": s.timeout_seconds, "parameters": {k: v.__name__ for k, v in s.parameters.items()}}
                for s in self._tools.values()]

    def dispatch(self, action, *, approved=False):
        if not isinstance(action, dict):
            raise ToolError("Action must be a dict")
        spec = self.resolve(action.get("name"))
        args = action.get("arguments", {})
        if not isinstance(args, dict) or set(args) != set(spec.parameters):
            raise ToolError("Tool arguments mismatch")
        if any(type(args[k]) is not kind for k, kind in spec.parameters.items()):
            raise ToolError("Tool argument type mismatch")
        if spec.impact != "low" and not approved:
            raise PermissionError("Tool requires explicit approval")
        result = spec.handler(dict(args))
        if not isinstance(result, dict):
            raise ToolError("Tool result must be a dict")
        return result

class RegisteredToolsBackend:
    """Desktop protocol adapter. Only low-impact tools are dispatched automatically."""
    def __init__(self, registry, observe):
        self.registry, self._observe = registry, observe

    def observe(self):
        observation = self._observe()
        if not isinstance(observation, dict):
            raise ToolError("Observation must be a dict")
        return {**observation, "tools": self.registry.describe()}

    def act(self, action):
        return self.registry.dispatch(action)
