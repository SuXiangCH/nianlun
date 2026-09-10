"""Bounded full-text fallback over one immutable workspace snapshot."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from nianlun.indexing.fts.build_records import build_records
from nianlun.knowledgebase.workspace_view import WorkspaceView


_TERM_RE = re.compile(r"[\w\u3400-\u9fff]+", re.UNICODE)


class LocalScanNodeRetriever:
    """Return FTS-shaped hits without claiming that a remote index is ready."""

    def __init__(
        self,
        *,
        workspace_dir: Path,
        snapshot_relpath: str,
        snapshot_manifest_sha256: str,
        knowledge_base_id: str,
        content_version: int,
    ) -> None:
        self.view = WorkspaceView.revision(
            workspace_dir,
            snapshot_relpath,
            snapshot_manifest_sha256,
            knowledge_base_id=knowledge_base_id,
            content_version=content_version,
        )
        self.knowledge_base_id = knowledge_base_id

    def search(
        self,
        query: str,
        limit: int = 512,
        doc_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        normalized_query = query.casefold().strip()
        if not normalized_query or limit <= 0:
            return []
        terms = list(dict.fromkeys(_TERM_RE.findall(normalized_query)))
        allowed = set(doc_ids) if doc_ids is not None else None
        scored: list[tuple[float, int, dict[str, Any]]] = []
        ordinal = 0
        for document_id in self.view.meta:
            if allowed is not None and document_id not in allowed:
                continue
            document = self.view.load_document(document_id)
            for record in build_records(
                document, knowledge_base_id=self.knowledge_base_id
            ):
                text = str(record.get("text") or "").casefold()
                term_score = sum(text.count(term) for term in terms)
                if term_score == 0:
                    continue
                score = float(term_score)
                if normalized_query in text:
                    score += 2.0
                hit = dict(record)
                hit["score"] = score
                scored.append((score, ordinal, hit))
                ordinal += 1
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in scored[:limit]]


__all__ = ["LocalScanNodeRetriever"]
