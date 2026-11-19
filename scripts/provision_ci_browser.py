"""Use an existing CI browser, downloading Playwright Chromium only if necessary."""
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    executable = next((path for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
                       if (path := shutil.which(name))), None)
    if executable is None:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                       check=True, timeout=180)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            executable = playwright.chromium.executable_path
    if not Path(executable).is_file() or "\n" in executable or "\r" in executable:
        raise RuntimeError("Expected a locally installed browser executable")
    with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as stream:
        stream.write(f"OPENAS_TEST_BROWSER_EXECUTABLE={executable}\n")
    print(f"CI browser executable: {executable}")


if __name__ == "__main__":
    main()
