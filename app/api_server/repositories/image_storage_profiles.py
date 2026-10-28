"""Pin secret-free storage locators for reads and outstanding cleanup duties."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import select

from app.api_server.database import SQLiteConnectionFactory
from app.api_server.database.models import AnswerImageBlob, ImageStorageProfile


class StorageLocator(Protocol):
    @property
    def backend(self) -> str: ...
    @property
    def canonical_json(self) -> str: ...
    @property
    def fingerprint(self) -> str: ...


class ImageStorageProfileRepository:
    def __init__(self, factory: SQLiteConnectionFactory) -> None:
        self.factory = factory

    def bind_configured_profiles(self, profiles: Mapping[str, StorageLocator]) -> None:
        """Validate all historical responsibilities, then register new IDs atomically."""
        with self.factory.session_scope(write=True) as session:
            registered = {
                row.profile_id: row
                for row in session.scalars(select(ImageStorageProfile))
            }
            required = (
                session.execute(
                    select(
                        AnswerImageBlob.storage_profile_id, AnswerImageBlob.backend
                    ).distinct()
                )
                .tuples()
                .all()
            )
            for profile_id, row in registered.items():
                configured = profiles.get(profile_id)
                if configured is None:
                    raise RuntimeError(f"Missing image storage profile: {profile_id}")
                if (
                    configured.backend != row.backend
                    or configured.fingerprint != row.locator_fingerprint
                    or configured.canonical_json != row.locator_json
                ):
                    raise RuntimeError(f"Image storage locator changed: {profile_id}")
            for profile_id, backend in required:
                configured = profiles.get(profile_id)
                if configured is None:
                    raise RuntimeError(f"Missing image storage profile: {profile_id}")
                if configured.backend != backend:
                    raise RuntimeError(f"Image storage locator changed: {profile_id}")
            now = datetime.now(timezone.utc).isoformat()
            for profile_id, locator in profiles.items():
                if profile_id not in registered:
                    session.add(
                        ImageStorageProfile(
                            profile_id=profile_id,
                            backend=locator.backend,
                            locator_json=locator.canonical_json,
                            locator_fingerprint=locator.fingerprint,
                            created_at=now,
                        )
                    )
