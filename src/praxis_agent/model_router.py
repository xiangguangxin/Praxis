"""Capability- and risk-aware routing across configured OpenAI-compatible models."""
from dataclasses import dataclass
import json
from pathlib import Path
from urllib.parse import urlsplit

@dataclass(frozen=True)
class ModelProfile:
    name: str
    model: str
    endpoint: str
    api_key_env: str
    capabilities: frozenset[str]
    max_cost_tier: int = 1
    task_types: frozenset[str] = frozenset()

    def __post_init__(self):
        p=urlsplit(self.endpoint)
        if not self.name or not self.model or not self.api_key_env.isidentifier(): raise ValueError("Invalid model profile")
        if p.scheme not in {"http","https"} or not p.hostname or p.username or p.password or p.query or p.fragment: raise ValueError("Invalid model endpoint")
        if not self.capabilities or not self.capabilities <= {"chat","vision","json"}: raise ValueError("Invalid model capabilities")
        if type(self.max_cost_tier) is not int or not 1 <= self.max_cost_tier <= 5: raise ValueError("Invalid cost tier")
        allowed_tasks = {"planning","execution","visual_inspection","verification","debugging","code","research","general"}
        if not isinstance(self.task_types, frozenset) or not self.task_types <= allowed_tasks:
            raise ValueError("Invalid task type specialization")

class ModelRouter:
    def __init__(self, profiles, default):
        self.profiles={p.name:p for p in profiles}
        if default not in self.profiles: raise ValueError("Unknown default profile")
        if len(self.profiles)!=len(profiles): raise ValueError("Duplicate profile")
        self.default=default
    @classmethod
    def from_json(cls,path):
        data=json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data,dict) or set(data)!={"default","profiles"} or not isinstance(data["profiles"],list): raise ValueError("Invalid model profile configuration")
        profiles=[]
        for item in data["profiles"]:
            if not isinstance(item,dict) or set(item)-{"name","model","endpoint","api_key_env","capabilities","max_cost_tier","task_types"}: raise ValueError("Invalid profile fields")
            profiles.append(ModelProfile(item["name"],item["model"],item["endpoint"],item["api_key_env"],frozenset(item["capabilities"]),item.get("max_cost_tier",1),frozenset(item.get("task_types",[]))))
        return cls(profiles,data["default"])
    def select(self, *, task_type="general", requires_vision=False, risk="low", preferred=None):
        if risk not in {"low","medium","high"}: raise ValueError("Invalid risk")
        required={"chat"} | ({"vision"} if requires_vision else set())
        candidates=[p for p in self.profiles.values() if required <= p.capabilities]
        if preferred:
            p=self.profiles.get(preferred)
            if p not in candidates: raise ValueError("Preferred profile lacks required capabilities")
            return p
        if not candidates: raise LookupError("No compatible model profile")
        if task_type not in {"planning","execution","visual_inspection","verification","debugging","code","research","general"}:
            raise ValueError("Invalid task type")
        specialized=[p for p in candidates if task_type in p.task_types]
        generic=[p for p in candidates if not p.task_types]
        candidates=specialized or generic or candidates
        candidates.sort(key=lambda p: ((-p.max_cost_tier if risk=="high" else p.max_cost_tier),p.name))
        return candidates[0]
    def describe(self):
        return [{"name":p.name,"model":p.model,"capabilities":sorted(p.capabilities),"max_cost_tier":p.max_cost_tier,"task_types":sorted(p.task_types)} for p in self.profiles.values()]
