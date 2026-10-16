"""Serialize mutable index writes against revision-checked remote queries."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any, Literal, Protocol

IndexKind = Literal["fts", "vector"]
logger = logging.getLogger(__name__)


class SearchPort(Protocol):
    def search(
        self, query: str, limit: int = 512, doc_ids: list[str] | None = None
    ) -> list[dict[str, Any]]: ...


class IndexAccessCoordinator:
    def __init__(self, lookup: Callable[[str], dict[str, Any]]) -> None:
        self.lookup = lookup
        self._locks: dict[tuple[str, IndexKind], threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock(self, knowledge_base_id: str, kind: IndexKind) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault((knowledge_base_id, kind), threading.Lock())

    @contextmanager
    def mutation(
        self, knowledge_base_id: str, kind: IndexKind
    ) -> Generator[None, None, None]:
        with self._lock(knowledge_base_id, kind):
            yield

    @contextmanager
    def read(
        self, knowledge_base_id: str, kind: IndexKind, revision: int
    ) -> Generator[bool, None, None]:
        lock = self._lock(knowledge_base_id, kind)
        if not lock.acquire(blocking=False):
            yield False
            return
        try:
            item = self.lookup(knowledge_base_id)
            yield (
                item.get("content_version") == revision
                and item.get(f"{kind}_revision") == revision
                and item.get(f"{kind}_status") == "ready"
            )
        finally:
            lock.release()


class GuardedFullTextRetriever:
    def __init__(
        self,
        coordinator: IndexAccessCoordinator,
        knowledge_base_id: str,
        revision: int,
        remote: SearchPort,
        fallback: SearchPort,
    ) -> None:
        self.coordinator = coordinator
        self.knowledge_base_id = knowledge_base_id
        self.revision = revision
        self.remote = remote
        self.fallback = fallback

    def close(self) -> None:
        for retriever in (self.remote, self.fallback):
            close = getattr(retriever, "close", None)
            if callable(close):
                close()

    def search(
        self, query: str, limit: int = 512, doc_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        with self.coordinator.read(
            self.knowledge_base_id, "fts", self.revision
        ) as usable:
            if usable:
                return self.remote.search(query, limit=limit, doc_ids=doc_ids)
        logger.info(
            "application.fts_snapshot_fallback knowledge_base_id=%s revision=%s",
            self.knowledge_base_id,
            self.revision,
        )
        return self.fallback.search(query, limit=limit, doc_ids=doc_ids)


class GuardedSemanticRetriever:
    def __init__(
        self,
        coordinator: IndexAccessCoordinator,
        knowledge_base_id: str,
        revision: int,
        remote: Any,
    ) -> None:
        self.coordinator = coordinator
        self.knowledge_base_id = knowledge_base_id
        self.revision = revision
        self.remote = remote

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        with self.coordinator.read(
            self.knowledge_base_id, "vector", self.revision
        ) as usable:
            if usable:
                return self.remote.search(query, limit=limit)
        logger.info(
            "application.vector_revision_unavailable knowledge_base_id=%s revision=%s",
            self.knowledge_base_id,
            self.revision,
        )
        return []
