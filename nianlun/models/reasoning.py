"""Shared reasoning effort values and application override resolution."""

from typing import Literal, TypedDict

ReasoningEffort = Literal["low", "medium", "high"]
ReasoningEffortOverride = Literal["inherit", "default", "low", "medium", "high"]


class ReasoningEffortKwargs(TypedDict, total=False):
    reasoning_effort: ReasoningEffort


def resolve_reasoning_effort(
    override: ReasoningEffortOverride,
    model_default: ReasoningEffort | None,
) -> ReasoningEffort | None:
    """Resolve inheritance while keeping provider defaults distinct from disabling."""
    if model_default is not None and model_default not in ("low", "medium", "high"):
        raise ValueError("Invalid model reasoning_effort")
    if override == "inherit":
        return model_default
    if override == "default":
        return None
    if override not in ("low", "medium", "high"):
        raise ValueError("Invalid reasoning_effort override")
    return override
