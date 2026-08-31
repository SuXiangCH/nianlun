"""SQLite schema bootstrap for the first public release."""

from __future__ import annotations

import sqlite3

from app.api_server.database.connection import SQLiteConnectionFactory
from app.api_server.database.models import Base


SCHEMA_VERSION = 2

_REQUIRED_COLUMNS = {
    table.name: {column.name for column in table.columns}
    for table in Base.metadata.sorted_tables
}


def _record_current_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO schema_migrations(version, name, applied_at)
        VALUES (?, 'message_trace', CURRENT_TIMESTAMP)
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
        _require_current_schema(connection)
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
