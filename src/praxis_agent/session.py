"""Select the runtime backend shared by the interactive CLI and live evaluation."""
from contextlib import contextmanager

from .policy import fingerprint


def browser_scope(start_url, download_dir):
    return fingerprint({"browser_url": start_url, "download_dir": str(download_dir) if download_dir else None})


@contextmanager
def open_session(planner, *, nova=None, browser_url=None, download_dir=None, browser_executable=None,
                 tool_config=None, expected_backend_scope=None):
    if tool_config is not None:
        from .adapters.tool_backend import open_tool_backend
        with open_tool_backend(tool_config, expected_identity=expected_backend_scope) as backend:
            yield backend
    elif browser_url is not None:
        from .adapters.browser import open_browser
        with open_browser(browser_url, download_dir, browser_executable) as browser:
            yield browser
    else:
        from .adapters.nova import NovaMCP
        from .executor import LiveDesktop
        desktop = NovaMCP(nova)
        try:
            yield LiveDesktop(desktop, planner)
        finally:
            desktop.close()
