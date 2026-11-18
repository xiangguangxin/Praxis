"""Host-side action policy and reviewable, payload-free approval requests.

This is an authorization aid, not a sandbox or a guarantee of semantic UI safety.
"""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

SENSITIVE = {"delete", "overwrite", "send", "pay", "install", "permissions", "export", "commit",
             "publish", "purchase", "transfer", "submit", "remove", "上传", "删除", "发送", "付款",
             "支付", "购买", "提交", "发布", "授权", "覆盖", "转账", "安装"}
_SECRET = re.compile(r"password|passwd|secret|token|api.?key|authorization|cookie|credential", re.I)
_SAFE_TARGETS = {"role", "name", "label", "filename", "url"}


def fingerprint(action):
    return hashlib.sha256(json.dumps(action, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _sensitive(value):
    text = str(value).casefold()
    return any((word in text if not word.isascii() else
                re.search(r"(?<![a-z])" + word + r"(?![a-z])", text)) for word in SENSITIVE)


def review_arguments(arguments):
    """Only expose bounded control locators; never persist text, commands or credentials."""
    result = {}
    for key, value in arguments.items():
        if key not in _SAFE_TARGETS or _SECRET.search(key):
            result[key[:64]] = "[redacted]"
            continue
        text = str(value)
        if key == "url":
            parts = urlsplit(text)
            text = urlunsplit((parts.scheme, parts.hostname or "", parts.path, "", ""))
        result[key] = re.sub(r"[\x00-\x1f\x7f]", " ", text)[:160]
    return result


class ActionPolicy:
    """Operator-authored exact-match rules, evaluated before heuristic classification.

    Rules match the entire argument object, so an allow for one target cannot
    authorize another target or an additional command argument.
    """
    def __init__(self, rules=()):
        self.rules = list(rules)
        for rule in self.rules:
            if (not isinstance(rule, dict) or set(rule) != {"tool", "arguments", "effect"}
                    or not isinstance(rule["tool"], str) or not rule["tool"]
                    or not isinstance(rule["arguments"], dict)
                    or rule["effect"] not in {"allow", "approve", "deny"}):
                raise ValueError("Policy rules require tool, exact arguments and allow/approve/deny effect")
        self.identity = fingerprint(self.rules)

    @classmethod
    def from_json(cls, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != {"rules"} or not isinstance(data["rules"], list):
            raise ValueError("Policy JSON requires a rules list")
        return cls(data["rules"])

    def classify(self, action):
        name, args = action.get("name", ""), action.get("arguments", {})
        matches = [rule for rule in self.rules if rule["tool"] == name and rule["arguments"] == args]
        # Denial always wins, even over an exact allow rule or an existing approval.
        for effect in ("deny", "approve", "allow"):
            if any(rule["effect"] == effect for rule in matches):
                return effect, "operator_policy_" + effect
        if action.get("impact") != "low":
            return "approve", "unclassified_or_high_impact"
        if _sensitive(name):
            return "approve", "sensitive_tool"
        # Input data is not a control target. Inspect locator/command parameters,
        # rather than treating a sentence typed into a test field as an action.
        targets = [value for key, value in args.items() if key not in {"text", "value"}]
        if any(_sensitive(value) for value in targets):
            return "approve", "sensitive_target_or_command"
        if any(key in args for key in ("command", "script", "code", "expression")):
            return "approve", "executable_payload"
        if any(re.search(r"(?<![a-z])(enter|return)(?![a-z])", str(args.get(key, "")).casefold())
               for key in ("key", "keys", "shortcut")):
            return "approve", "ambiguous_keyboard_submission"
        return "allow", "low_impact"


def needs_approval(action):
    return ActionPolicy().classify(action)[0] != "allow"
