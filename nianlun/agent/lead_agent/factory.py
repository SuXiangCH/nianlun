"""Agent graph 与应用依赖的 composition root。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langchain.agents.middleware import AgentMiddleware

from nianlun.agent.contracts import AgentRequestContext, AgentVersion, KnowledgeBasePort
from nianlun.agent.lead_agent.agent import build_agent, estimate_agent_context_overhead
from nianlun.agent.image_context import AnswerImagePolicy
from nianlun.agent.prompt_registry import LEGACY_PROMPT_BUNDLE, PromptBundle
from nianlun.agent.lead_agent.runtime import AgentRuntime
from nianlun.agent.middleware import (
    ClarificationMiddleware,
    ContextSummarizationMiddleware,
    DanglingToolCallMiddleware,
    RequestCancellationMiddleware,
    RetrievalDeduplicationMiddleware,
    RetrievalLoopGuardMiddleware,
    ToolErrorHandlingMiddleware,
)
from nianlun.agent.middleware.retrieval_loop_guard_middleware import (
    AgentLoopGuardConfig,
)
from nianlun.agent.tools import build_tools
from nianlun.agent.retrieval_answer import build_retrieval_answer_graph
from nianlun.config import (
    get_enable_thinking,
    get_openai_api_key,
    get_openai_base_url,
    get_openai_model,
    get_openai_temperature,
)
from nianlun.knowledgebase import KnowledgeBaseConfig
from nianlun.knowledgebase.factory import KnowledgeBaseFactory
from nianlun.indexing.vector.config import get_vector_enabled
from nianlun.models.llm import build_chat_model
from nianlun.models.reasoning import ReasoningEffort, ReasoningEffortKwargs


@dataclass(frozen=True, slots=True)
class AgentRuntimeFactory:
    """保存构建输入，并按需创建相互隔离的 AgentRuntime。"""

    tool_logging: bool = True
    knowledge_base: KnowledgeBasePort | None = None
    knowledge_base_config: KnowledgeBaseConfig | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    context_window_tokens: int | None = None
    allow_env_fallback: bool = True
    loop_guard_config: AgentLoopGuardConfig | None = None
    agent_version: AgentVersion = "v0"
    temperature: float | None = None
    legacy_temperature: bool = False
    enable_thinking: bool | None = None
    reasoning_effort: ReasoningEffort | None = None
    checkpointer: Any = None
    prompt_bundle: PromptBundle = LEGACY_PROMPT_BUNDLE
    image_capability: bool = False
    image_policy: AnswerImagePolicy = AnswerImagePolicy()

    def __post_init__(self) -> None:
        if self.image_capability and not self.prompt_bundle.supports_images:
            raise ValueError("v4 不支持图片输出")
        if self.knowledge_base is not None and self.knowledge_base_config is not None:
            raise ValueError(
                "knowledge_base 与 knowledge_base_config 不能同时传入，请选择一种方式。"
            )
        if self.agent_version not in ("v0", "v1"):
            raise ValueError(f"不支持的 Agent 版本: {self.agent_version}")

    def create(self) -> AgentRuntime:
        api_key = self.api_key
        base_url = self.base_url
        model = self.model
        if self.allow_env_fallback:
            api_key = api_key or get_openai_api_key()
            base_url = base_url or get_openai_base_url()
            model = model or get_openai_model()
        if not api_key:
            raise RuntimeError("未设置 OPENAI_API_KEY")
        if not model:
            raise RuntimeError("未配置模型名称")

        knowledge_base = self.knowledge_base
        if knowledge_base is None:
            config = self.knowledge_base_config or KnowledgeBaseConfig(
                vector_enabled=get_vector_enabled()
            )
            knowledge_base = KnowledgeBaseFactory(config).create(
                api_key=api_key,
                base_url=base_url,
                allow_env_fallback=self.allow_env_fallback,
            )
        elif not knowledge_base.has_fts:
            raise RuntimeError("全文检索未配置。")

        reasoning_kwargs: ReasoningEffortKwargs = {}
        if self.reasoning_effort is not None:
            reasoning_kwargs["reasoning_effort"] = self.reasoning_effort
        llm = build_chat_model(
            model=model,
            temperature=(
                self.temperature
                if self.temperature is not None
                else get_openai_temperature()
                if self.allow_env_fallback
                else None
            ),
            legacy_temperature=self.legacy_temperature,
            enable_thinking=(
                get_enable_thinking()
                if self.allow_env_fallback and self.enable_thinking is None
                else self.enable_thinking
            ),
            api_key=api_key,
            base_url=base_url,
            allow_env_fallback=self.allow_env_fallback,
            **reasoning_kwargs,
        )
        tools = build_tools(
            include_vector=knowledge_base.has_vector,
            include_clarification=True,
            tool_schema_version=self.prompt_bundle.tool_schema_version,
            image_capability=self.image_capability,
        )
        effective_system_prompt = (
            self.prompt_bundle.build_system_prompt(
                knowledge_base, image_capability=self.image_capability
            )
            if self.agent_version == "v0"
            else self.prompt_bundle.build_retrieval_system_prompt(
                knowledge_base, image_capability=self.image_capability
            )
        )
        loop_guard_config = self.loop_guard_config or AgentLoopGuardConfig()
        context_overhead_tokens = estimate_agent_context_overhead(
            effective_system_prompt, tools
        )
        middleware: list[AgentMiddleware] = [
            RequestCancellationMiddleware(),
            ContextSummarizationMiddleware(
                llm,
                context_overhead_tokens=context_overhead_tokens,
                model_context_limit=self.context_window_tokens,
            ),
            DanglingToolCallMiddleware(),
            ToolErrorHandlingMiddleware(),
            RetrievalLoopGuardMiddleware(loop_guard_config),
            RetrievalDeduplicationMiddleware(),
            ClarificationMiddleware(),
        ]
        if self.image_capability:
            from nianlun.agent.middleware.answer_image_selection_middleware import (
                AnswerImageSelectionMiddleware,
            )

            middleware.insert(5, AnswerImageSelectionMiddleware())
        graph = build_agent(
            llm,
            tools=tools,
            system_prompt=effective_system_prompt,
            checkpointer=(
                self.checkpointer if self.checkpointer is not None else MemorySaver()
            )
            if self.agent_version == "v0"
            else None,
            context_schema=AgentRequestContext,
            name="pageindex-multi-doc-rag",
            middleware=middleware,
        )
        if self.agent_version == "v1":
            graph = build_retrieval_answer_graph(
                retrieval_agent=graph,
                answer_model=llm,
                recursion_limit=loop_guard_config.recursion_limit,
                context_window_tokens=self.context_window_tokens,
                checkpointer=self.checkpointer,
                prompt_bundle=self.prompt_bundle,
                image_capability=self.image_capability,
            )
        return AgentRuntime(
            agent=graph,
            model=model,
            effective_url=base_url or "https://api.openai.com/v1",
            tool_logging=self.tool_logging,
            kb=knowledge_base,
            recursion_limit=loop_guard_config.recursion_limit,
            agent_version=self.agent_version,
            tool_schema_version=self.prompt_bundle.tool_schema_version,
            image_capability=self.image_capability,
            image_policy=self.image_policy,
            context_window_tokens=self.context_window_tokens,
            context_overhead_tokens=context_overhead_tokens,
        )


__all__ = ["AgentRuntimeFactory"]
