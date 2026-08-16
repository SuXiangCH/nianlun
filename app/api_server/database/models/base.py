"""SQLAlchemy ORM models for the API server SQLite database."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all business tables managed by the API server."""
