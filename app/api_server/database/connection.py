"""SQLite connection factory with API-server defaults."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Generator

from sqlalchemy import URL, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def _configure_sqlite_connection(
    connection: sqlite3.Connection, busy_timeout_ms: int
) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")


@dataclass(frozen=True)
class SQLiteConnectionFactory:
    """Create short-lived SQLite connections and SQLAlchemy sessions."""

    path: Path
    timeout_seconds: float = 30.0
    busy_timeout_ms: int = 5_000
    _engine: Engine = field(init=False, repr=False, compare=False)
    _session_factory: sessionmaker[Session] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            URL.create("sqlite", database=str(self.path)),
            connect_args={
                "timeout": self.timeout_seconds,
                "check_same_thread": False,
            },
            pool_pre_ping=True,
        )

        def configure_engine_connection(
            dbapi_connection: sqlite3.Connection, _connection_record: object
        ) -> None:
            _configure_sqlite_connection(dbapi_connection, self.busy_timeout_ms)

        event.listen(engine, "connect", configure_engine_connection)

        object.__setattr__(self, "_engine", engine)
        object.__setattr__(self, "_session_factory", sessionmaker(bind=engine))

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.timeout_seconds,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        _configure_sqlite_connection(connection, self.busy_timeout_ms)
        return connection

    @property
    def engine(self) -> Engine:
        """Return the SQLAlchemy engine shared by this database factory."""
        return self._engine

    @property
    def session_factory(self) -> sessionmaker[Session]:
        """Return the SQLAlchemy session factory."""
        return self._session_factory

    def session(self) -> Session:
        """Create one independent ORM session."""
        return self.session_factory()

    @contextmanager
    def session_scope(self, *, write: bool = False) -> Generator[Session, None, None]:
        """Run one repository operation in a short-lived ORM transaction."""
        session = self.session()
        try:
            if write:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        """Release pooled ORM connections, primarily for tests and shutdown."""
        self._engine.dispose()


__all__ = ["SQLiteConnectionFactory"]
