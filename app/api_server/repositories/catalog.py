"""A small atomic JSON catalog for the first API-server deployment.

The repository is deliberately behind an interface-like class. It keeps the MVP
usable without introducing a database, while leaving the service layer ready for
a SQLite/PostgreSQL implementation later.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


class CatalogRepository:
    """Persist knowledge-base and application metadata in one JSON document."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"knowledge_bases": {}, "applications": {}})

    def _read(self) -> dict[str, dict[str, dict[str, Any]]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        return {
            "knowledge_bases": dict(payload.get("knowledge_bases", {})),
            "applications": dict(payload.get("applications", {})),
        }

    def _write(self, payload: dict[str, Any]) -> None:
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp_path.replace(self.path)

    def list(self, kind: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._read()[kind].values())

    def get(self, kind: str, item_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._read()[kind].get(item_id)

    def put(self, kind: str, item_id: str, item: dict[str, Any]) -> None:
        with self._lock:
            payload = self._read()
            payload[kind][item_id] = item
            self._write(payload)


__all__ = ["CatalogRepository"]
