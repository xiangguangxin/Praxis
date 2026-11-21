"""Allowlisted tool contracts; runtime approval is separate from registration."""
from dataclasses import dataclass
from typing import Callable
import json
import math
import re
from ..validation import validate_action

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
    input_schema: dict | None = None
    description: str = ""
    preflight: Callable | None = None

    def __post_init__(self):
        if not isinstance(self.name, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", self.name) or not callable(self.handler):
            raise ToolError("Invalid tool name or handler")
        if (self.impact not in {"low", "medium", "high"} or type(self.timeout_seconds) not in (int, float)
                or not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 3600):
            raise ToolError("Invalid tool policy")
        if not isinstance(self.parameters, dict) or any(not isinstance(k, str) or not isinstance(v, type) for k, v in self.parameters.items()):
            raise ToolError("Invalid parameter types")
        if (self.input_schema is not None and not isinstance(self.input_schema, dict)
                or self.preflight is not None and not callable(self.preflight)
                or type(self.idempotent) is not bool or not isinstance(self.description, str)):
            raise ToolError("Invalid tool schema or preflight")
        supported = {str: "string", int: "integer", float: "number", bool: "boolean", dict: "object", list: "array"}
        if self.input_schema is None and any(kind not in supported for kind in self.parameters.values()):
            raise ToolError("Parameter type has no supported JSON representation")

    def schema(self):
        if self.input_schema is not None:
            return json.loads(json.dumps(self.input_schema, allow_nan=False))
        names = {str: "string", int: "integer", float: "number", bool: "boolean", dict: "object", list: "array"}
        return {"type": "object", "properties": {key: {"type": names[kind]} for key, kind in self.parameters.items()},
                "required": list(self.parameters), "additionalProperties": False}

class ToolRegistry:
    def __init__(self):
        self._tools = {}

    def register(self, spec):
        if not isinstance(spec, ToolSpec): raise ToolError("ToolSpec required")
        if spec.name in self._tools:
            raise ToolError("Duplicate tool")
        self._tools[spec.name] = spec

    def resolve(self, name):
        if name not in self._tools:
            raise ToolError("Unknown tool")
        return self._tools[name]

    def describe(self):
        return [{"name": s.name, "impact": s.impact, "idempotent": s.idempotent,
                 "description": s.description, "inputSchema": s.schema(),
                 "timeout_seconds": s.timeout_seconds, "parameters": {k: v.__name__ for k, v in s.parameters.items()}}
                for s in self._tools.values()]

    def validate(self, action):
        if not isinstance(action, dict):
            raise ToolError("Action must be a dict")
        spec = self.resolve(action.get("name"))
        args = action.get("arguments", {})
        if not isinstance(args, dict) or (spec.input_schema is None and set(args) != set(spec.parameters)):
            raise ToolError("Tool arguments mismatch")
        if spec.input_schema is None and any(type(args[k]) is not kind for k, kind in spec.parameters.items()):
            raise ToolError("Tool argument type mismatch")
        validate_action(action, self.describe())
        if spec.preflight is not None: spec.preflight(dict(args))
        return spec, args

    def dispatch(self, action, *, approved=False):
        spec, args = self.validate(action)
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
        self._latest_content = []

    def observe(self):
        observation = self._observe()
        if not isinstance(observation, dict):
            raise ToolError("Observation must be a dict")
        return {"content": self._latest_content, **observation, "tools": self.registry.describe()}

    def validate_action(self, action):
        self.registry.validate(action)

    def action_impact(self, action):
        return "low" if self.registry.resolve(action["name"]).impact == "low" else "high"

    def act(self, action):
        result = self.registry.dispatch(action)
        self._latest_content = result.get("content", [])
        return result

    def act_authorized(self, action):
        """Host runtime entry point, called only after policy and approval checks."""
        result = self.registry.dispatch(action, approved=True)
        self._latest_content = result.get("content", [])
        return result
