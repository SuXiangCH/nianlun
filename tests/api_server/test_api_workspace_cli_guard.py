from __future__ import annotations

from pathlib import Path
import sys

import pytest


@pytest.mark.parametrize(
    "marker",
    [
        "snapshots",
        ".api-server.lock",
        ".tree-build-options.json",
        ".api-storage-owner.json",
    ],
)
def test_legacy_cli_guard_rejects_owned_workspace_and_descendants(
    tmp_path: Path, marker: str
) -> None:
    from nianlun.knowledgebase.workspace_ownership import require_legacy_workspace

    workspace = tmp_path / "api"
    workspace.mkdir()
    if marker == "snapshots":
        (workspace / marker).mkdir()
    else:
        (workspace / marker).touch()
    with pytest.raises(RuntimeError, match="API-owned workspace"):
        require_legacy_workspace(workspace / "artifacts" / "document" / "full.md")
    require_legacy_workspace(tmp_path / "legacy")


def test_tree_guard_precedes_clean_and_model_call(tmp_path: Path, monkeypatch) -> None:
    from nianlun.indexing.tree import cli

    workspace = tmp_path / "api"
    workspace.mkdir()
    (workspace / "snapshots").mkdir()
    document = workspace / "document.json"
    document.write_text("keep")
    monkeypatch.setattr(
        cli, "build_workspace_doc", lambda *args, **kwargs: pytest.fail("model called")
    )
    with pytest.raises(RuntimeError, match="API-owned workspace"):
        cli.cmd_reindex(str(workspace), ["input.md"], None, True, clean=True)
    assert document.read_text() == "keep"
    with pytest.raises(RuntimeError, match="API-owned workspace"):
        cli.cmd_single("input.md", str(workspace / "output.json"), None, False)


@pytest.mark.parametrize("kind", ["fts", "vector"])
def test_index_cli_guard_precedes_external_build(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    from nianlun.indexing.fts import cli as fts
    from nianlun.indexing.vector import cli as vector

    workspace = tmp_path / "api"
    workspace.mkdir()
    (workspace / "snapshots").mkdir()
    module = fts if kind == "fts" else vector
    build_name = "build_node_fts" if kind == "fts" else "build_doc_vectors"
    monkeypatch.setattr(
        module, build_name, lambda *args, **kwargs: pytest.fail("external build called")
    )
    monkeypatch.setattr(sys, "argv", ["cli", "--workspace", str(workspace)])
    with pytest.raises(RuntimeError, match="API-owned workspace"):
        module.main()


def test_agent_cli_guard_precedes_runtime_factory(tmp_path: Path, monkeypatch) -> None:
    from nianlun.agent import cli

    workspace = tmp_path / "api"
    workspace.mkdir()
    (workspace / "snapshots").mkdir()
    monkeypatch.setattr(cli, "WORKSPACE_DIR", workspace, raising=False)
    monkeypatch.setattr(
        cli, "AgentRuntimeFactory", lambda **kwargs: pytest.fail("runtime assembled")
    )
    with pytest.raises(RuntimeError, match="API-owned workspace"):
        cli.setup(tool_logging=False)
