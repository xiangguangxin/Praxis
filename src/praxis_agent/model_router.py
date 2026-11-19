"""Deterministic model profile routing; secrets stay outside serializable profiles."""
from dataclasses import dataclass
@dataclass(frozen=True)
class ModelProfile:
    name: str
    model: str
    endpoint: str
    api_key_env: str
    capabilities: frozenset[str]
    max_cost_tier: int = 1
class ModelRouter:
    def __init__(self, profiles, default):
        self.profiles={p.name:p for p in profiles}
        if default not in self.profiles: raise ValueError("Unknown default profile")
        if len(self.profiles)!=len(profiles): raise ValueError("Duplicate profile")
        self.default=default
    def select(self, *, task_type="general", requires_vision=False, risk="low", preferred=None):
        if risk not in {"low","medium","high"}: raise ValueError("Invalid risk")
        required={"chat"} | ({"vision"} if requires_vision else set())
        candidates=[p for p in self.profiles.values() if required <= p.capabilities]
        if preferred:
            p=self.profiles.get(preferred)
            if p not in candidates: raise ValueError("Preferred profile lacks capabilities")
            return p
        if not candidates: raise LookupError("No compatible model profile")
        candidates.sort(key=lambda p: ((-p.max_cost_tier if risk=="high" else p.max_cost_tier),p.name))
        return candidates[0]
    def describe(self):
        return [{"name":p.name,"model":p.model,"capabilities":sorted(p.capabilities),"max_cost_tier":p.max_cost_tier} for p in self.profiles.values()]
