from types import SimpleNamespace
import hashlib
import json

import pytest
from sqlalchemy import select

from app.api_server.database import SQLiteConnectionFactory, initialize_database
from app.api_server.repositories.image_storage_profiles import (
    ImageStorageProfileRepository,
)
from app.api_server.database.models import ImageStorageProfile, AnswerImageBlob


def locator(root):
    canonical = json.dumps(
        {"backend": "filesystem", "root": str(root)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return SimpleNamespace(
        backend="filesystem",
        canonical_json=canonical,
        fingerprint=hashlib.sha256(canonical.encode()).hexdigest(),
    )


@pytest.fixture
def repository(tmp_path):
    factory = SQLiteConnectionFactory(tmp_path / "db.sqlite")
    initialize_database(factory)
    yield ImageStorageProfileRepository(factory)
    factory.dispose()


def test_profile_locator_is_pinned_and_all_old_profiles_remain_required(repository):
    profiles = {"old": locator("/old"), "current": locator("/current")}
    repository.bind_configured_profiles(profiles)
    repository.bind_configured_profiles(profiles)
    with repository.factory.session_scope() as session:
        records = session.scalars(select(ImageStorageProfile)).all()
        assert {row.profile_id for row in records} == {"old", "current"}
        assert {row.locator_fingerprint for row in records} == {
            profile.fingerprint for profile in profiles.values()
        }
    with pytest.raises(RuntimeError, match="Missing.*old"):
        repository.bind_configured_profiles({"current": profiles["current"]})


def test_changed_locator_refuses_start_and_registration_is_atomic(repository):
    repository.bind_configured_profiles({"old": locator("/old")})
    with pytest.raises(RuntimeError, match="changed.*old"):
        repository.bind_configured_profiles(
            {"new": locator("/new"), "old": locator("/different")}
        )
    with repository.factory.session_scope() as session:
        assert session.get(ImageStorageProfile, "new") is None
        assert (
            session.get(ImageStorageProfile, "old").locator_json
            == locator("/old").canonical_json
        )


def test_upgrade_creates_profile_schema_without_rewriting_console_contract(repository):
    from app.api_server.database import migrations

    connection = repository.factory.connect()
    try:
        connection.execute("DROP TABLE image_storage_profiles")
        connection.execute("UPDATE schema_migrations SET version=15")
        connection.commit()
    finally:
        connection.close()
    initialize_database(repository.factory)
    initialize_database(repository.factory)
    repository.bind_configured_profiles({"local": locator("/local")})
    assert migrations.database_is_initialized(repository.factory)


@pytest.mark.parametrize(
    "status", ["uploading", "ready", "retired", "deleting", "deleted"]
)
def test_blob_profiles_are_required_even_before_locator_registration(
    repository, status
):
    from tests.api_server.test_chat_persistence import _seed_application

    _seed_application(repository.factory)
    now = "2026-10-25T00:00:00+00:00"
    with repository.factory.session_scope(write=True) as session:
        session.add(
            AnswerImageBlob(
                id="blob",
                knowledge_base_id="kb-1",
                sha256="a" * 64,
                media_type="image/png",
                size_bytes=4,
                width=1,
                height=1,
                backend="filesystem",
                storage_profile_id="old",
                object_key="old/image.png",
                status=status,
                created_at=now,
                updated_at=now,
            )
        )
    with pytest.raises(RuntimeError, match="Missing.*old"):
        repository.bind_configured_profiles({"new": locator("/new")})
    repository.bind_configured_profiles({"old": locator("/old")})


def test_conflicting_blob_backends_cannot_be_hidden_by_same_profile_id(repository):
    from tests.api_server.test_chat_persistence import _seed_application

    _seed_application(repository.factory)
    now = "2026-10-25T00:00:00+00:00"
    with repository.factory.session_scope(write=True) as session:
        for backend in ("s3", "filesystem"):
            session.add(
                AnswerImageBlob(
                    id=backend,
                    knowledge_base_id="kb-1",
                    sha256="a" * 64,
                    media_type="image/png",
                    size_bytes=4,
                    width=1,
                    height=1,
                    backend=backend,
                    storage_profile_id="same",
                    object_key=f"{backend}/image.png",
                    status="deleted",
                    created_at=now,
                    updated_at=now,
                )
            )
    with pytest.raises(RuntimeError, match="changed.*same"):
        repository.bind_configured_profiles({"same": locator("/same")})
