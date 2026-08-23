"""Shared Pydantic base for evaluation contracts."""

from pydantic import BaseModel, ConfigDict


class EvaluationSchema(BaseModel):
    """Strict base class for public evaluation contracts."""

    model_config = ConfigDict(extra="forbid")
