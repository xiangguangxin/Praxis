"""Live desktop evidence is transient and part of the observed view identity."""
from .adapters.nova import NovaMCP
from .planner import ModelPlanner


class LiveDesktop:
    def __init__(self, nova: NovaMCP, planner: ModelPlanner):
        self.nova, self.planner = nova, planner
        self.backend_version = getattr(nova, "backend_version", None)
        self._latest_content = []

    def observe(self):
        observation = dict(self.nova.observe())
        if "content" not in observation:
            # Manual observation results must participate in view fingerprints,
            # rather than existing only inside the planner's private context.
            observation["content"] = self._latest_content
        elif self._latest_content:
            observation["tool_result_content"] = [item for item in self._latest_content
                                                  if isinstance(item, dict) and item.get("type") == "text"][:2]
        self.planner.last_tool_content = observation["content"]
        return observation

    def act(self, action):
        result = self.nova.act(action)
        # Replace even on an empty result: do not carry an old screenshot over a write.
        self._latest_content = result.pop("content", [])[:8]
        self.planner.last_tool_content = self._latest_content
        return result
