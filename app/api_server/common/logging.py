"""Minimal standard-library logging setup for the API layer."""

from __future__ import annotations

import logging


class _ApiLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.name == "app.api_server" or record.name.startswith(
            "app.api_server."
        )


def configure_logging(level: str = "INFO") -> None:
    """Make API logs visible without replacing an application's log config.

    Uvicorn configures its own loggers but does not always install a root
    handler for application loggers. The handler is marked and installed only
    once, so importing or creating multiple test applications is harmless.
    """
    api_logger = logging.getLogger("app.api_server")
    api_logger.setLevel(level.upper())

    root_logger = logging.getLogger()
    if root_logger.handlers:
        return

    handler = logging.StreamHandler()
    handler.setLevel(level.upper())
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    handler.addFilter(_ApiLogFilter())
    setattr(handler, "_nianlun_api_handler", True)
    root_logger.addHandler(handler)


__all__ = ["configure_logging"]
