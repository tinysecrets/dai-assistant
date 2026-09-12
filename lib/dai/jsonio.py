"""JSON file I/O: atomic writes, tolerant reads, mtime-cached config files.

State files here (cooldowns, router stats, task records, approval tokens) are
written from multiple threads and read by other processes, so every write goes
to a temp file in the same directory and is swapped in with ``os.replace``.
A reader therefore never observes a half-written file, and a crash mid-write
cannot truncate the previous good copy.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

DEFAULT_MODE = 0o600


def load_json(path: Path | str, default: Any = None) -> Any:
    """Read JSON, returning ``default`` for a missing or unreadable file.

    A corrupt state file must never take a service down: it is reported to the
    caller as ``default`` and overwritten on the next save.
    """
    p = Path(path)
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return default


def atomic_write_json(
    path: Path | str,
    data: Any,
    *,
    mode: int = DEFAULT_MODE,
    indent: int = 2,
) -> None:
    """Write JSON atomically, creating parent directories as needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=indent, sort_keys=False, default=str) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, p)
    except BaseException:
        # Best effort: never let cleanup of the temp file mask the real error.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def save_json(path: Path | str, data: Any, *, mode: int = DEFAULT_MODE) -> None:
    atomic_write_json(path, data, mode=mode)


class AtomicJsonStore:
    """A JSON document on disk with a lock and read-modify-write helper."""

    def __init__(self, path: Path | str, default: Any = None, *, mode: int = DEFAULT_MODE) -> None:
        self.path = Path(path)
        self.default = {} if default is None else default
        self.mode = mode
        # Reentrant: a caller may hold the lock across a read-modify-write pair
        # (see ApprovalStore.consume) and still call update().
        self._lock = threading.RLock()

    @property
    def lock(self) -> "threading.RLock":
        return self._lock

    def read(self) -> Any:
        value = load_json(self.path, self.default)
        if value is None:
            return self.default
        return value

    def write(self, data: Any) -> None:
        atomic_write_json(self.path, data, mode=self.mode)

    def update(self, mutate: Callable[[Any], Any]) -> Any:
        """Apply ``mutate`` to the document under the lock and persist it.

        ``mutate`` may return a new document or modify and return the same one.
        Returns the persisted document.
        """
        with self._lock:
            data = self.read()
            result = mutate(data)
            data = self.default if result is None else result
            self.write(data)
            return data

    def exists(self) -> bool:
        return self.path.exists()


class CachedJsonFile:
    """JSON file re-read only when its mtime/size changes.

    The router reads the rotation pool and the ~1.7k-line model catalog on
    every request; caching on stat keeps that off the hot path while still
    picking up edits without a restart.
    """

    def __init__(self, path: Path | str, default: Any = None) -> None:
        self.path = Path(path)
        self.default = {} if default is None else default
        self._lock = threading.Lock()
        self._stamp: Tuple[float, int] = (-1.0, -1)
        self._value: Any = self.default
        self._loaded_at: float = 0.0
        self._error: Optional[str] = None

    def _stat(self) -> Tuple[float, int]:
        try:
            st = self.path.stat()
            return (st.st_mtime, st.st_size)
        except OSError:
            return (-1.0, -1)

    def get(self) -> Any:
        stamp = self._stat()
        with self._lock:
            if stamp == self._stamp and self._loaded_at:
                return self._value
            raw = self.path.read_text(encoding="utf-8") if stamp[0] >= 0 else None
            if raw is None:
                self._stamp, self._value, self._error = stamp, self.default, None
            else:
                try:
                    self._value = json.loads(raw)
                    self._error = None
                except json.JSONDecodeError as exc:
                    # Keep serving the last good copy; surface the reason.
                    self._error = f"invalid JSON in {self.path.name}: {exc}"
                    if not self._loaded_at:
                        self._value = self.default
                self._stamp = stamp
            self._loaded_at = time.time()
            return self._value

    @property
    def error(self) -> Optional[str]:
        with self._lock:
            return self._error

    def invalidate(self) -> None:
        with self._lock:
            self._stamp = (-1.0, -1)
            self._loaded_at = 0.0

    def as_dict(self) -> Dict[str, Any]:
        value = self.get()
        return value if isinstance(value, dict) else {}
