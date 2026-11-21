"""Small embedding API around the same CLI execution, approval and recovery gates."""
from pathlib import Path
from .agent import run_session
from .policy import ActionPolicy
from .state import create


class Runtime:
    def __init__(self, planner, backend_factory, *, backend_scope, verifier=None, policy=None, journal=None):
        if not callable(backend_factory): raise ValueError("Context-managed backend factory required")
        self.planner, self.backend_factory, self.backend_scope = planner, backend_factory, backend_scope
        self.verifier, self.policy, self.journal = verifier, policy or ActionPolicy(), journal

    @classmethod
    def tools(cls, config, planner, **kwargs):
        from .adapters.tool_backend import config_identity, open_tool_backend
        config = Path(config).resolve(strict=True)
        scope = config_identity(config)
        return cls(planner, lambda: open_tool_backend(config, expected_identity=scope), backend_scope=scope, **kwargs)

    def execute(self, checkpoint, *, goal=None, criteria=None, max_steps=40, approve_pending=False, approval_id=None):
        checkpoint = Path(checkpoint)
        if goal is not None:
            create(checkpoint, goal, criteria, max_steps=max_steps)
        return run_session(checkpoint, self.backend_factory, self.planner, verifier=self.verifier,
                           policy=self.policy, backend_scope=self.backend_scope, trace_tools=True,
                           approval=approve_pending, approval_id=approval_id, journal=self.journal)
