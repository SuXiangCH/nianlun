"""Global test isolation for integrations with external services."""

from __future__ import annotations

import os


# Importing ``app.api_server.main`` creates a default application. Keep its
# startup recovery from contacting Milvus during ordinary unit tests.
os.environ.setdefault("NIANLUN_API_FTS_ENABLED", "false")
