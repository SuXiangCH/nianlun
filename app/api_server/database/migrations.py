"""SQLite schema bootstrap for the first public release."""

from __future__ import annotations

import sqlite3

from app.api_server.database.connection import SQLiteConnectionFactory
from app.api_server.database.models import Base


SCHEMA_VERSION = 7

_REQUIRED_COLUMNS = {
    table.name: {column.name for column in table.columns}
    for table in Base.metadata.sorted_tables
}


def _record_current_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO schema_migrations(version, name, applied_at)
        VALUES (?, 'parse_chunk_plan', CURRENT_TIMESTAMP)
        """,
        (SCHEMA_VERSION,),
    )


def _migrate_message_trace(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1]) for row in connection.execute('PRAGMA table_info("messages")')
    }
    if "trace_json" not in columns:
        connection.execute(
            "ALTER TABLE messages ADD COLUMN trace_json TEXT NOT NULL DEFAULT '[]'"
        )


def _migrate_heading_recovery_enabled(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in connection.execute('PRAGMA table_info("knowledge_bases")')
    }
    if "heading_recovery_enabled" not in columns:
        connection.execute(
            "ALTER TABLE knowledge_bases "
            "ADD COLUMN heading_recovery_enabled INTEGER NOT NULL DEFAULT 1"
        )


def _migrate_pipeline_generation(connection: sqlite3.Connection) -> None:
    """v3 -> v4: pipeline generation columns and the V2 revision table.

    ``Base.metadata.create_all`` already creates the
    ``knowledge_base_workspace_revisions`` table on fresh databases; existing
    databases only need the additive column backfill, which SQLite applies to
    every pre-existing row via the DEFAULT clause.
    """
    for table in ("documents", "document_artifacts"):
        columns = {
            str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        if "pipeline_generation" not in columns:
            connection.execute(
                f"ALTER TABLE {table} "
                "ADD COLUMN pipeline_generation INTEGER NOT NULL DEFAULT 1"
            )
    _rebuild_document_artifacts_for_v2_kinds(connection)
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_revisions_current "
        "ON knowledge_base_workspace_revisions(knowledge_base_id) "
        "WHERE state = 'committed'"
    )


def _migrate_document_pipeline(connection: sqlite3.Connection) -> None:
    """v4 -> v5: durable stage state and chunk-aware parse dispatch."""
    document_columns = {
        str(row[1]) for row in connection.execute('PRAGMA table_info("documents")')
    }
    additions = {
        "current_stage": "TEXT NOT NULL DEFAULT 'complete'",
        "stage_state": "TEXT NOT NULL DEFAULT 'succeeded'",
        "failed_stage": "TEXT NULL",
        "progress_completed": "INTEGER NULL",
        "progress_total": "INTEGER NULL",
        "progress_unit": "TEXT NULL",
        "warning_json": "TEXT NOT NULL DEFAULT '[]'",
        "deleted_content_version": "INTEGER NULL",
    }
    for name, definition in additions.items():
        if name not in document_columns:
            connection.execute(
                f'ALTER TABLE documents ADD COLUMN "{name}" {definition}'
            )

    parse_columns = {
        str(row[1])
        for row in connection.execute('PRAGMA table_info("document_parse_tasks")')
    }
    if "chunk_index" in parse_columns:
        return
    connection.execute("DROP INDEX IF EXISTS idx_document_parse_tasks_polling")
    connection.execute("DROP INDEX IF EXISTS idx_document_parse_tasks_batch")
    connection.execute(
        "ALTER TABLE document_parse_tasks RENAME TO document_parse_tasks_pre_v5"
    )
    connection.execute(
        """
        CREATE TABLE document_parse_tasks (
            id VARCHAR NOT NULL PRIMARY KEY,
            document_id VARCHAR NOT NULL,
            provider VARCHAR NOT NULL,
            api_mode VARCHAR NOT NULL,
            attempt INTEGER NOT NULL,
            pipeline_generation INTEGER NOT NULL DEFAULT 1,
            chunk_index INTEGER NOT NULL DEFAULT 0,
            chunk_count INTEGER NOT NULL DEFAULT 1,
            source_page_start INTEGER NULL,
            source_page_end INTEGER NULL,
            chunk_source_relpath VARCHAR NULL,
            result_root_relpath VARCHAR NULL,
            input_sha256 VARCHAR(64) NULL,
            output_sha256 VARCHAR(64) NULL,
            data_id VARCHAR NOT NULL,
            batch_id VARCHAR NULL,
            task_id VARCHAR NULL,
            model_version VARCHAR NOT NULL,
            request_json TEXT NOT NULL,
            state VARCHAR NOT NULL,
            dispatch_state VARCHAR NOT NULL DEFAULT 'queued',
            available_at VARCHAR(64) NOT NULL DEFAULT '',
            next_poll_at VARCHAR(64) NULL,
            lease_owner VARCHAR NULL,
            lease_expires_at VARCHAR(64) NULL,
            lease_token VARCHAR NULL,
            extracted_pages INTEGER NULL,
            total_pages INTEGER NULL,
            result_zip_url VARCHAR NULL,
            error_code VARCHAR NULL,
            error_message TEXT NULL,
            created_at VARCHAR(64) NOT NULL,
            updated_at VARCHAR(64) NOT NULL,
            started_at VARCHAR(64) NULL,
            completed_at VARCHAR(64) NULL,
            CONSTRAINT uq_document_parse_attempt UNIQUE (
                document_id, pipeline_generation, chunk_index, attempt
            ),
            CONSTRAINT uq_document_parse_data_attempt UNIQUE (
                provider, data_id, pipeline_generation, chunk_index, attempt
            ),
            CONSTRAINT ck_document_parse_attempt CHECK (attempt > 0),
            CONSTRAINT ck_document_parse_state CHECK (
                state IN ('created', 'uploading', 'waiting-file', 'pending',
                          'running', 'converting', 'done', 'failed')
            ),
            FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        INSERT INTO document_parse_tasks (
            id, document_id, provider, api_mode, attempt, pipeline_generation,
            chunk_index, chunk_count, input_sha256, data_id, batch_id, task_id,
            model_version, request_json, state, dispatch_state, available_at,
            extracted_pages, total_pages, result_zip_url, error_code,
            error_message, created_at, updated_at, started_at, completed_at
        )
        SELECT p.id, p.document_id, p.provider, p.api_mode, p.attempt, 1,
               0, 1, d.source_sha256, p.data_id, p.batch_id, p.task_id,
               p.model_version, p.request_json, p.state,
               CASE
                   WHEN p.state = 'done' THEN 'succeeded'
                   WHEN p.state = 'failed' THEN 'failed'
                   WHEN p.batch_id IS NOT NULL OR p.task_id IS NOT NULL THEN 'waiting'
                   ELSE 'queued'
               END,
               p.updated_at, p.extracted_pages, p.total_pages, p.result_zip_url,
               p.error_code, p.error_message, p.created_at, p.updated_at,
               p.started_at, p.completed_at
        FROM document_parse_tasks_pre_v5 AS p
        JOIN documents AS d ON d.id = p.document_id
        """
    )
    connection.execute("DROP TABLE document_parse_tasks_pre_v5")
    connection.execute(
        "CREATE INDEX idx_document_parse_tasks_polling "
        "ON document_parse_tasks(dispatch_state, available_at)"
    )
    connection.execute(
        "CREATE INDEX idx_document_parse_tasks_batch ON document_parse_tasks(batch_id)"
    )


def _migrate_document_tombstones(connection: sqlite3.Connection) -> None:
    """v5 -> v6: retain deleted documents and fence their active tasks."""
    indexes = {
        str(row[1]): bool(row[4])
        for row in connection.execute('PRAGMA index_list("documents")')
    }
    if not indexes.get("uq_documents_kb_source_hash", False):
        connection.execute(
            "DROP INDEX IF EXISTS idx_documents_knowledge_base_status_updated"
        )
        connection.execute("DROP INDEX IF EXISTS uq_documents_kb_source_hash")
        connection.execute(
            """
            CREATE TABLE documents_v6 (
                id VARCHAR NOT NULL PRIMARY KEY,
                knowledge_base_id VARCHAR NOT NULL,
                original_filename VARCHAR NOT NULL,
                file_extension VARCHAR(16) NOT NULL,
                mime_type VARCHAR(256) NOT NULL,
                size_bytes INTEGER NOT NULL,
                source_relpath VARCHAR NOT NULL,
                source_sha256 VARCHAR(64) NOT NULL,
                parser VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                pipeline_generation INTEGER NOT NULL DEFAULT 1,
                current_stage VARCHAR(16) NOT NULL DEFAULT 'complete',
                stage_state VARCHAR(16) NOT NULL DEFAULT 'succeeded',
                failed_stage VARCHAR(16) NULL,
                progress_completed INTEGER NULL,
                progress_total INTEGER NULL,
                progress_unit VARCHAR(16) NULL,
                warning_json TEXT NOT NULL DEFAULT '[]',
                deleted_content_version INTEGER NULL,
                parsed_markdown_relpath VARCHAR NULL,
                parsed_content_version INTEGER NULL,
                fts_indexed_version INTEGER NULL,
                vector_indexed_version INTEGER NULL,
                error_code VARCHAR NULL,
                error_message TEXT NULL,
                created_at VARCHAR(64) NOT NULL,
                updated_at VARCHAR(64) NOT NULL,
                completed_at VARCHAR(64) NULL,
                CONSTRAINT ck_documents_size_bytes CHECK (size_bytes > 0),
                CONSTRAINT ck_documents_parser CHECK (
                    parser IN ('native_markdown', 'mineru')
                ),
                CONSTRAINT ck_documents_status CHECK (
                    status IN ('uploaded', 'parsing', 'parsed', 'indexing',
                               'ready', 'failed', 'deleted')
                ),
                FOREIGN KEY(knowledge_base_id) REFERENCES knowledge_bases (id)
                    ON DELETE CASCADE
            )
            """
        )
        columns = (
            "id, knowledge_base_id, original_filename, file_extension, mime_type, "
            "size_bytes, source_relpath, source_sha256, parser, status, "
            "pipeline_generation, current_stage, stage_state, failed_stage, "
            "progress_completed, progress_total, progress_unit, warning_json, "
            "deleted_content_version, parsed_markdown_relpath, "
            "parsed_content_version, fts_indexed_version, vector_indexed_version, "
            "error_code, error_message, created_at, updated_at, completed_at"
        )
        connection.execute(
            f"INSERT INTO documents_v6 ({columns}) SELECT {columns} FROM documents"
        )
        connection.execute("DROP TABLE documents")
        connection.execute("ALTER TABLE documents_v6 RENAME TO documents")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_knowledge_base_status_updated "
        "ON documents(knowledge_base_id, status, updated_at)"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_kb_source_hash "
        "ON documents(knowledge_base_id, source_sha256) WHERE status != 'deleted'"
    )

    parse_table = connection.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'table' AND name = 'document_parse_tasks'"
    ).fetchone()
    if parse_table is None or "'canceled'" in str(parse_table[0]):
        return
    connection.execute("DROP INDEX IF EXISTS idx_document_parse_tasks_polling")
    connection.execute("DROP INDEX IF EXISTS idx_document_parse_tasks_batch")
    connection.execute(
        """
        CREATE TABLE document_parse_tasks_v6 (
            id VARCHAR NOT NULL PRIMARY KEY,
            document_id VARCHAR NOT NULL,
            provider VARCHAR NOT NULL,
            api_mode VARCHAR NOT NULL,
            attempt INTEGER NOT NULL,
            pipeline_generation INTEGER NOT NULL DEFAULT 1,
            chunk_index INTEGER NOT NULL DEFAULT 0,
            chunk_count INTEGER NOT NULL DEFAULT 1,
            source_page_start INTEGER NULL,
            source_page_end INTEGER NULL,
            chunk_source_relpath VARCHAR NULL,
            result_root_relpath VARCHAR NULL,
            input_sha256 VARCHAR(64) NULL,
            output_sha256 VARCHAR(64) NULL,
            data_id VARCHAR NOT NULL,
            batch_id VARCHAR NULL,
            task_id VARCHAR NULL,
            model_version VARCHAR NOT NULL,
            request_json TEXT NOT NULL,
            state VARCHAR NOT NULL,
            dispatch_state VARCHAR(16) NOT NULL DEFAULT 'queued',
            available_at VARCHAR(64) NOT NULL DEFAULT '',
            next_poll_at VARCHAR(64) NULL,
            lease_owner VARCHAR NULL,
            lease_expires_at VARCHAR(64) NULL,
            lease_token VARCHAR NULL,
            extracted_pages INTEGER NULL,
            total_pages INTEGER NULL,
            result_zip_url VARCHAR NULL,
            error_code VARCHAR NULL,
            error_message TEXT NULL,
            created_at VARCHAR(64) NOT NULL,
            updated_at VARCHAR(64) NOT NULL,
            started_at VARCHAR(64) NULL,
            completed_at VARCHAR(64) NULL,
            CONSTRAINT uq_document_parse_attempt UNIQUE (
                document_id, pipeline_generation, chunk_index, attempt
            ),
            CONSTRAINT uq_document_parse_data_attempt UNIQUE (
                provider, data_id, pipeline_generation, chunk_index, attempt
            ),
            CONSTRAINT ck_document_parse_attempt CHECK (attempt > 0),
            CONSTRAINT ck_document_parse_state CHECK (
                state IN ('created', 'uploading', 'waiting-file', 'pending',
                          'running', 'converting', 'done', 'failed', 'canceled')
            ),
            FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE CASCADE
        )
        """
    )
    parse_columns = (
        "id, document_id, provider, api_mode, attempt, pipeline_generation, "
        "chunk_index, chunk_count, source_page_start, source_page_end, "
        "chunk_source_relpath, result_root_relpath, input_sha256, output_sha256, "
        "data_id, batch_id, task_id, model_version, request_json, state, "
        "dispatch_state, available_at, next_poll_at, lease_owner, "
        "lease_expires_at, lease_token, extracted_pages, total_pages, "
        "result_zip_url, error_code, error_message, created_at, updated_at, "
        "started_at, completed_at"
    )
    connection.execute(
        f"INSERT INTO document_parse_tasks_v6 ({parse_columns}) "
        f"SELECT {parse_columns} FROM document_parse_tasks"
    )
    connection.execute("DROP TABLE document_parse_tasks")
    connection.execute(
        "ALTER TABLE document_parse_tasks_v6 RENAME TO document_parse_tasks"
    )
    connection.execute(
        "CREATE INDEX idx_document_parse_tasks_polling "
        "ON document_parse_tasks(dispatch_state, available_at)"
    )
    connection.execute(
        "CREATE INDEX idx_document_parse_tasks_batch ON document_parse_tasks(batch_id)"
    )


def _migrate_parse_chunk_plan(connection: sqlite3.Connection) -> None:
    """v6 -> v7: retain a generation's exact PDF chunk plan for recovery."""
    columns = {
        str(row[1]) for row in connection.execute('PRAGMA table_info("documents")')
    }
    if "parse_plan_json" not in columns:
        connection.execute("ALTER TABLE documents ADD COLUMN parse_plan_json TEXT NULL")


def _rebuild_document_artifacts_for_v2_kinds(
    connection: sqlite3.Connection,
) -> None:
    table_sql_row = connection.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'table' AND name = 'document_artifacts'"
    ).fetchone()
    if table_sql_row is None or "enriched_markdown" in str(table_sql_row[0]):
        return
    connection.execute("DROP INDEX IF EXISTS idx_document_artifacts_document_kind")
    connection.execute(
        "ALTER TABLE document_artifacts RENAME TO document_artifacts_pre_v4"
    )
    connection.execute(
        """
        CREATE TABLE document_artifacts (
            id VARCHAR NOT NULL PRIMARY KEY,
            document_id VARCHAR NOT NULL,
            kind VARCHAR NOT NULL,
            relpath VARCHAR NOT NULL,
            mime_type VARCHAR(256) NOT NULL,
            size_bytes INTEGER NOT NULL,
            sha256 VARCHAR(64) NOT NULL,
            pipeline_generation INTEGER NOT NULL DEFAULT 1,
            created_at VARCHAR(64) NOT NULL,
            CONSTRAINT uq_document_artifact_path
                UNIQUE (document_id, kind, relpath),
            CONSTRAINT ck_document_artifact_kind CHECK (
                kind IN (
                    'original', 'result_zip', 'full_markdown', 'content_list',
                    'layout', 'model', 'asset', 'parse_chunk_source',
                    'parse_chunk_result', 'normalized_markdown', 'page_map',
                    'enriched_markdown', 'tree', 'diagnostics'
                )
            ),
            CONSTRAINT ck_document_artifact_size CHECK (size_bytes >= 0),
            FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        INSERT INTO document_artifacts (
            id, document_id, kind, relpath, mime_type, size_bytes, sha256,
            pipeline_generation, created_at
        )
        SELECT id, document_id, kind, relpath, mime_type, size_bytes, sha256,
               pipeline_generation, created_at
        FROM document_artifacts_pre_v4
        """
    )
    connection.execute("DROP TABLE document_artifacts_pre_v4")
    connection.execute(
        "CREATE INDEX idx_document_artifacts_document_kind "
        "ON document_artifacts(document_id, kind)"
    )


def _require_current_schema(connection: sqlite3.Connection) -> None:
    expected_tables = set(Base.metadata.tables)
    existing_tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    missing_tables = sorted(expected_tables - existing_tables)
    if missing_tables:
        raise RuntimeError(f"数据库 schema 不完整: {missing_tables}")
    existing_columns = {
        table_name: {
            str(row[1])
            for row in connection.execute(
                f'PRAGMA table_info("{table_name}")'
            ).fetchall()
        }
        for table_name in _REQUIRED_COLUMNS
    }
    missing_columns = sorted(
        f"{table_name}.{column_name}"
        for table_name, required_columns in _REQUIRED_COLUMNS.items()
        for column_name in required_columns - existing_columns[table_name]
    )
    if missing_columns:
        raise RuntimeError(f"数据库 schema 缺少字段: {missing_columns}")


def initialize_database(factory: SQLiteConnectionFactory) -> None:
    """Create the current schema and migrate existing databases forward.

    Pre-release databases already contain the business tables. Their historical
    marker is normalized after the additive message-trace migration succeeds.
    """
    Base.metadata.create_all(factory.engine)

    connection = factory.connect()
    try:
        # SQLite cannot rebuild a referenced parent table while FK enforcement is on.
        # The migration validates the complete graph before committing and restores
        # enforcement on this connection in ``finally``.
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        _migrate_message_trace(connection)
        _migrate_heading_recovery_enabled(connection)
        _migrate_pipeline_generation(connection)
        _migrate_document_pipeline(connection)
        _migrate_document_tombstones(connection)
        _migrate_parse_chunk_plan(connection)
        _require_current_schema(connection)
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"数据库外键校验失败: {violations[:5]}")
        applied = {
            int(row[0])
            for row in connection.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        }
        if applied != {SCHEMA_VERSION}:
            connection.execute("DELETE FROM schema_migrations")
            _record_current_schema(connection)
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.close()


def database_is_initialized(factory: SQLiteConnectionFactory) -> bool:
    """Return whether the current schema version has been recorded."""
    connection = factory.connect()
    try:
        row = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (SCHEMA_VERSION,),
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()


__all__ = ["SCHEMA_VERSION", "database_is_initialized", "initialize_database"]
