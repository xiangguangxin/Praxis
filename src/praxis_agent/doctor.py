"""Local readiness checks; no model calls, browser launch or desktop actions."""
import importlib.util
import os
from pathlib import Path
import shutil
import sys
from urllib.parse import urlsplit
from .model_connection import resolve_model_connection


def doctor(args):
    checks = [{"name": "python", "passed": sys.version_info >= (3, 10), "detail": sys.version.split()[0]}]
    if getattr(args, "browser", False) or getattr(args, "browser_executable", None):
        installed = importlib.util.find_spec("playwright") is not None
        checks.append({"name": "playwright", "passed": installed,
                       "detail": "installed" if installed else 'Install praxis-agent-runtime[browser]'})
        executable = getattr(args, "browser_executable", None)
        if not executable and installed:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                executable = playwright.chromium.executable_path
        ready = bool(executable and Path(executable).is_file() and os.access(executable, os.X_OK))
        checks.append({"name": "chromium", "passed": ready,
                       "detail": "executable found" if ready else "Run python -m playwright install chromium or pass --browser-executable"})
    if getattr(args, "nova", None):
        ready = bool(shutil.which(args.nova))
        checks.append({"name": "nova", "passed": ready,
                       "detail": "executable found; authorization not checked" if ready else "Nova executable not found"})
    if getattr(args, "check_model", False):
        try:
            connection = resolve_model_connection(args)
            parts = urlsplit(connection.endpoint)
            valid = parts.scheme in {"http", "https"} and bool(parts.hostname) and parts.username is None and parts.password is None
            checks.append({"name": "model_configuration", "passed": valid,
                           "detail": "credentials and route configured; connectivity and vision support not checked"})
        except (ValueError, OSError):
            checks.append({"name": "model_configuration", "passed": False,
                           "detail": "Configure model, endpoint and API key; see praxis doctor --help"})
    return {"healthy": all(check["passed"] for check in checks), "checks": checks,
            "scope": "local-readiness-only; no provider or backend requests"}
