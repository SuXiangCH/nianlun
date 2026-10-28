"""Resolve complete published contracts to fixed prompt implementations.

The v4 hash is the SHA256 of the original v1 source bytes, as persisted by
the release service. Neither contract depends on the active prompt modules.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING
from pydantic import BaseModel, ConfigDict, ValidationError

from nianlun.agent.prompts.v4 import lead as v4_lead
from nianlun.agent.prompts.v4 import retrieval_answer as v4_retrieval
from nianlun.agent.prompts.v5 import lead as v5_lead
from nianlun.agent.prompts.v5 import retrieval_answer as v5_retrieval

if TYPE_CHECKING:
    from nianlun.agent.contracts import KnowledgeBasePort, RetrievedSnippet

PromptContractKey = tuple[int, int, str, int]
Messages = list[dict[str, str]]
AnswerMessages = tuple[Messages, list[int], list[int]]


class UnsupportedPromptBundle(ValueError):
    """The requested complete contract or capability is not supported."""


class PromptContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    bundle_id: str
    tool_schema_version: int
    prompt_version: int
    v1_prompt_hash: str
    checkpoint_schema_version: int


@dataclass(frozen=True, slots=True)
class PromptBundle:
    """Immutable release identity and its fixed prompt builders.

    Builders consume model-safe projections only; callers retain private image
    identities outside these messages. The checkpoint version is an explicit
    compatibility declaration, not permission to decode other state schemas.
    """

    bundle_id: str
    tool_schema_version: int
    prompt_version: int
    v1_prompt_hash: str
    checkpoint_schema_version: int
    lead_prompt_builder: Callable[..., str]
    retrieval_prompt_builder: Callable[..., str]
    answer_prompt_builder: Callable[..., str]
    retrieval_messages_builder: Callable[..., Messages]
    answer_messages_builder: Callable[..., AnswerMessages]

    @property
    def contract_key(self) -> PromptContractKey:
        return (
            self.tool_schema_version,
            self.prompt_version,
            self.v1_prompt_hash,
            self.checkpoint_schema_version,
        )

    @property
    def supports_images(self) -> bool:
        return self.tool_schema_version == 5

    def contract_json(self) -> str:
        return PromptContract(
            **{field: getattr(self, field) for field in PromptContract.model_fields}
        ).model_dump_json()

    def _check_image_capability(self, image_capability: bool) -> None:
        if image_capability and not self.supports_images:
            raise UnsupportedPromptBundle(
                "The v4 prompt bundle does not support images."
            )

    def build_system_prompt(
        self, kb: KnowledgeBasePort, *, image_capability: bool = False
    ) -> str:
        self._check_image_capability(image_capability)
        if self.supports_images:
            return self.lead_prompt_builder(kb, image_capability=image_capability)
        return self.lead_prompt_builder(kb)

    def build_retrieval_system_prompt(
        self, kb: KnowledgeBasePort, *, image_capability: bool = False
    ) -> str:
        self._check_image_capability(image_capability)
        if self.supports_images:
            return self.retrieval_prompt_builder(kb, image_capability=image_capability)
        return self.retrieval_prompt_builder(kb)

    def build_answer_system_prompt(self, *, image_capability: bool = False) -> str:
        self._check_image_capability(image_capability)
        if self.supports_images:
            return self.answer_prompt_builder(image_capability=image_capability)
        return self.answer_prompt_builder()

    def build_retrieval_messages(
        self,
        question: str,
        *,
        context_window_tokens: int | None = None,
        conversation_context: list[dict[str, str]] | None = None,
    ) -> Messages:
        return self.retrieval_messages_builder(
            question,
            context_window_tokens=context_window_tokens,
            conversation_context=conversation_context,
        )

    def build_answer_messages(
        self,
        question: str,
        snippets: list[RetrievedSnippet],
        *,
        context_window_tokens: int | None = None,
        conversation_context: list[dict[str, str]] | None = None,
        image_capability: bool = False,
    ) -> AnswerMessages:
        self._check_image_capability(image_capability)
        if self.supports_images:
            return self.answer_messages_builder(
                question,
                snippets,
                context_window_tokens=context_window_tokens,
                conversation_context=conversation_context,
                image_capability=image_capability,
            )
        return self.answer_messages_builder(
            question,
            snippets,
            context_window_tokens=context_window_tokens,
            conversation_context=conversation_context,
        )


LEGACY_PROMPT_BUNDLE = PromptBundle(
    bundle_id="nianlun-v4-p21-c1",
    tool_schema_version=4,
    prompt_version=21,
    v1_prompt_hash="568b8426c403f98de5b4448c769289d0ad711e71fabd130c611f97446acae93a",
    checkpoint_schema_version=1,
    lead_prompt_builder=v4_lead.build_system_prompt,
    retrieval_prompt_builder=v4_retrieval.build_retrieval_system_prompt,
    answer_prompt_builder=v4_retrieval.build_answer_system_prompt,
    retrieval_messages_builder=v4_retrieval.build_retrieval_messages,
    answer_messages_builder=v4_retrieval.build_answer_messages,
)

CURRENT_PROMPT_BUNDLE = PromptBundle(
    bundle_id="nianlun-v5-p22-c1",
    tool_schema_version=5,
    prompt_version=22,
    v1_prompt_hash="a4bb69ca9cb3cf4a1446269ad4c1ca68d7c4fb914aa3a1706caf44382328eb76",
    checkpoint_schema_version=1,
    lead_prompt_builder=v5_lead.build_system_prompt,
    retrieval_prompt_builder=v5_retrieval.build_retrieval_system_prompt,
    answer_prompt_builder=v5_retrieval.build_answer_system_prompt,
    retrieval_messages_builder=v5_retrieval.build_retrieval_messages,
    answer_messages_builder=v5_retrieval.build_answer_messages,
)

_BUNDLES = MappingProxyType(
    {
        bundle.contract_key: bundle
        for bundle in (LEGACY_PROMPT_BUNDLE, CURRENT_PROMPT_BUNDLE)
    }
)


def resolve_prompt_bundle(
    *,
    tool_schema_version: int,
    prompt_version: int,
    v1_prompt_hash: str,
    checkpoint_schema_version: int,
) -> PromptBundle:
    """Resolve only a known complete tuple; never accept arbitrary old hashes."""
    key = (
        tool_schema_version,
        prompt_version,
        v1_prompt_hash,
        checkpoint_schema_version,
    )
    if (
        type(tool_schema_version) is not int
        or type(prompt_version) is not int
        or type(v1_prompt_hash) is not str
        or type(checkpoint_schema_version) is not int
        or key not in _BUNDLES
    ):
        raise UnsupportedPromptBundle("Unsupported agent prompt contract.")
    return _BUNDLES[key]


def resolve_prompt_contract_json(value: str) -> PromptBundle:
    try:
        contract = PromptContract.model_validate_json(value)
    except (ValidationError, TypeError) as exc:
        raise UnsupportedPromptBundle("Unsupported agent prompt contract.") from exc
    bundle = resolve_prompt_bundle(**contract.model_dump(exclude={"bundle_id"}))
    if contract.bundle_id != bundle.bundle_id:
        raise UnsupportedPromptBundle("Unsupported agent prompt contract.")
    return bundle


__all__ = [
    "CURRENT_PROMPT_BUNDLE",
    "LEGACY_PROMPT_BUNDLE",
    "PromptBundle",
    "PromptContractKey",
    "UnsupportedPromptBundle",
    "resolve_prompt_bundle",
    "resolve_prompt_contract_json",
]
