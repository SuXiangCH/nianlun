from __future__ import annotations

import logging
from io import StringIO

from nianlun.agent.cli import configure_tool_log_output


def test_configure_tool_log_output_writes_to_stdout(capsys):
    logger = logging.getLogger("nianlun.agent.tools")
    handlers = list(logger.handlers)
    level = logger.level
    propagate = logger.propagate
    try:
        logger.handlers.clear()
        configure_tool_log_output()
        configure_tool_log_output()

        logger.info("tool trace")
        captured = capsys.readouterr()
        assert captured.out == "tool trace\n"
        assert captured.err == ""
        assert (
            sum(
                getattr(handler, "_nianlun_cli_tool_handler", False)
                for handler in logger.handlers
            )
            == 1
        )
    finally:
        for handler in logger.handlers:
            if getattr(handler, "_nianlun_cli_tool_handler", False):
                handler.close()
        logger.handlers.clear()
        logger.handlers.extend(handlers)
        logger.setLevel(level)
        logger.propagate = propagate


def test_configure_tool_log_output_does_not_skip_existing_handler(capsys):
    logger = logging.getLogger("nianlun.agent.tools")
    handlers = list(logger.handlers)
    level = logger.level
    propagate = logger.propagate
    existing = logging.StreamHandler(StringIO())
    try:
        logger.handlers.clear()
        logger.addHandler(existing)
        configure_tool_log_output()

        logger.info("tool trace")
        captured = capsys.readouterr()
        assert captured.out == "tool trace\n"
        assert (
            sum(
                getattr(handler, "_nianlun_cli_tool_handler", False)
                for handler in logger.handlers
            )
            == 1
        )
    finally:
        for handler in logger.handlers:
            if getattr(handler, "_nianlun_cli_tool_handler", False):
                handler.close()
        logger.handlers.clear()
        logger.handlers.extend(handlers)
        logger.setLevel(level)
        logger.propagate = propagate
