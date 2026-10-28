"""Keep legacy CLI tools outside API-managed immutable workspaces."""

from pathlib import Path


def require_legacy_workspace(path: str | Path) -> None:
    """Reject known API-owned roots and any file/descendant within them."""
    resolved = Path(path).expanduser().resolve()
    for root in (resolved, *resolved.parents):
        if any(
            (root / marker).exists()
            for marker in (
                "snapshots",
                ".api-server.lock",
                ".tree-build-options.json",
                ".api-storage-owner.json",
            )
        ):
            raise RuntimeError(
                "API-owned workspace cannot be accessed by legacy CLI tools; "
                "use the API for indexing, or stop services and run "
                "python -m app.api_server.backfill_document_images for image catalogs"
            )
