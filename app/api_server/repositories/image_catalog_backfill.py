"""Minimal offline image backfill persistence; never migrates or recovers tasks."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any

from app.api_server.database.migrations import SCHEMA_VERSION
from app.api_server.repositories.metadata.publishing import PublishConflictError


class ImageCatalogBackfillRepository:
    def __init__(self, path: Path, *, dry_run: bool) -> None:
        path = path.expanduser().resolve()
        if dry_run:
            wal = Path(str(path) + "-wal")
            if wal.exists() and wal.stat().st_size:
                raise RuntimeError(
                    "dry-run requires a clean SQLite shutdown/checkpoint; nonempty WAL must not be ignored"
                )
        # A normal read-only WAL connection still writes shared-memory read marks.
        # Immutable mode avoids those writes after a clean offline checkpoint.
        uri = path.as_uri() + ("?mode=ro&immutable=1" if dry_run else "?mode=rw")
        self.connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA foreign_keys = ON")
            if dry_run:
                self.connection.execute("PRAGMA query_only = ON")
            versions = self.connection.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
            if [int(row[0]) for row in versions] != [SCHEMA_VERSION]:
                raise RuntimeError(
                    "database schema requires an explicit service upgrade before backfill"
                )
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def knowledge_base(self, knowledge_base_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM knowledge_bases WHERE id = ?", (knowledge_base_id,)
        ).fetchone()
        if row is None:
            raise KeyError(knowledge_base_id)
        return dict(row)

    def revision(self, knowledge_base_id: str, version: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM knowledge_base_workspace_revisions WHERE knowledge_base_id = ? AND content_version = ? AND state = 'committed'",
            (knowledge_base_id, version),
        ).fetchone()
        if row is None:
            raise PublishConflictError("current committed revision is missing")
        return dict(row)

    def document(
        self, knowledge_base_id: str, document_id: str
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM documents WHERE knowledge_base_id = ? AND id = ? AND status != 'deleted'",
            (knowledge_base_id, document_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def artifacts(self, document_id: str, generation: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM document_artifacts WHERE document_id = ? AND pipeline_generation = ?",
                (document_id, generation),
            )
        ]

    def commit_batch(
        self,
        *,
        knowledge_base_id: str,
        expected_version: int,
        snapshot_relpath: str,
        manifest_sha256: str,
        document_ids: list[str],
        artifacts: list[dict[str, Any]],
        now: str,
    ) -> None:
        """Only publish catalogs; leave in-progress document generations/tasks alone."""
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            kb = self.knowledge_base(knowledge_base_id)
            if int(kb["content_version"]) != expected_version:
                raise PublishConflictError("content version changed; retry batch")
            for document_id in document_ids:
                if self.document(knowledge_base_id, document_id) is None:
                    raise PublishConflictError(
                        "document deleted during publication; retry batch"
                    )
            previous = connection.execute(
                "UPDATE knowledge_base_workspace_revisions SET state = 'superseded' WHERE knowledge_base_id = ? AND content_version = ? AND state = 'committed'",
                (knowledge_base_id, expected_version),
            )
            if previous.rowcount != 1:
                raise PublishConflictError("current committed revision changed")
            next_version = expected_version + 1
            connection.execute(
                "INSERT INTO knowledge_base_workspace_revisions (id, knowledge_base_id, content_version, snapshot_relpath, manifest_sha256, state, created_at) VALUES (?, ?, ?, ?, ?, 'committed', ?)",
                (
                    str(uuid.uuid4()),
                    knowledge_base_id,
                    next_version,
                    snapshot_relpath,
                    manifest_sha256,
                    now,
                ),
            )
            for artifact in artifacts:
                connection.execute(
                    "INSERT INTO document_artifacts (id, document_id, kind, relpath, mime_type, size_bytes, sha256, pipeline_generation, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(document_id, kind, relpath) DO NOTHING",
                    (
                        str(uuid.uuid4()),
                        artifact["document_id"],
                        artifact["kind"],
                        artifact["relpath"],
                        artifact["mime_type"],
                        artifact["size_bytes"],
                        artifact["sha256"],
                        artifact["generation"],
                        now,
                    ),
                )
            connection.execute(
                "UPDATE knowledge_bases SET content_version = ?, updated_at = ?, fts_status = CASE WHEN fts_status = 'disabled' THEN fts_status ELSE 'pending' END, fts_target_revision = CASE WHEN fts_status = 'disabled' THEN fts_target_revision ELSE ? END, vector_status = CASE WHEN vector_status = 'disabled' THEN vector_status ELSE 'pending' END, vector_target_revision = CASE WHEN vector_status = 'disabled' THEN vector_target_revision ELSE ? END, vector_progress_stage = CASE WHEN vector_status = 'disabled' THEN vector_progress_stage ELSE 'queued' END WHERE id = ? AND content_version = ?",
                (
                    next_version,
                    now,
                    next_version,
                    next_version,
                    knowledge_base_id,
                    expected_version,
                ),
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
