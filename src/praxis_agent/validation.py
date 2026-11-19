"""Validate advertised tool contracts locally before write-ahead dispatch.

External schema references are never fetched. A backend with no tool directory
retains the legacy dictionary adapter contract, without schema-validation claims.
"""
from functools import lru_cache
import json
import re
from jsonschema.validators import validator_for
from referencing import Registry
from referencing.exceptions import NoSuchResource


class InvalidToolAction(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _offline(uri):
    raise NoSuchResource(ref=uri)


@lru_cache(maxsize=128)
def _validator(encoded):
    schema = json.loads(encoded)
    cls = validator_for(schema, default=None) if isinstance(schema, dict) and "$schema" in schema else validator_for(schema)
    if cls is None:
        raise InvalidToolAction("unsupported_schema_dialect")
    cls.check_schema(schema)
    return cls(schema, registry=Registry(retrieve=_offline))


def validate_action(action, tools=None):
    if (not isinstance(action, dict) or not isinstance(action.get("name"), str)
            or not 1 <= len(action["name"]) <= 256 or re.search(r"[\x00-\x1f\x7f]", action["name"])
            or not isinstance(action.get("arguments", {}), dict)
            or (action.get("impact") is not None and action.get("impact") not in ("low", "high"))):
        raise InvalidToolAction("invalid_action")
    arguments = action.get("arguments", {})
    try:
        json.dumps(action, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise InvalidToolAction("non_json_action") from exc
    if tools is None:
        return
    if not isinstance(tools, list):
        raise InvalidToolAction("invalid_tool_directory")
    matches = [tool for tool in tools if isinstance(tool, dict) and tool.get("name") == action["name"]]
    if len(matches) != 1:
        raise InvalidToolAction("unknown_or_ambiguous_tool")
    schema = matches[0].get("inputSchema", {})
    try:
        encoded = json.dumps(schema, sort_keys=True, allow_nan=False)
        if len(encoded) > 128_000:
            raise InvalidToolAction("tool_schema_too_large")
        validator = _validator(encoded)
        if next(validator.iter_errors(arguments), None) is not None:
            raise InvalidToolAction("tool_arguments_mismatch")
    except InvalidToolAction:
        raise
    except Exception as exc:
        # Schema exception text may contain argument values; never expose it.
        raise InvalidToolAction("invalid_or_unresolvable_tool_schema") from exc
