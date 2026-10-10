#!/usr/bin/env python3
"""Omni Memory System - Episodic, Semantic, Procedural, Vector stores."""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

# Optional embeddings are loaded dynamically: the core memory layer remains
# importable in a standard-library-only install.
try:
    _st = importlib.import_module("sentence_transformers")
    _np = importlib.import_module("numpy")
    SentenceTransformer = _st.SentenceTransformer
    np = _np
    HAS_EMBEDDINGS = True
except Exception:
    SentenceTransformer = None
    np = None
    HAS_EMBEDDINGS = False

@dataclass
class EpisodicEvent:
    id: str
    timestamp: float
    event_type: str
    detail: str
    context: Dict[str, Any]
    outcome: Optional[str] = None
    tags: Optional[List[str]] = None

    def __post_init__(self):
        if self.tags is None:
            self.tags = []


class OmniMemory:
    """Unified memory interface for dai-omni."""

    def __init__(self, dai_root: Path):
        self.dai_root = Path(dai_root).resolve()
        self.state_dir = self.dai_root / "state/omni"
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)

        self.episodic_path = self.state_dir / "episodic.jsonl"
        self.semantic_path = self.state_dir / "semantic.json"
        self.procedural_path = self.state_dir / "procedural.json"
        self.vectors_dir = self.state_dir / "vectors"
        self.vectors_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.vectors_dir, 0o700)
        for existing in self.state_dir.rglob("*"):
            if existing.is_file():
                with contextlib.suppress(OSError):
                    os.chmod(existing, 0o600)

        self._semantic_cache: Dict[str, Any] = {}
        self._procedural_cache: Dict[str, Any] = {}
        self._embedding_model = None
        self._vector_index: Dict[str, Any] = {}

        self._load_caches()
        self._load_vector_index()

    def _load_caches(self):
        """Load semantic and procedural memory into memory."""
        try:
            if self.semantic_path.exists():
                self._semantic_cache = json.loads(self.semantic_path.read_text())
        except Exception:
            self._semantic_cache = {}

        try:
            if self.procedural_path.exists():
                self._procedural_cache = json.loads(self.procedural_path.read_text())
        except Exception:
            self._procedural_cache = {}

    def _load_vector_index(self):
        """Load vector embeddings from disk."""
        if not HAS_EMBEDDINGS:
            return
        for vec_file in self.vectors_dir.glob("*.npy"):
            try:
                key = vec_file.stem
                self._vector_index[key] = np.load(vec_file)
            except Exception:
                pass

    def _get_embedding_model(self):
        """Lazy-load embedding model."""
        if self._embedding_model is None and HAS_EMBEDDINGS:
            self._embedding_model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
        return self._embedding_model

    def _embed(self, text: str) -> Optional[np.ndarray]:
        """Generate embedding for text."""
        model = self._get_embedding_model()
        if model is None:
            return None
        return model.encode(text, convert_to_numpy=True)

    # ===== Episodic Memory (Event Log) =====

    def log_event(self, event_type: str, detail: str, context: Optional[Dict[str, Any]] = None,
                  outcome: Optional[str] = None, tags: Optional[List[str]] = None) -> str:
        """Append an event to the episodic log."""
        event = EpisodicEvent(
            id=str(uuid.uuid4())[:8],
            timestamp=time.time(),
            event_type=event_type,
            detail=detail,
            context=context or {},
            outcome=outcome,
            tags=tags or []
        )

        with open(self.episodic_path, "a") as f:
            os.chmod(self.episodic_path, 0o600)
            f.write(json.dumps(asdict(event)) + "\n")

        # Also index in vector store for semantic search
        self._index_event(event)

        return event.id

    def _index_event(self, event: EpisodicEvent):
        """Add event to vector index for semantic search."""
        if not HAS_EMBEDDINGS:
            return
        text = f"{event.event_type}: {event.detail}"
        embedding = self._embed(text)
        if embedding is not None:
            key = f"episodic_{event.id}"
            self._vector_index[key] = embedding
            np.save(self.vectors_dir / f"{key}.npy", embedding)

    def episodic_search(self, query: str, limit: int = 10) -> List[Dict]:
        """Search episodic memory by semantic similarity."""
        results = []

        if HAS_EMBEDDINGS and self._vector_index:
            query_vec = self._embed(query)
            if query_vec is not None:
                # Cosine similarity
                for key, vec in self._vector_index.items():
                    if key.startswith("episodic_"):
                        sim = np.dot(query_vec, vec) / (np.linalg.norm(query_vec) * np.linalg.norm(vec) + 1e-8)
                        results.append((sim, key))
                results.sort(reverse=True, key=lambda x: x[0])
                results = results[:limit]

        # Fallback: text search in JSONL
        if not results:
            with open(self.episodic_path) as f:
                for line in f:
                    try:
                        event = json.loads(line)
                        if query.lower() in event.get("detail", "").lower():
                            results.append((1.0, event["id"]))
                    except Exception:
                        pass

        # Load full events
        full_results = []
        if results:
            with open(self.episodic_path) as f:
                events_by_id = {json.loads(line)["id"]: json.loads(line) for line in f}
            for _, eid in results:
                if eid in events_by_id:
                    full_results.append(events_by_id[eid])

        return full_results

    def get_recent_events(self, limit: int = 50) -> List[Dict]:
        """Get most recent events."""
        events = []
        if self.episodic_path.exists():
            with open(self.episodic_path) as f:
                for line in f:
                    with contextlib.suppress(Exception):
                        events.append(json.loads(line))
        return events[-limit:]

    # ===== Semantic Memory (Facts) =====

    def remember_fact(self, key: str, value: Any, confidence: float = 1.0, source: str = "user"):
        """Store a semantic fact."""
        self._semantic_cache[key] = {
            "value": value,
            "confidence": confidence,
            "source": source,
            "updated": time.time(),
            "access_count": self._semantic_cache.get(key, {}).get("access_count", 0) + 1
        }
        self._save_semantic()

        # Index for search
        if HAS_EMBEDDINGS:
            text = f"{key}: {value}"
            embedding = self._embed(text)
            if embedding is not None:
                vkey = f"semantic_{key}"
                self._vector_index[vkey] = embedding
                np.save(self.vectors_dir / f"{vkey}.npy", embedding)

    def recall_fact(self, key: str, default: Any = None) -> Any:
        """Retrieve a semantic fact."""
        entry = self._semantic_cache.get(key)
        if entry:
            entry["access_count"] = entry.get("access_count", 0) + 1
            self._save_semantic()
            return entry["value"]
        return default

    def semantic_search(self, query: str, limit: int = 10) -> List[Dict]:
        """Search semantic facts by similarity."""
        results = []

        if HAS_EMBEDDINGS and self._vector_index:
            query_vec = self._embed(query)
            if query_vec is not None:
                for key, vec in self._vector_index.items():
                    if key.startswith("semantic_"):
                        sim = np.dot(query_vec, vec) / (np.linalg.norm(query_vec) * np.linalg.norm(vec) + 1e-8)
                        results.append((sim, key.replace("semantic_", "")))
                results.sort(reverse=True, key=lambda x: x[0])
                results = results[:limit]

        # Fallback: substring match
        if not results:
            for key, entry in self._semantic_cache.items():
                if query.lower() in key.lower() or query.lower() in str(entry.get("value", "")).lower():
                    results.append((1.0, key))

        full_results = []
        for _, key in results:
            if key in self._semantic_cache:
                entry = self._semantic_cache[key]
                full_results.append({
                    "key": key,
                    "value": entry["value"],
                    "confidence": entry.get("confidence", 1.0),
                    "source": entry.get("source", "unknown"),
                    "updated": entry.get("updated", 0)
                })

        return full_results

    def _save_semantic(self):
        """Persist semantic memory to disk."""
        self.semantic_path.write_text(json.dumps(self._semantic_cache, indent=2))
        os.chmod(self.semantic_path, 0o600)

    # ===== Procedural Memory (How-To) =====

    def learn_procedure(self, name: str, steps: List[Dict], context: str = "",
                       success_rate: float = 1.0, tags: List[str] = None):
        """Store a learned procedure."""
        self._procedural_cache[name] = {
            "steps": steps,
            "context": context,
            "success_rate": success_rate,
            "tags": tags or [],
            "created": time.time(),
            "updated": time.time(),
            "use_count": 0
        }
        self._save_procedural()

        # Index
        if HAS_EMBEDDINGS:
            text = f"{name}: {context} " + " ".join(str(s) for s in steps)
            embedding = self._embed(text)
            if embedding is not None:
                vkey = f"procedural_{name}"
                self._vector_index[vkey] = embedding
                np.save(self.vectors_dir / f"{vkey}.npy", embedding)

    def recall_procedure(self, name: str) -> Optional[Dict]:
        """Retrieve a procedure."""
        entry = self._procedural_cache.get(name)
        if entry:
            entry["use_count"] = entry.get("use_count", 0) + 1
            self._save_procedural()
            return entry
        return None

    def search_procedures(self, query: str, limit: int = 5) -> List[Dict]:
        """Search procedures by similarity."""
        results = []

        if HAS_EMBEDDINGS and self._vector_index:
            query_vec = self._embed(query)
            if query_vec is not None:
                for key, vec in self._vector_index.items():
                    if key.startswith("procedural_"):
                        sim = np.dot(query_vec, vec) / (np.linalg.norm(query_vec) * np.linalg.norm(vec) + 1e-8)
                        results.append((sim, key.replace("procedural_", "")))
                results.sort(reverse=True, key=lambda x: x[0])
                results = results[:limit]

        full_results = []
        for _, name in results:
            if name in self._procedural_cache:
                entry = self._procedural_cache[name]
                full_results.append({
                    "name": name,
                    "steps": entry["steps"],
                    "context": entry["context"],
                    "success_rate": entry.get("success_rate", 1.0),
                    "tags": entry.get("tags", [])
                })

        return full_results

    def _save_procedural(self):
        """Persist procedural memory to disk."""
        self.procedural_path.write_text(json.dumps(self._procedural_cache, indent=2))
        os.chmod(self.procedural_path, 0o600)

    # ===== Maintenance =====

    def forget(self, pattern: str):
        """Delete entries matching pattern from all stores."""
        # Semantic
        keys_to_delete = [k for k in self._semantic_cache if pattern.lower() in k.lower()]
        for k in keys_to_delete:
            del self._semantic_cache[k]
            # Also remove vector
            vkey = f"semantic_{k}"
            if vkey in self._vector_index:
                del self._vector_index[vkey]
            vec_file = self.vectors_dir / f"{vkey}.npy"
            if vec_file.exists():
                vec_file.unlink()
        self._save_semantic()

        # Procedural
        keys_to_delete = [k for k in self._procedural_cache if pattern.lower() in k.lower()]
        for k in keys_to_delete:
            del self._procedural_cache[k]
            vkey = f"procedural_{k}"
            if vkey in self._vector_index:
                del self._vector_index[vkey]
            vec_file = self.vectors_dir / f"{vkey}.npy"
            if vec_file.exists():
                vec_file.unlink()
        self._save_procedural()

        # Episodic: would need to rewrite JSONL (expensive), mark as deleted instead
        # For now, just note it
        self.log_event("forget", f"Forgot pattern: {pattern}", {"pattern": pattern})

    def consolidate(self):
        """Compress and deduplicate memory."""
        # Semantic: merge similar keys, boost confidence on agreement
        # For now, just re-save to ensure clean JSON
        self._save_semantic()
        self._save_procedural()

        # Prune old episodic events (keep last 10000)
        if self.episodic_path.exists():
            events = []
            with open(self.episodic_path) as f:
                for line in f:
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        pass
            if len(events) > 10000:
                with open(self.episodic_path, "w") as f:
                    for e in events[-10000:]:
                        f.write(json.dumps(e) + "\n")

        print("Memory consolidated")

    def stats(self) -> Dict:
        """Get memory statistics."""
        episodic_count = 0
        if self.episodic_path.exists():
            with open(self.episodic_path) as f:
                episodic_count = sum(1 for _ in f)

        return {
            "episodic_events": episodic_count,
            "semantic_facts": len(self._semantic_cache),
            "procedural_skills": len(self._procedural_cache),
            "vector_embeddings": len(self._vector_index),
            "state_dir_size_mb": sum(f.stat().st_size for f in self.state_dir.rglob("*")) / 1024 / 1024
        }
