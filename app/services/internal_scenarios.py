"""
Internal Scenarios Store — file-backed library of curated planner step templates.

Each scenario stores a canonical question, vetted planner steps, and an OpenAI
embedding of the question. The planner queries this store before its own LLM
decomposition: when a stored scenario matches the user's query above the
threshold (default 0.90 cosine similarity), the planner uses the curated steps
as the spine of its plan and only adapts filters.

Storage:
    app/data/internal_scenarios.json
    Schema: {"version": int, "embedding_model": str, "scenarios": [...]}

Embeddings:
    Generated via OpenAI's text-embedding-3-small at write time and cached on
    disk. Search re-embeds the query at request time.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from langchain_openai import OpenAIEmbeddings

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_THRESHOLD = 0.90
_DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "internal_scenarios.json"


class InternalScenariosStore:
    """File-backed store with OpenAI embeddings and cosine similarity search."""

    def __init__(self, path: Path = _DATA_PATH):
        self._path = path
        self._lock = threading.Lock()
        self._embedder = OpenAIEmbeddings(
            model=EMBEDDING_MODEL,
            api_key=os.getenv("OPENAI_API_KEY"),
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._write_unlocked({
                "version": 1,
                "embedding_model": EMBEDDING_MODEL,
                "scenarios": [],
            })

    # ── File I/O ───────────────────────────────────────────────────────────

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if not content:
                raise ValueError("file is empty")
            return json.loads(content)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(
                "Internal scenarios file at %s is empty or invalid (%s); "
                "recreating with default schema.",
                self._path, e,
            )
            default = {
                "version": 1,
                "embedding_model": EMBEDDING_MODEL,
                "scenarios": [],
            }
            self._write_unlocked(default)
            return default

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self._path)

    # ── Embedding ──────────────────────────────────────────────────────────

    def _embed(self, text: str) -> list[float]:
        return self._embedder.embed_query(text)

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
        denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
        if denom == 0.0:
            return 0.0
        return float(np.dot(va, vb) / denom)

    @staticmethod
    def _new_id() -> str:
        return "S" + secrets.token_hex(4)

    @staticmethod
    def _strip_embedding(scenario: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in scenario.items() if k != "embedding"}

    # ── Public API ─────────────────────────────────────────────────────────

    def list_all(self) -> list[dict[str, Any]]:
        with self._lock:
            data = self._read_unlocked()
        return [self._strip_embedding(s) for s in data.get("scenarios", [])]

    def count(self) -> int:
        with self._lock:
            data = self._read_unlocked()
        return len(data.get("scenarios", []))

    def create(self, tag: str, question: str, steps: list[str]) -> dict[str, Any]:
        if not question.strip():
            raise ValueError("question must not be empty")
        if not steps:
            raise ValueError("steps must not be empty")

        embedding = self._embed(question)
        scenario = {
            "id": self._new_id(),
            "tag": tag,
            "question": question,
            "steps": list(steps),
            "embedding": embedding,
            "embedding_model": EMBEDDING_MODEL,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        with self._lock:
            data = self._read_unlocked()
            data.setdefault("scenarios", []).append(scenario)
            self._write_unlocked(data)

        logger.info("Internal scenario created: id=%s tag=%s", scenario["id"], tag)
        return self._strip_embedding(scenario)

    def delete(self, scenario_id: str) -> bool:
        with self._lock:
            data = self._read_unlocked()
            scenarios = data.get("scenarios", [])
            new_scenarios = [s for s in scenarios if s.get("id") != scenario_id]
            if len(new_scenarios) == len(scenarios):
                return False
            data["scenarios"] = new_scenarios
            self._write_unlocked(data)
        logger.info("Internal scenario deleted: id=%s", scenario_id)
        return True

    def search(
        self,
        query: str,
        threshold: float = DEFAULT_THRESHOLD,
        top_k: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Cosine-similarity search over stored scenario embeddings.

        Returns matches ≥ threshold sorted by similarity desc.
        Each match includes the scenario fields (without embedding) plus
        `similarity_score` (float in [0, 1]).
        """
        if not query.strip():
            return []

        with self._lock:
            data = self._read_unlocked()
        scenarios = data.get("scenarios", [])
        if not scenarios:
            return []

        try:
            q_emb = self._embed(query)
        except Exception as e:
            logger.warning("Embedding failed for query %.80s: %s", query, e)
            return []

        scored: list[tuple[float, dict[str, Any]]] = []
        for s in scenarios:
            emb = s.get("embedding")
            if not emb:
                continue
            score = self._cosine(q_emb, emb)
            if score >= threshold:
                scored.append((score, s))

        scored.sort(key=lambda x: x[0], reverse=True)
        if top_k is not None:
            scored = scored[:top_k]

        return [
            {**self._strip_embedding(s), "similarity_score": score}
            for score, s in scored
        ]


# ── Thread-safe singleton ─────────────────────────────────────────────────

_shared_instance: InternalScenariosStore | None = None
_singleton_lock = threading.Lock()


def get_internal_scenarios_store() -> InternalScenariosStore:
    """Return a shared InternalScenariosStore instance (thread-safe lazy init)."""
    global _shared_instance
    if _shared_instance is None:
        with _singleton_lock:
            if _shared_instance is None:
                _shared_instance = InternalScenariosStore()
    return _shared_instance
