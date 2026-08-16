"""SQLite lifecycle helpers for the API server."""

from app.api_server.database.connection import SQLiteConnectionFactory
from app.api_server.database.migrations import initialize_database
from app.api_server.database.models import (
    Application,
    Base,
    Conversation,
    Document,
    DocumentArtifact,
    DocumentParseTask,
    KnowledgeBase,
    Message,
    MessageSource,
    ModelProfile,
    LLMModelProfile,
    EmbeddingModelProfile,
    ParserModelProfile,
    UploadOperation,
)

__all__ = [
    "Application",
    "Base",
    "Conversation",
    "Document",
    "DocumentArtifact",
    "DocumentParseTask",
    "KnowledgeBase",
    "Message",
    "MessageSource",
    "ModelProfile",
    "LLMModelProfile",
    "EmbeddingModelProfile",
    "ParserModelProfile",
    "SQLiteConnectionFactory",
    "UploadOperation",
    "initialize_database",
]
