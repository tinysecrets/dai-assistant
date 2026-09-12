"""In-memory counters with throttled persistence.

The router records what each backend actually did — successes, failures by
kind, latency — so ``GET /v1/status/stats`` can answer "which free model is
reliable right now?" without a scrape harness.  Writes to disk are coalesced
(at most once per ``flush_interval``) because a busy router would otherwise
touch the state file on every request.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .jsonio import AtomicJsonStore


class StatsCollector:
    """Per-key request counters, safe for concurrent use."""

    def __init__(
        self,
        path: Optional[Path | str] = None,
        *,
        flush_interval: float = 5.0,
        max_keys: int = 512,
    ) -> None:
        self._lock = threading.RLock()
        self._counters: Dict[str, Dict[str, Any]] = {}
        self._dirty = False
        self._last_flush = 0.0
        self._flush_interval = flush_interval
        self._max_keys = max_keys
        self.store = AtomicJsonStore(path, {"counters": {}}, mode=0o600) if path else None
        if self.store:
            loaded = self.store.read()
            counters = loaded.get("counters") if isinstance(loaded, dict) else None
            if isinstance(counters, dict):
                self._counters = {str(k): dict(v) for k, v in counters.items() if isinstance(v, dict)}

    def _entry(self, key: str) -> Dict[str, Any]:
        entry = self._counters.get(key)
        if entry is None:
            if len(self._counters) >= self._max_keys:
                # Drop the least-recently-touched key rather than grow forever.
                oldest = min(self._counters.items(), key=lambda kv: kv[1].get("last_seen", 0.0))
                self._counters.pop(oldest[0], None)
            entry = {
                "attempts": 0,
                "ok": 0,
                "errors": 0,
                "kinds": {},
                "total_ms": 0.0,
                "last_ms": 0.0,
                "last_status": 0,
                "last_kind": "",
                "last_ok_at": None,
                "last_error_at": None,
                "last_seen": 0.0,
            }
            self._counters[key] = entry
        return entry

    def record(self, key: str, *, status: int, kind: str, ok: bool, elapsed_ms: float = 0.0) -> None:
        now = time.time()
        with self._lock:
            entry = self._entry(key)
            entry["attempts"] += 1
            entry["total_ms"] = round(entry["total_ms"] + elapsed_ms, 1)
            entry["last_ms"] = round(elapsed_ms, 1)
            entry["last_status"] = status
            entry["last_kind"] = kind
            entry["last_seen"] = now
            if ok:
                entry["ok"] += 1
                entry["last_ok_at"] = now
            else:
                entry["errors"] += 1
                entry["last_error_at"] = now
                kinds = entry.setdefault("kinds", {})
                kinds[kind] = int(kinds.get(kind, 0)) + 1
            avg = entry["total_ms"] / entry["attempts"] if entry["attempts"] else 0.0
            entry["avg_ms"] = round(avg, 1)
            self._dirty = True
            self._maybe_flush_locked(now)

    def _maybe_flush_locked(self, now: float) -> None:
        if not self.store or not self._dirty:
            return
        if now - self._last_flush < self._flush_interval:
            return
        self._flush_locked()

    def _flush_locked(self) -> None:
        if not self.store:
            return
        self.store.write({"counters": self._counters, "updated_at": time.time()})
        self._dirty = False
        self._last_flush = time.time()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "counters": {k: dict(v) for k, v in sorted(self._counters.items())},
                "keys": len(self._counters),
            }

    def reset(self) -> None:
        with self._lock:
            self._counters = {}
            self._dirty = True
            self._flush_locked()
