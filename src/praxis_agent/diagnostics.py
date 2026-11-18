"""Bounded diagnostics that do not retain raw exception or provider messages."""
import re
from urllib.parse import urlsplit, urlunsplit


def safe_summary(value, limit=200):
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value))
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer [redacted]", text)
    text = re.sub(r"(?i)(password|token|secret|api[_-]?key)\s*[:=]\s*\S+", r"\1=[redacted]", text)
    def url(match):
        try:
            parts = urlsplit(match.group())
            return urlunsplit((parts.scheme, parts.hostname or "", parts.path, "", ""))
        except ValueError:
            return "[redacted-url]"
    return re.sub(r"https?://[^\s]+", url, text)[:limit]


def error_detail(exc, phase, pending=False):
    if isinstance(exc, TimeoutError):
        code, message = "timeout", f"{phase} timed out"
    elif isinstance(exc, (ValueError, TypeError, KeyError, IndexError)):
        code, message = "invalid_data", f"Invalid {phase} data or configuration"
    elif isinstance(exc, OSError):
        code, message = "transport_error", f"{phase} transport or filesystem failed"
    else:
        code, message = "unexpected_error", f"Unexpected failure during {phase}"
    return {"code": code, "phase": phase, "exception_type": type(exc).__name__, "message": message,
            "may_have_executed": pending, "retryable": not pending,
            "recovery": ("Inspect the target, then use praxis reconcile with the observed outcome."
                         if pending else "Correct the backend/model configuration, then use praxis retry if blocked.")}
