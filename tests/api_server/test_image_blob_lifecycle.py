from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
import threading
import time

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from app.api_server.database import initialize_database
from app.api_server.database.models import AnswerImageBlob, Application, KnowledgeBase
from tests.api_server.test_answer_image_service import image_context as image_context
from tests.api_server.test_answer_image_service import save_result
from app.api_server.storage.image_blob_store import BlobStoreError, verify_image_bytes


class PausedImageBlobStore:
    """In-memory remote store with a controlled first PUT, without SDK/network IO."""

    def __init__(self, *, first_upload_fails=False):
        self.started = threading.Event()
        self.release = threading.Event()
        self.first_upload_fails = first_upload_fails
        self.put_locations = []
        self.objects = {}

    def put(self, location, data, metadata):
        self.put_locations.append(location)
        if len(self.put_locations) == 1:
            self.started.set()
            assert self.release.wait(3), "first upload was never released"
            if self.first_upload_fails:
                raise BlobStoreError("storage_unavailable")
        verify_image_bytes(data, metadata)
        self.objects[location.object_key] = data
        return location

    @contextmanager
    def open(self, location, metadata):
        data = self.objects[location.object_key]
        verify_image_bytes(data, metadata)
        with BytesIO(data) as source:
            yield source


@pytest.mark.parametrize("first_upload_fails", [False, True])
def test_concurrent_remote_upload_waits_for_owner_before_reuse_or_retry(
    image_context, monkeypatch, first_upload_fails
):
    context = image_context
    store = PausedImageBlobStore(first_upload_fails=first_upload_fails)
    context.service.backend = "s3"
    context.service.blob_store = store
    attempted = threading.Event()
    reserve = context.repository.create_uploading

    def observe_reservation(**values):
        try:
            return reserve(**values)
        finally:
            if store.started.is_set():
                attempted.set()

    monkeypatch.setattr(context.repository, "create_uploading", observe_reservation)
    context.chat.begin_turn(
        application_id="app-1",
        conversation_id="conversation-2",
        user_message_id="user-2",
        assistant_message_id="assistant-2",
        user_content="show image",
        now=context.now,
    )

    def first_request():
        with context.service.completion_scope():
            result = context.service.prepare_result("app-1", context.result)
            save_result(context, result)
            return result

    def second_request():
        with context.service.completion_scope():
            result = context.service.prepare_result("app-1", context.result)
            context.chat.complete_turn(
                application_id="app-1",
                conversation_id="conversation-2",
                assistant_message_id="assistant-2",
                answer=result["answer"],
                route="retrieval",
                snippets=result["retrieved_snippets"],
                images=result["images"],
                answer_format_version=result["answer_format_version"],
                answer_output_mode=result["answer_output_mode"],
                now=context.now,
            )
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_request)
        try:
            assert store.started.wait(2)
            second = pool.submit(second_request)
            assert attempted.wait(2)
        finally:
            store.release.set()
        first_result = first.result(timeout=3)
        second_result = second.result(timeout=3)

    first_image = first_result["images"][0]
    second_image = second_result["images"][0]
    assert second_image["status"] == "available"
    assert context.repository.message_images("assistant-2")[0]["status"] == "available"
    assert context.repository.blob(second_image["blob_id"])["status"] == "ready"
    if first_upload_fails:
        assert first_image["status"] == "unavailable"
        assert len(store.put_locations) == 2
        assert store.put_locations[0].object_key != store.put_locations[1].object_key
    else:
        assert second_image["blob_id"] == first_image["blob_id"]
        assert len(store.put_locations) == 1
        assert second_image["_read_token"]


def test_concurrent_remote_upload_times_out_without_stealing_owner(
    image_context, monkeypatch
):
    import app.api_server.services.answer_image_service as module

    context = image_context
    store = PausedImageBlobStore()
    context.service.backend = "s3"
    context.service.blob_store = store
    monkeypatch.setattr(module, "_UPLOAD_WAIT_SECONDS", 0.02)

    def first_request():
        with context.service.completion_scope():
            result = context.service.prepare_result("app-1", context.result)
            save_result(context, result)
            return result

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(first_request)
        try:
            assert store.started.wait(2)
            started = time.monotonic()
            with context.service.completion_scope():
                second = context.service.prepare_result("app-1", context.result)
            assert 0.02 <= time.monotonic() - started < 1
            assert second["images"][0]["status"] == "unavailable"
            assert second["images"][0]["unavailable_reason"] == "storage_unavailable"
            assert "blob_id" not in second["images"][0]
            assert len(store.put_locations) == 1
        finally:
            store.release.set()
        result = first.result(timeout=3)
    blob = context.repository.blob(result["images"][0]["blob_id"])
    assert blob["status"] == "ready"


def test_reservation_rechecks_blob_that_became_ready_after_initial_lookup(
    image_context, monkeypatch
):
    context = image_context
    first = context.service.prepare_result("app-1", context.result)
    save_result(context, first)
    ready_blob = context.repository.ready_blob
    initial_lookup = True

    def stale_first_lookup(*args):
        nonlocal initial_lookup
        if initial_lookup:
            initial_lookup = False
            return None
        return ready_blob(*args)

    monkeypatch.setattr(context.repository, "ready_blob", stale_first_lookup)
    with context.service.completion_scope():
        second = context.service.prepare_result("app-1", context.result)
    assert second["images"][0]["status"] == "available"
    assert second["images"][0]["blob_id"] == first["images"][0]["blob_id"]


def test_prepared_upload_becomes_ready_only_with_message_commit(image_context):
    context = image_context
    result = context.service.prepare_result("app-1", context.result)
    image = result["images"][0]
    assert context.repository.blob(image["blob_id"])["status"] == "uploading"
    save_result(context, result)
    assert context.repository.blob(image["blob_id"])["status"] == "ready"


def test_duplicate_active_blob_is_rejected_and_retired_key_is_reusable(image_context):
    context = image_context
    result = context.service.prepare_result("app-1", context.result)
    first = context.repository.blob(result["images"][0]["blob_id"])
    values = {key: value for key, value in first.items()}
    values.update(id="duplicate", object_key="answer-images/other.png")
    with pytest.raises(IntegrityError):
        with context.factory.session_scope(write=True) as session:
            session.add(AnswerImageBlob(**values))
    context.repository.retire_blob(first["id"])
    with context.factory.session_scope(write=True) as session:
        session.add(AnswerImageBlob(**values))


def test_blob_cleanup_locator_survives_kb_deletion_and_repeat_migration(image_context):
    context = image_context
    result = context.service.prepare_result("app-1", context.result)
    blob_id = result["images"][0]["blob_id"]
    initialize_database(context.factory)
    initialize_database(context.factory)
    with context.factory.session_scope(write=True) as session:
        session.execute(delete(Application).where(Application.id == "app-1"))
        session.execute(delete(KnowledgeBase).where(KnowledgeBase.id == "kb-1"))
    blob = context.repository.blob(blob_id)
    assert blob is not None
    assert blob["object_key"].startswith("answer-images/kb-1/")


def test_expired_owner_cannot_commit_or_renew_after_replacement(image_context):
    context = image_context
    result = context.service.prepare_result("app-1", context.result)
    image = result["images"][0]
    blob_id = image["blob_id"]
    token = image["_upload_token"]
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with context.factory.session_scope(write=True) as session:
        session.get(AnswerImageBlob, blob_id).lease_expires_at = expired
    replacement = context.service.prepare_result("app-1", context.result)
    assert replacement["images"][0]["blob_id"] != blob_id
    assert context.repository.renew_upload(blob_id, token) is False
    with pytest.raises(ValueError, match="租约|状态"):
        save_result(context, result)
    assert context.repository.blob(blob_id)["status"] == "retired"


def test_read_lease_blocks_collection_and_terminal_owner_cannot_renew(image_context):
    context = image_context
    result = context.service.prepare_result("app-1", context.result)
    save_result(context, result)
    blob = context.repository.blob(result["images"][0]["blob_id"])
    token = context.repository.acquire_read(blob)
    assert token
    context.repository.retire_blob(blob["id"])
    assert context.repository.claim_collection(blob, grace_hours=0) is None
    context.repository.release_read(token)
    claim = context.repository.claim_collection(blob, grace_hours=0)
    assert claim and claim["status"] == "deleting"
    assert context.repository.acquire_read(blob) is None
    assert context.repository.renew_read(token) is False


def test_gc_keeps_receipt_and_retries_exact_late_object(image_context):
    context = image_context
    result = context.service.prepare_result("app-1", context.result)
    blob_id = result["images"][0]["blob_id"]
    context.repository.retire_blob(blob_id)
    assert context.service.collect_orphans(grace_hours=0) == 1
    assert context.repository.blob(blob_id)["status"] == "deleted"
    assert context.service.collect_orphans(grace_hours=0) == 1


def test_completion_scope_rolls_back_uncommitted_upload(image_context):
    context = image_context
    with pytest.raises(RuntimeError, match="cancelled"):
        with context.service.completion_scope():
            result = context.service.prepare_result("app-1", context.result)
            blob_id = result["images"][0]["blob_id"]
            raise RuntimeError("cancelled")
    assert context.repository.blob(blob_id)["status"] == "retired"
    assert context.repository.message_images("assistant-1") == []


def test_source_deleted_before_commit_preserves_text_and_removes_marker(image_context):
    from app.api_server.database.models import Document, Message

    context = image_context
    result = context.service.prepare_result("app-1", context.result)
    with context.factory.session_scope(write=True) as session:
        session.delete(session.get(Document, "doc-1"))
    save_result(context, result)
    with context.factory.session_scope() as session:
        message = session.get(Message, "assistant-1")
        assert "Answer" in message.content
        assert "nianlun-image:" not in message.content
        assert message.answer_output_mode == "text"
    assert context.repository.message_images("assistant-1")[0]["blob_id"] is None


def test_confirmed_missing_read_retires_and_unbinds(image_context):
    from app.api_server.common.errors import ApiError

    context = image_context
    result = context.service.prepare_result("app-1", context.result)
    save_result(context, result)
    image = result["images"][0]
    blob = context.repository.blob(image["blob_id"])
    (context.service.blob_store.root / blob["object_key"]).unlink()
    with pytest.raises(ApiError):
        context.service.read_content(
            "app-1", "conversation-1", "assistant-1", image["id"]
        )
    assert context.repository.blob(blob["id"])["status"] == "retired"
    assert context.repository.message_images("assistant-1")[0]["blob_id"] is None


def test_ready_reuse_lease_survives_direct_prepare_until_commit(image_context):
    from app.api_server.database.models import MessageSource, MessageImage, Message

    context = image_context
    result = context.service.prepare_result("app-1", context.result)
    save_result(context, result)
    with context.factory.session_scope(write=True) as session:
        session.execute(
            delete(MessageSource).where(MessageSource.message_id == "assistant-1")
        )
        session.execute(
            delete(MessageImage).where(MessageImage.message_id == "assistant-1")
        )
        session.get(Message, "assistant-1").status = "pending"
    reused = context.service.prepare_result("app-1", context.result)
    assert reused["images"][0]["blob_id"] == result["images"][0]["blob_id"]
    save_result(context, reused)


def test_populated_legacy_fk_rebuild_is_repeatable(image_context):
    from app.api_server.database.migrations import _migrate_image_blob_lifecycle

    context = image_context
    result = context.service.prepare_result("app-1", context.result)
    save_result(context, result)
    blob_id = result["images"][0]["blob_id"]
    connection = context.factory.connect()
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='answer_image_blobs'"
        ).fetchone()[0]
        definition = definition.replace(
            "CREATE TABLE answer_image_blobs", "CREATE TABLE legacy_blobs"
        )
        definition = (
            definition[: definition.rfind(")")]
            + ", FOREIGN KEY(knowledge_base_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE)"
        )
        connection.execute(definition)
        connection.execute("INSERT INTO legacy_blobs SELECT * FROM answer_image_blobs")
        connection.execute("DROP TABLE answer_image_blobs")
        connection.execute("ALTER TABLE legacy_blobs RENAME TO answer_image_blobs")
        _migrate_image_blob_lifecycle(connection)
        _migrate_image_blob_lifecycle(connection)
        assert not connection.execute(
            "PRAGMA foreign_key_list(answer_image_blobs)"
        ).fetchall()
        assert (
            connection.execute("SELECT id FROM answer_image_blobs").fetchone()[0]
            == blob_id
        )
        assert (
            connection.execute("SELECT blob_id FROM message_images").fetchone()[0]
            == blob_id
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_media_lock_fixed_inode_and_independent_process(tmp_path):
    import subprocess
    import sys
    from app.api_server.services.image_blob_lifecycle import media_lock

    root = tmp_path / "locks"
    with media_lock(root, [("kb", "sha")]):
        lock_path = next(root.iterdir())
        inode = lock_path.stat().st_ino
        script = "import os,fcntl,sys; fd=os.open(sys.argv[1],os.O_RDWR); fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)"
        child = subprocess.run(
            [sys.executable, "-c", script, str(lock_path)], capture_output=True
        )
        assert child.returncode != 0
    with media_lock(root, [("kb", "sha")]):
        assert lock_path.stat().st_ino == inode
    child = subprocess.run(
        [sys.executable, "-c", script, str(lock_path)], capture_output=True
    )
    assert child.returncode == 0


def test_orphan_grace_setting_positive_and_environment(monkeypatch, tmp_path):
    from app.api_server.config.settings import ApiServerSettings

    monkeypatch.setenv("NIANLUN_API_ANSWER_IMAGE_ORPHAN_GRACE_HOURS", "7")
    assert ApiServerSettings.from_env().answer_image_orphan_grace_hours == 7
    with pytest.raises(ValueError):
        ApiServerSettings(answer_image_orphan_grace_hours=0)


def test_collection_claim_expiry_fences_stale_completion(image_context):
    context = image_context
    result = context.service.prepare_result("app-1", context.result)
    blob = context.repository.blob(result["images"][0]["blob_id"])
    context.repository.retire_blob(blob["id"])
    first = context.repository.claim_collection(blob, grace_hours=0)
    assert first and context.repository.claim_collection(blob, grace_hours=0) is None
    with context.factory.session_scope(write=True) as session:
        session.get(AnswerImageBlob, blob["id"]).lease_expires_at = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
    second = context.repository.claim_collection(blob, grace_hours=0)
    assert second and second["lease_token"] != first["lease_token"]
    assert not context.repository.renew_collection(first)
    assert not context.repository.finish_collection(first)
    assert context.repository.finish_collection(second)


def test_deleted_source_keeps_alternate_binding_until_last_source_deleted(
    image_context,
):
    import json
    from app.api_server.database.models import Document, MessageImage
    from app.api_server.repositories.answer_images import invalidate_source_images

    context = image_context
    result = context.service.prepare_result("app-1", context.result)
    save_result(context, result)
    with context.factory.session_scope(write=True) as session:
        source = session.get(Document, "doc-1")
        values = {
            column.name: getattr(source, column.name)
            for column in source.__table__.columns
        }
        values["id"] = "alternate"
        values["source_sha256"] = "c" * 64
        session.add(Document(**values))
        attachment = session.get(MessageImage, result["images"][0]["id"])
        bindings = json.loads(attachment.source_bindings_json)
        attachment.source_bindings_json = json.dumps(
            bindings + [{**bindings[0], "doc_id": "alternate"}]
        )
        source.status = "deleted"
        assert invalidate_source_images(session) == 0
    assert context.repository.message_images("assistant-1")[0]["blob_id"]
    with context.factory.session_scope(write=True) as session:
        session.get(Document, "alternate").status = "deleted"
        assert invalidate_source_images(session) == 1
    assert context.repository.message_images("assistant-1")[0]["blob_id"] is None


def test_gc_delete_failure_retains_retry_claim_and_shutdown_drains(
    image_context, monkeypatch
):
    import threading
    from app.api_server.storage.image_blob_store import BlobStoreError

    context = image_context
    result = context.service.prepare_result("app-1", context.result)
    blob_id = result["images"][0]["blob_id"]
    context.repository.retire_blob(blob_id)

    def fail(*args, **kwargs):
        raise BlobStoreError("storage_permission_denied")

    monkeypatch.setattr(context.service.blob_store, "delete", fail)
    assert context.service.collect_orphans(grace_hours=0) == 0
    assert context.repository.blob(blob_id)["status"] == "deleting"
    started = threading.Event()
    released = threading.Event()
    monkeypatch.setattr(
        context.service,
        "collect_orphans",
        lambda **kwargs: (started.set(), released.wait(2)),
    )
    context.service.start_gc()
    assert started.wait(2)
    closed = threading.Event()
    worker = threading.Thread(target=lambda: (context.service.shutdown(), closed.set()))
    worker.start()
    assert not closed.wait(0.05)
    released.set()
    worker.join(2)
    assert closed.is_set()


@pytest.mark.parametrize("reason", ["storage_permission_denied", "storage_unavailable"])
def test_transient_read_does_not_retire_committed_blob(
    image_context, monkeypatch, reason
):
    from app.api_server.common.errors import ApiError
    from app.api_server.storage.image_blob_store import BlobStoreError

    context = image_context
    result = context.service.prepare_result("app-1", context.result)
    save_result(context, result)
    image = result["images"][0]

    def fail(*args, **kwargs):
        raise BlobStoreError(reason)

    monkeypatch.setattr(context.service.blob_store, "open", fail)
    with pytest.raises(ApiError):
        context.service.read_content(
            "app-1", "conversation-1", "assistant-1", image["id"]
        )
    assert context.repository.blob(image["blob_id"])["status"] == "ready"
    assert (
        context.repository.message_images("assistant-1")[0]["blob_id"]
        == image["blob_id"]
    )


def test_late_put_is_deleted_again_without_touching_replacement(image_context):
    from app.api_server.storage import ImageBlobLocation, ImageMetadata

    context = image_context
    result = context.service.prepare_result("app-1", context.result)
    old = context.repository.blob(result["images"][0]["blob_id"])
    context.repository.retire_blob(old["id"])
    assert context.service.collect_orphans(grace_hours=0) == 1
    replacement = context.service.prepare_result("app-1", context.result)
    metadata = ImageMetadata.model_validate(
        {key: old[key] for key in ImageMetadata.model_fields}
    )
    location = ImageBlobLocation.model_validate(
        {key: old[key] for key in ImageBlobLocation.model_fields}
    )
    context.service.blob_store.put(location, context.raw, metadata)
    assert context.service.collect_orphans(grace_hours=0) == 1
    assert not (context.service.blob_store.root / old["object_key"]).exists()
    assert context.service.read_prepared_image(replacement["images"][0]) == context.raw


def test_competing_upload_owner_does_not_receive_staging_bytes(
    image_context, monkeypatch
):
    import app.api_server.services.answer_image_service as module

    monkeypatch.setattr(module, "_UPLOAD_WAIT_SECONDS", 0.02)
    context = image_context
    first = context.service.prepare_result("app-1", context.result)
    second = context.service.prepare_result("app-1", context.result)
    assert first["images"][0]["status"] == "available"
    assert second["images"][0]["status"] == "unavailable"
    assert "blob_id" not in second["images"][0]


def test_duplicate_migration_uses_utc_and_repairs_existing_naive_retirement(
    image_context,
):
    from app.api_server.database.migrations import _migrate_image_blob_lifecycle

    context = image_context
    result = context.service.prepare_result("app-1", context.result)
    original = context.repository.blob(result["images"][0]["blob_id"])
    connection = context.factory.connect()
    try:
        connection.execute("DROP INDEX uq_answer_image_blob_active")
        columns = list(original)
        duplicate = {
            **original,
            "id": "migrated-duplicate",
            "object_key": "answer-images/duplicate.png",
        }
        connection.execute(
            "INSERT INTO answer_image_blobs ("
            + ",".join(columns)
            + ") VALUES ("
            + ",".join("?" for _ in columns)
            + ")",
            tuple(duplicate[column] for column in columns),
        )
        _migrate_image_blob_lifecycle(connection)
        retired = context.repository.blob("migrated-duplicate")
        assert retired["status"] == "retired"
        assert datetime.fromisoformat(retired["retired_at"]).tzinfo is not None
        connection.execute(
            "UPDATE answer_image_blobs SET retired_at='2020-01-01 00:00:00' WHERE id='migrated-duplicate'"
        )
        _migrate_image_blob_lifecycle(connection)
        _migrate_image_blob_lifecycle(connection)
        repaired = context.repository.blob("migrated-duplicate")
        assert datetime.fromisoformat(repaired["retired_at"]).tzinfo is not None
        assert context.service.collect_orphans(grace_hours=24) == 1
        assert context.repository.blob("migrated-duplicate")["status"] == "deleted"
    finally:
        connection.close()
