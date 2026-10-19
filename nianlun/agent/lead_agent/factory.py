"""Agent graph 与应用依赖的 composition root。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from nianlun.agent.contracts import AgentRequestContext, AgentVersion, KnowledgeBasePort
from nianlun.agent.lead_agent.agent import build_agent, estimate_agent_context_overhead
from nianlun.agent.lead_agent.prompt import build_system_prompt
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
from nianlun.agent.retrieval_answer.prompt import build_retrieval_system_prompt
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
    enable_thinking: bool | None = None
    checkpointer: Any = None

    def __post_init__(self) -> None:
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

        llm = build_chat_model(
            model=model,
            temperature=(
                self.temperature
                if self.temperature is not None
                else get_openai_temperature()
                if self.allow_env_fallback
                else 0.0
            ),
            enable_thinking=(
                get_enable_thinking()
                if self.allow_env_fallback and self.enable_thinking is None
                else self.enable_thinking
            ),
            api_key=api_key,
            base_url=base_url,
            allow_env_fallback=self.allow_env_fallback,
        )
        tools = build_tools(
            include_vector=knowledge_base.has_vector,
            include_clarification=True,
        )
        effective_system_prompt = (
            build_system_prompt(knowledge_base)
            if self.agent_version == "v0"
            else build_retrieval_system_prompt(knowledge_base)
        )
        loop_guard_config = self.loop_guard_config or AgentLoopGuardConfig()
        middleware = [
            RequestCancellationMiddleware(),
            ContextSummarizationMiddleware(
                llm,
                context_overhead_tokens=estimate_agent_context_overhead(
                    effective_system_prompt, tools
                ),
                model_context_limit=self.context_window_tokens,
            ),
            DanglingToolCallMiddleware(),
            ToolErrorHandlingMiddleware(),
            RetrievalLoopGuardMiddleware(loop_guard_config),
            RetrievalDeduplicationMiddleware(),
            ClarificationMiddleware(),
        ]
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
            )
        return AgentRuntime(
            agent=graph,
            model=model,
            effective_url=base_url or "https://api.openai.com/v1",
            tool_logging=self.tool_logging,
            kb=knowledge_base,
            recursion_limit=loop_guard_config.recursion_limit,
            agent_version=self.agent_version,
        )


__all__ = ["AgentRuntimeFactory"]
