"""MinerU task payload construction shared by submission and retry flows."""

from __future__ import annotations

from typing import Any


def build_parser_options(config: dict[str, Any], extension: str) -> dict[str, Any]:
    """Return the persisted MinerU options for one source document."""
    options: dict[str, Any] = {
        "language": config["language"],
        "is_ocr": config["is_ocr"],
        "enable_table": config["enable_table"],
        "enable_formula": config["enable_formula"],
    }
    if extension == ".pdf" and config["page_ranges"]:
        options["page_ranges"] = config["page_ranges"]
    return options
