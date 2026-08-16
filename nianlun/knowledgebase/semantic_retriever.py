"""Runtime adapter for semantic document routing."""

from __future__ import annotations

from nianlun.models.embedding import TextEmbedder
from nianlun.indexing.fts.postprocess import postprocess_node_hits, top_doc_ids
from nianlun.indexing.vector.config import VECTOR_DERIVE_LIMIT, VECTOR_NODE_PER_DOC
from nianlun.indexing.vector.store import DocVectorStore


class SemanticDocumentRetriever:
    """Embed a query and return grouped document-level vector hits."""

    def __init__(self, store: DocVectorStore, embedder: TextEmbedder) -> None:
        self.store = store
        self.embedder = embedder

    def search(self, query: str, limit: int = 5) -> list[dict]:
        if not query.strip():
            return []
        vector = self.embedder.embed_query(query)
        hits = self.store.search(vector, limit=VECTOR_DERIVE_LIMIT)
        doc_ids = top_doc_ids(hits, doc_top_n=limit)
        documents: list[dict] = []
        best_by_doc: dict[str, dict] = {}
        for hit in hits:
            doc_id = str(hit.get("doc_id") or "")
            if doc_id not in doc_ids:
                continue
            current = best_by_doc.get(doc_id)
            if current is None or (hit.get("score") or 0) > (current.get("score") or 0):
                best_by_doc[doc_id] = hit
        for doc_id in doc_ids:
            hit = best_by_doc.get(doc_id)
            if hit is not None:
                documents.append(
                    {
                        "doc_id": doc_id,
                        "doc_name": hit.get("doc_name"),
                        "score": hit.get("score"),
                        "node_hints": [],
                    }
                )
        node_hints = postprocess_node_hits(
            [
                hit
                for hit in hits
                if hit.get("doc_id") in doc_ids and hit.get("node_id")
            ],
            per_doc_cap=VECTOR_NODE_PER_DOC,
        )
        documents_by_id = {item["doc_id"]: item for item in documents}
        for hit in node_hints:
            document = documents_by_id.get(hit.get("doc_id"))
            if document is None:
                continue
            document["node_hints"].append(
                {
                    "node_id": hit.get("node_id"),
                    "title": hit.get("title"),
                    "line_num": hit.get("line_num"),
                }
            )
        return documents


__all__ = ["SemanticDocumentRetriever"]
