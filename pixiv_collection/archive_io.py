"""Small atomic JSON writer for archive commits and resumable local state."""

import json
import os
import tempfile
from pathlib import Path


def atomic_json(path, value, *, indent=None, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    # Atomic replacement must not turn an existing receiver-readable archive
    # into a new root-only file. Private sync state defaults to owner-only.
    target_mode = (path.stat().st_mode & 0o777) if path.exists() else mode
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         dir=path.parent, prefix='.' + path.name,
                                         suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=indent)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, target_mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
