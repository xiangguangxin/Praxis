"""Cross-process locks for named external resources used by independent tasks."""
import hashlib
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from ..locking import checkpoint_lock

class ResourceLockTimeout(TimeoutError):
    pass

class TaskLockManager:
    def __init__(self, directory, *, timeout=300, poll_interval=0.05):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("resource lock timeout must be positive")
        if isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float)) or poll_interval <= 0:
            raise ValueError("resource lock poll interval must be positive")
        self.directory = Path(directory)
        self.timeout, self.poll_interval = float(timeout), float(poll_interval)

    @contextmanager
    def acquire(self, resources):
        if not isinstance(resources, (tuple, list)) or any(not isinstance(r, str) or not r.strip() for r in resources):
            raise ValueError("resources must be nonempty strings")
        names = sorted(set(resources))
        deadline = time.monotonic() + self.timeout
        with ExitStack() as stack:
            for name in names:
                digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
                path = self.directory / (digest + ".resource")
                while True:
                    try:
                        stack.enter_context(checkpoint_lock(path))
                        break
                    except RuntimeError as exc:
                        if time.monotonic() >= deadline:
                            raise ResourceLockTimeout("Timed out waiting for a task resource lock") from exc
                        time.sleep(min(self.poll_interval, max(0, deadline-time.monotonic())))
            yield
