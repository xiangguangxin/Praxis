"""Independent acceptance registry; does not trust model-provided completion."""
class VerificationError(ValueError):
    pass

class VerificationRegistry:
    def __init__(self):
        self._checks = {}

    def register(self, name, check):
        if not isinstance(name, str) or not name.strip() or not callable(check) or name in self._checks:
            raise VerificationError("Verifier must be unique and callable")
        self._checks[name] = check

    def evaluate(self, criteria):
        if not isinstance(criteria, dict) or not criteria:
            raise VerificationError("Nonempty criteria mapping required")
        checks = {}
        for name, rule in criteria.items():
            if not isinstance(rule, dict) or rule.get("verifier") not in self._checks:
                checks[name] = {"passed": False, "status": "unknown_verifier"}
                continue
            try:
                params = rule.get("parameters", {})
                if not isinstance(params, dict):
                    raise VerificationError("Parameters must be a dict")
                evidence = self._checks[rule["verifier"]](dict(params))
                if not isinstance(evidence, dict) or type(evidence.get("passed")) is not bool:
                    raise VerificationError("Verifier must return passed: bool")
                checks[name] = {"passed": evidence["passed"], "status": "passed" if evidence["passed"] else "failed",
                                "evidence": evidence.get("evidence", {})}
            except Exception as exc:
                checks[name] = {"passed": False, "status": "error", "error": type(exc).__name__}
        return {"passed": all(c["passed"] for c in checks.values()), "checks": checks}

class CriteriaVerifier:
    """Opt-in independent verification for the existing agent completion gate."""
    def __init__(self, registry, rules):
        if not isinstance(registry, VerificationRegistry) or not isinstance(rules, dict) or not rules:
            raise VerificationError("Registry and nonempty rules required")
        self.registry, self.rules = registry, rules

    def prepare(self, state):
        if set(state["criteria"]) != set(self.rules):
            raise VerificationError("All checkpoint criteria require rules")

    def evaluate(self, state):
        self.prepare(state)
        return self.registry.evaluate(self.rules)
