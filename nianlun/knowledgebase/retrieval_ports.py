"""Small constructor ports for infrastructure retrieval adapters."""

from typing import Any, Protocol


class NodeSearchPort(Protocol):
    def search(
        self, query: str, limit: int = 512, doc_ids: list[str] | None = None
    ) -> list[dict[str, Any]]: ...


class DocumentSearchPort(Protocol):
    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]: ...
