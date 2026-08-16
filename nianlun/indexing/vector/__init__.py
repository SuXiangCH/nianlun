"""Dense-vector indexing for optional semantic document routing."""

from nianlun.indexing.vector.build import build_doc_vectors
from nianlun.indexing.vector.store import DocVectorStore

__all__ = ["DocVectorStore", "build_doc_vectors"]
