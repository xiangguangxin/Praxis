"""Normalize transient observations without checkpointing screen contents."""
import time
from .policy import fingerprint


def normalize(observation):
    if not isinstance(observation, dict):
        raise TypeError("Backend observation must be a dict")
    result = dict(observation)
    result.setdefault("observed_at", time.time())
    view = {key: result[key] for key in ("summary", "active_window", "window", "ui_tree", "elements", "action_scope")
            if key in result}
    if result.get("content"):
        view["content_id"] = fingerprint(result["content"])
    result.setdefault("observation_id", fingerprint(view))
    return result
