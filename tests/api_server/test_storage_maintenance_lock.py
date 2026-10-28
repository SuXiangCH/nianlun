from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("service_name", ["fts", "vector", "documents", "pipeline"])
def test_workers_wait_until_storage_access_finishes(service_name: str) -> None:
    from types import SimpleNamespace
    from app.api_server.services.fts_index_service import FTSIndexService
    from app.api_server.services.vector_index_service import VectorIndexService
    from app.api_server.services.document_ingestion_service import (
        DocumentIngestionService,
    )
    from app.api_server.services.document_pipeline_service import (
        DocumentPipelineService,
    )

    calls = []
    executor = SimpleNamespace(
        shutdown=lambda *, wait, cancel_futures: calls.append((wait, cancel_futures))
    )
    event = SimpleNamespace(set=lambda: None)
    thread = SimpleNamespace(join=lambda **kwargs: calls.append(kwargs))
    if service_name == "fts":
        FTSIndexService.shutdown(
            SimpleNamespace(
                _shutdown_event=event, _schema_executor=executor, _executor=executor
            )
        )
    elif service_name == "vector":
        VectorIndexService.shutdown(SimpleNamespace(_executor=executor))
    elif service_name == "documents":
        DocumentIngestionService.shutdown(
            SimpleNamespace(
                _dispatcher_stop=event,
                _dispatcher_wake=event,
                _dispatcher_thread=thread,
                pipeline=SimpleNamespace(shutdown=lambda: None),
                _submission_executor=executor,
                _poll_executor=executor,
            )
        )
    else:
        DocumentPipelineService.shutdown(SimpleNamespace(_loop=None, _thread=thread))
    assert calls
    assert all(call == (True, True) or call == {} for call in calls)


def test_lifecycle_lock_is_nonblocking_and_process_shared(tmp_path: Path) -> None:
    from app.api_server.services.storage_maintenance_lock import (
        StorageMaintenanceBusy,
        StorageMaintenanceLock,
    )

    with StorageMaintenanceLock(tmp_path, exclusive=False):
        with StorageMaintenanceLock(tmp_path, exclusive=False):
            with pytest.raises(StorageMaintenanceBusy):
                StorageMaintenanceLock(tmp_path, exclusive=True)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; from app.api_server.services.storage_maintenance_lock import StorageMaintenanceLock; StorageMaintenanceLock(Path(__import__('sys').argv[1]), exclusive=True)",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "StorageMaintenanceBusy" in result.stderr
    with StorageMaintenanceLock(tmp_path, exclusive=True):
        with pytest.raises(StorageMaintenanceBusy):
            StorageMaintenanceLock(tmp_path, exclusive=False)
    assert (tmp_path / ".storage-maintenance.lock").is_file()


def test_build_services_conflict_precedes_database_initialization(
    tmp_path: Path, monkeypatch
) -> None:
    from app.api_server.config import ApiServerSettings
    from app.api_server.services import container
    from app.api_server.services.storage_maintenance_lock import (
        StorageMaintenanceBusy,
        StorageMaintenanceLock,
    )

    settings = ApiServerSettings(
        data_dir=tmp_path, workspace_root=tmp_path / "workspaces"
    )
    monkeypatch.setattr(
        container, "initialize_database", lambda _: pytest.fail("migration ran")
    )
    with StorageMaintenanceLock(tmp_path, exclusive=True):
        with pytest.raises(StorageMaintenanceBusy):
            container.build_services(settings)
    assert not (tmp_path / "nianlun.sqlite3").exists()


def test_failed_startup_releases_shared_lock(tmp_path: Path, monkeypatch) -> None:
    from app.api_server.config import ApiServerSettings
    from app.api_server.services import container
    from app.api_server.services.storage_maintenance_lock import StorageMaintenanceLock

    settings = ApiServerSettings(
        data_dir=tmp_path, workspace_root=tmp_path / "workspaces"
    )

    def fail(_):
        raise RuntimeError("migration failed")

    monkeypatch.setattr(container, "initialize_database", fail)
    with pytest.raises(RuntimeError, match="migration failed"):
        container.build_services(settings)
    with StorageMaintenanceLock(tmp_path, exclusive=True):
        pass


def test_shutdown_keeps_lock_until_all_storage_users_finish(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from app.api_server.services.container import shutdown_services
    from app.api_server.services.storage_maintenance_lock import (
        StorageMaintenanceBusy,
        StorageMaintenanceLock,
    )

    lock = StorageMaintenanceLock(tmp_path, exclusive=False)
    calls = []

    def drain():
        with pytest.raises(StorageMaintenanceBusy):
            StorageMaintenanceLock(tmp_path, exclusive=True)
        calls.append("drained")

    services = SimpleNamespace(
        maintenance_lock=lock,
        invocations=SimpleNamespace(close=drain),
        documents=SimpleNamespace(shutdown=drain),
        applications=SimpleNamespace(shutdown=drain),
        close_storage=lock.close,
    )
    shutdown_services(services)
    assert len(calls) == 3
    with StorageMaintenanceLock(tmp_path, exclusive=True):
        pass


def test_failed_shutdown_keeps_lock_until_process_exit(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from app.api_server.services.container import shutdown_services
    from app.api_server.services.storage_maintenance_lock import (
        StorageMaintenanceBusy,
        StorageMaintenanceLock,
    )

    lock = StorageMaintenanceLock(tmp_path, exclusive=False)

    def fail():
        raise RuntimeError("worker still running")

    services = SimpleNamespace(
        maintenance_lock=lock,
        documents=SimpleNamespace(shutdown=fail),
        close_storage=lock.close,
    )
    try:
        with pytest.raises(RuntimeError, match="worker still running"):
            shutdown_services(services)
        with pytest.raises(StorageMaintenanceBusy):
            StorageMaintenanceLock(tmp_path, exclusive=True)
    finally:
        # Explicitly simulate process teardown for this isolated test.
        lock._retain = False
        lock.close()
