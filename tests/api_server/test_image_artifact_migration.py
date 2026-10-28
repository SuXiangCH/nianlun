import sqlite3

import pytest

from app.api_server.database.migrations import _rebuild_document_artifacts_for_v2_kinds


def test_existing_artifact_rows_survive_image_kind_upgrade():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE documents (id VARCHAR PRIMARY KEY)")
    connection.execute("INSERT INTO documents VALUES ('a')")
    connection.execute("""CREATE TABLE document_artifacts (
        id VARCHAR PRIMARY KEY, document_id VARCHAR NOT NULL,
        kind VARCHAR CHECK(kind IN ('tree', 'enriched_markdown')),
        relpath VARCHAR NOT NULL, mime_type VARCHAR NOT NULL,
        size_bytes INTEGER NOT NULL, sha256 VARCHAR NOT NULL,
        pipeline_generation INTEGER NOT NULL, created_at VARCHAR NOT NULL)""")
    row = ("old", "a", "tree", "tree.json", "application/json", 2, "a" * 64, 1, "now")
    connection.execute(
        "INSERT INTO document_artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", row
    )
    _rebuild_document_artifacts_for_v2_kinds(connection)
    _rebuild_document_artifacts_for_v2_kinds(connection)
    assert connection.execute("SELECT * FROM document_artifacts").fetchone() == row
    for kind in ("image_catalog", "image_asset"):
        connection.execute(
            "INSERT INTO document_artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, "a", kind, kind, "application/json", 2, "a" * 64, 1, "now"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO document_artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "invalid",
                "a",
                "invalid",
                "invalid",
                "application/json",
                2,
                "a" * 64,
                1,
                "now",
            ),
        )
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()
