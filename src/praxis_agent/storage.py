"""Atomic JSON publication with explicit create-only and directory durability."""
import json
import os
from pathlib import Path
import tempfile


def atomic_json_write(path: Path, data, *, create_only=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".praxis-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if create_only:
            # Hard-link publication fails if a competing writer created the target.
            # Unlike replace(), this cannot overwrite an existing file or symlink.
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        if os.name != "nt":
            folder = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(folder)
            finally:
                os.close(folder)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
