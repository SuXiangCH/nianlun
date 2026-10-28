"""Offline upgrade and complete-backup restore against the actual v2 codebase."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from contextlib import closing

from app.api_server.database import SQLiteConnectionFactory, initialize_database
from app.api_server.database.migrations import SCHEMA_VERSION
from app.api_server.repositories import SQLiteMetadataRepository
from app.api_server.services.workspace_snapshot import (
    document_resource_closure,
    load_committed_manifest,
)
from tests.api_server.test_image_deployment import ROOT


LEGACY_REF = "3a40c3e1f5c9eade9bd966444010d25a2e9b6e88"


def test_offline_v2_upgrade_backfill_and_consistent_backup_restore(tmp_path):
    legacy = tmp_path / "legacy-code"
    legacy.mkdir()
    archive = subprocess.run(
        [
            "git",
            "archive",
            "--format=tar",
            LEGACY_REF,
            "app/__init__.py",
            "app/api_server",
            "nianlun",
        ],
        cwd=ROOT,
        capture_output=True,
        check=True,
        timeout=15,
    )
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as exported:
        exported.extractall(legacy, filter="data")
    data = tmp_path / "data"
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "TMPDIR", "LANG"}
    }
    env.update(
        PYTHON_DOTENV_DISABLED="1",
        NIANLUN_API_FTS_ENABLED="false",
        NIANLUN_API_VECTOR_ENABLED="false",
        NIANLUN_API_APPLICATION_ENABLED="false",
        NIANLUN_API_DATA_DIR=str(data),
        NIANLUN_WORKSPACE_ROOT=str(data / "workspaces"),
        NIANLUN_API_DATABASE_PATH=str(data / "db.sqlite3"),
    )
    # The shared fixture uses APIs present in both versions. Its imports resolve
    # against the exported legacy cwd, producing a genuine old schema + v2 manifest.
    seed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy,sys; from pathlib import Path; "
            "from app.api_server.database.migrations import SCHEMA_VERSION; "
            "runpy.run_path(sys.argv[1])['seed'](Path(sys.argv[2]), count=1); "
            "print(SCHEMA_VERSION)",
            str(ROOT / "tests/api_server/test_backfill_document_images.py"),
            str(data),
        ],
        cwd=legacy,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert int(seed.stdout.strip()) < SCHEMA_VERSION
    workspace = data / "workspaces/kb"
    assert (
        json.loads((workspace / "snapshots/r0/manifest.json").read_text())[
            "schema_version"
        ]
        == 2
    )
    factory = SQLiteConnectionFactory(data / "db.sqlite3")
    backup = tmp_path / "consistent-backup"
    with closing(sqlite3.connect(data / "db.sqlite3")) as connection:
        assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    shutil.copytree(data, backup)
    before = {
        str(file.relative_to(data)): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in data.rglob("*")
        if file.is_file()
    }

    def legacy_read(path):
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from pathlib import Path; "
                "from app.api_server.services.workspace_snapshot import read_manifest_file; "
                "print(read_manifest_file(Path(sys.argv[1])).schema_version)",
                str(path),
            ],
            cwd=legacy,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )

    assert legacy_read(workspace / "snapshots/r0/manifest.json").returncode == 0
    # All old invocations above have exited before the first v3 publication.
    initialize_database(factory)
    initialize_database(factory)
    repository = SQLiteMetadataRepository(factory)
    current = repository.get_committed_revision("kb", 0)
    assert load_committed_manifest(workspace, current).schema_version == 2
    factory.dispose()
    with closing(sqlite3.connect(data / "db.sqlite3")) as connection:
        assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    command = [
        sys.executable,
        "-m",
        "app.api_server.backfill_document_images",
        "--knowledge-base-id",
        "kb",
    ]
    dry = subprocess.run(
        [*command, "--dry-run"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert dry.returncode == 0, dry.stderr + dry.stdout
    assert json.loads(dry.stdout)["committed_revisions"] == 0
    committed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert json.loads(committed.stdout)["completed"] == 1
    revision = repository.get_committed_revision("kb", 1)
    manifest = load_committed_manifest(workspace, revision)
    assert manifest.schema_version == 3
    assert manifest.documents[0].image_catalog_relpath
    records = {item.relpath: item for item in manifest.artifact_files}
    for entry in manifest.documents:
        assert document_resource_closure(workspace, entry, records)
    assert legacy_read(workspace / "snapshots/r1/manifest.json").returncode != 0
    again = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert json.loads(again.stdout)["already_current"] == 1
    assert json.loads(again.stdout)["committed_revisions"] == 0
    factory.dispose()

    # Restore into another empty directory. Never overwrite even this test's
    # upgraded source; rollback means the complete database/workspace pair.
    restored = tmp_path / "restored"
    shutil.copytree(backup, restored)
    after = {
        str(file.relative_to(restored)): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in restored.rglob("*")
        if file.is_file()
    }
    assert before == after
    assert (
        legacy_read(restored / "workspaces/kb/snapshots/r0/manifest.json").returncode
        == 0
    )
    with closing(sqlite3.connect(restored / "db.sqlite3")) as connection:
        assert (
            connection.execute(
                "SELECT content_version FROM knowledge_bases WHERE id='kb'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT version FROM schema_migrations").fetchone()[0]
            < SCHEMA_VERSION
        )
