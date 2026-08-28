from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from nianlun.agent.lead_agent.agent import build_agent
from nianlun.knowledgebase import KnowledgeBase, KnowledgeBaseConfig
from nianlun.agent.lead_agent.runtime import AgentRuntime
from nianlun.agent.lead_agent.runner import RetrievalCollector
from nianlun.agent.lead_agent.factory import AgentRuntimeFactory
from nianlun.agent.tools import (
    build_tools,
    find_semantic_documents_tool,
    get_line_content_tool,
    get_structure_outline_tool,
    search_document_nodes_tool,
)


def _tool_by_name(tools, name: str):
    return next(tool for tool in tools if tool.name == name)


def _runtime_context(kb):
    return SimpleNamespace(
        context={
            "knowledge_base": kb,
            "retrieval_collector": RetrievalCollector(),
            "tool_logging": False,
        }
    )


def test_search_tool_schema_uses_fts_query():
    search = _tool_by_name(build_tools(), "search_document_nodes")

    assert list(search.tool_call_schema.model_fields) == ["query", "doc_ids"]
    assert "runtime" not in search.tool_call_schema.model_fields


def test_tool_names_match_prompt_protocol():
    assert [tool.name for tool in build_tools()] == [
        "search_document_nodes",
        "get_document",
        "get_structure_outline",
        "get_line_content",
    ]


def test_vector_tool_is_opt_in():
    assert "find_semantic_documents" not in [tool.name for tool in build_tools()]
    vector_tools = build_tools(include_vector=True)
    vector_tool = _tool_by_name(vector_tools, "find_semantic_documents")
    assert list(vector_tool.tool_call_schema.model_fields) == ["query", "top_k"]


def test_vector_tool_uses_runtime_context():
    class FakeKnowledgeBase:
        def find_semantic_documents(self, query, top_k):
            assert query == "经营风险"
            assert top_k == 3
            return [{"doc_id": "doc-1", "score": 0.9, "node_hints": []}]

    result = find_semantic_documents_tool.func(
        _runtime_context(FakeKnowledgeBase()),
        "经营风险",
        3,
    )
    assert json.loads(result)["documents"] == [
        {"doc_id": "doc-1", "score": 0.9, "node_hints": []}
    ]


def test_vector_tool_returns_empty_match_lists_for_blank_query():
    class FakeKnowledgeBase:
        def find_semantic_documents(self, query, top_k):
            assert query == ""
            assert top_k == 3
            return []

    result = find_semantic_documents_tool.func(
        _runtime_context(FakeKnowledgeBase()),
        "",
        3,
    )

    assert json.loads(result) == {
        "query": "",
        "documents": [],
    }


def test_defaults_prefer_milvus_query_mode():
    default_search = _tool_by_name(build_tools(), "search_document_nodes")
    assert list(default_search.tool_call_schema.model_fields) == ["query", "doc_ids"]
    config = KnowledgeBaseConfig()
    assert config.fts_enabled is True


def test_tools_use_runtime_context_without_closure_binding():
    class FakeKnowledgeBase:
        def search_document_nodes(self, query, doc_ids=None):
            assert doc_ids is None
            return f"query:{query}"

    runtime = _runtime_context(FakeKnowledgeBase())
    assert (
        search_document_nodes_tool.func(runtime, "营业收入变化") == "query:营业收入变化"
    )


def test_get_line_content_tool_applies_default_and_maximum_limits():
    calls = []

    class FakeKnowledgeBase:
        def get_line_content(self, **kwargs):
            calls.append(kwargs)
            return "{}"

    runtime = _runtime_context(FakeKnowledgeBase())

    get_line_content_tool.func(runtime, "doc-1", "1")
    assert calls[-1]["char_limit"] == 4000

    get_line_content_tool.func(runtime, "doc-1", "1", char_offset=120, char_limit=1500)
    assert calls[-1]["char_offset"] == 120
    assert calls[-1]["char_limit"] == 1500

    get_line_content_tool.func(runtime, "doc-1", "1", char_limit=None)
    assert calls[-1]["char_limit"] == 4000

    get_line_content_tool.func(runtime, "doc-1", "1", char_limit=20_000)
    assert calls[-1]["char_limit"] == 8000


def test_get_line_content_allows_direct_read_and_outline_remains_optional():
    content_calls = []

    class FakeKnowledgeBase:
        def get_structure_outline(self, doc_id):
            return f"[0001] 第 1 行: {doc_id} 目录"

        def get_line_content(self, **kwargs):
            content_calls.append(kwargs)
            return "{}"

    runtime = _runtime_context(FakeKnowledgeBase())

    assert json.loads(get_line_content_tool.func(runtime, "doc-1", "1")) == {}
    assert len(content_calls) == 1
    assert "doc-1 目录" in get_structure_outline_tool.func(runtime, "doc-1")
    assert json.loads(get_line_content_tool.func(runtime, "doc-1", "1")) == {}
    assert len(content_calls) == 2


def test_tool_exceptions_are_left_for_error_middleware():
    class FailingKnowledgeBase:
        def get_line_content(self, **_kwargs):
            raise TimeoutError("backend timeout")

    runtime = _runtime_context(FailingKnowledgeBase())

    with pytest.raises(TimeoutError, match="backend timeout"):
        get_line_content_tool.func(runtime, "doc-1", "1")


def test_runtime_context_keeps_knowledge_bases_isolated():
    class FakeKnowledgeBase:
        def __init__(self, value):
            self.value = value

        def search_document_nodes(self, query, doc_ids=None):
            return f"{self.value}:{query}"

    first = _runtime_context(FakeKnowledgeBase("research"))
    second = _runtime_context(FakeKnowledgeBase("manual"))

    assert search_document_nodes_tool.func(first, "风险") == "research:风险"
    assert search_document_nodes_tool.func(second, "风险") == "manual:风险"


def test_knowledge_base_fts_keeps_pageindex_result_shape():
    class FakeSearcher:
        def search(self, query, limit):
            assert query == "营业收入"
            assert limit == 512
            return [
                {
                    "doc_id": "doc-1",
                    "doc_name": "报告",
                    "source_type": "node_text",
                    "node_id": "0001",
                    "title": "经营情况",
                    "line_num": 12,
                    "score": 3.2,
                },
                {
                    "doc_id": "doc-1",
                    "doc_name": "报告",
                    "source_type": "node_summary",
                    "node_id": "0001",
                    "title": "经营情况",
                    "line_num": 12,
                    "score": 2.1,
                },
                {
                    "doc_id": "doc-2",
                    "doc_name": "描述命中文档",
                    "source_type": "doc_desc",
                    "node_id": None,
                    "title": None,
                    "line_num": None,
                    "score": 1.5,
                },
            ]

    kb = KnowledgeBase("data/workspaces/default", full_text_retriever=FakeSearcher())
    result = json.loads(kb.search_document_nodes("营业收入"))

    assert result["query"] == "营业收入"
    assert result["documents"] == [
        {
            "doc_id": "doc-1",
            "doc_name": "报告",
            "node_hints": [{"node_id": "0001", "title": "经营情况", "line_num": 12}],
        },
        {
            "doc_id": "doc-2",
            "doc_name": "描述命中文档",
            "node_hints": [],
        },
    ]
    assert result["truncated"] is False


def test_fts_limits_are_twenty_summaries_and_sixty_nodes():
    from nianlun.indexing.fts.config import DOC_TOP_N, NODE_MATCH_LIMIT, NODE_PER_DOC
    from nianlun.knowledgebase.config import NODE_MATCH_LIMIT as KB_NODE_MATCH_LIMIT

    assert DOC_TOP_N == 20
    assert NODE_PER_DOC == 3
    assert NODE_MATCH_LIMIT == 60
    assert KB_NODE_MATCH_LIMIT == 60


def test_fts_merges_summary_and_node_recall_channels():
    selected_hits = []
    for index in range(25):
        doc_id = f"selected-{index}"
        selected_hits.extend(
            [
                {
                    "doc_id": doc_id,
                    "doc_name": doc_id,
                    "source_type": "doc_desc",
                    "node_id": None,
                    "title": None,
                    "line_num": None,
                    "score": 200.0 - index,
                },
                {
                    "doc_id": doc_id,
                    "doc_name": doc_id,
                    "source_type": "node_text",
                    "node_id": "own-node",
                    "title": "正文",
                    "line_num": 1,
                    "score": 1.0,
                },
            ]
        )
    outside_hits = [
        {
            "doc_id": f"outside-{doc_index}",
            "doc_name": f"outside-{doc_index}",
            "source_type": "node_text",
            "node_id": f"node-{node_index}",
            "title": "外部节点",
            "line_num": node_index + 1,
            "score": 100.0 - doc_index - node_index / 10,
        }
        for doc_index in range(40)
        for node_index in range(3)
    ]

    class FakeSearcher:
        def search(self, query, limit):
            assert query == "覆盖测试"
            assert limit == 512
            return [*selected_hits, *outside_hits]

    kb = KnowledgeBase("data/workspaces/default", full_text_retriever=FakeSearcher())
    result = json.loads(kb.search_document_nodes("覆盖测试"))

    assert len(result["documents"]) == 40
    assert [document["doc_id"] for document in result["documents"][:20]] == [
        f"selected-{index}" for index in range(20)
    ]
    assert all(document["node_hints"] == [] for document in result["documents"][:20])
    assert [document["doc_id"] for document in result["documents"][20:]] == [
        f"outside-{index}" for index in range(20)
    ]
    assert all(
        document["node_hints"]
        == [
            {
                "node_id": f"node-{node_index}",
                "title": "外部节点",
                "line_num": node_index + 1,
            }
            for node_index in range(3)
        ]
        for document in result["documents"][20:]
    )
    assert result["truncated"] is True


def test_agent_runtime_exposes_application_context():
    kb = object()
    runtime = AgentRuntime(
        agent=object(),
        model="test",
        effective_url="test",
        tool_logging=False,
        kb=kb,
    )

    collector, context = runtime.new_request_context()
    assert context == {
        "knowledge_base": kb,
        "retrieval_collector": collector,
        "tool_logging": False,
        "clarification_enabled": False,
        "retrieval_deduplication_state": {"documents": set(), "nodes": set()},
    }
    second_collector, second_context = runtime.new_request_context()
    assert second_collector is not collector
    assert second_context["retrieval_collector"] is second_collector
    assert (
        second_context["retrieval_deduplication_state"]
        is not context["retrieval_deduplication_state"]
    )


def test_agent_runtime_does_not_retain_factory_secrets_or_worker_builder():
    runtime = AgentRuntime(
        agent=object(),
        model="test",
        effective_url="test",
        tool_logging=False,
        kb=object(),
    )

    assert not hasattr(runtime, "api_key")
    assert not hasattr(runtime, "kb_config")
    assert not hasattr(runtime, "allow_env_fallback")
    assert not hasattr(runtime, "spawn_worker")


def test_runtime_factory_rejects_ambiguous_knowledge_base_sources():
    with pytest.raises(ValueError, match="不能同时传入"):
        AgentRuntimeFactory(
            knowledge_base=object(),
            knowledge_base_config=KnowledgeBaseConfig(),
        )


def test_retrieval_collector_copies_doc_name_into_snippets():
    collector = RetrievalCollector()
    result = json.dumps(
        {
            "doc_id": "doc-1",
            "doc_name": "营收季报.md",
            "line_spec": "5-7",
            "content": [
                {
                    "node_id": "n1",
                    "title": "营收分析",
                    "line_num": 5,
                    "text": "正文片段",
                },
            ],
        },
        ensure_ascii=False,
    )

    annotated = json.loads(collector.add_line_content_result(result))
    assert annotated["content"][0]["citation_id"] == 1
    assert collector.snippets[0]["citation_id"] == 1
    assert collector.snippets[0]["doc_name"] == "营收季报.md"
    assert collector.snippets[0]["doc_id"] == "doc-1"
    assert collector.snippets[0]["title"] == "营收分析"

    repeated = json.loads(collector.add_line_content_result(result))
    assert repeated["content"][0]["citation_id"] == 1
    assert len(collector.snippets) == 1


def test_get_line_content_tool_returns_collector_assigned_citation_ids():
    class FakeKnowledgeBase:
        def get_line_content(self, **_kwargs):
            return json.dumps(
                {
                    "doc_id": "doc-1",
                    "doc_name": "规格书.md",
                    "line_spec": "12,24",
                    "content": [
                        {
                            "node_id": "n1",
                            "title": "电气性能",
                            "line_num": 12,
                            "text": "漏电流最大值 4.0 mA",
                        },
                        {
                            "node_id": "n2",
                            "title": "尺寸",
                            "line_num": 24,
                            "text": "直径 10 mm",
                        },
                    ],
                },
                ensure_ascii=False,
            )

    runtime = _runtime_context(FakeKnowledgeBase())
    payload = json.loads(get_line_content_tool.func(runtime, "doc-1", "12,24"))

    assert [item["citation_id"] for item in payload["content"]] == [1, 2]
    assert [
        item["citation_id"] for item in runtime.context["retrieval_collector"].snippets
    ] == [1, 2]


def test_batch_worker_runtime_uses_explicit_factory():
    import nianlun.agent.batch as batch_module

    batch_module._BATCH_RUNTIME_LOCAL.__dict__.clear()
    created = []
    worker = SimpleNamespace(tool_logging=False)

    def runtime_factory():
        created.append(True)
        return worker

    first = batch_module.get_batch_worker_runtime(
        tool_logging=False,
        runtime_factory=runtime_factory,
    )
    second = batch_module.get_batch_worker_runtime(
        tool_logging=False,
        runtime_factory=runtime_factory,
    )

    assert first is worker
    assert second is worker
    assert created == [True]
    batch_module._BATCH_RUNTIME_LOCAL.__dict__.clear()


def test_langgraph_injects_context_into_tool_runtime():
    class FakeKnowledgeBase:
        def search_document_nodes(self, query, doc_ids=None):
            return f"from-bound-kb:{query}"

        def get_document(self, doc_id):
            return "{}"

        def get_structure_outline(self, doc_id):
            return ""

        def get_line_content(self, doc_id, line_spec):
            return "{}"

    class FakeToolCallingModel(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    collector = RetrievalCollector()
    model = FakeToolCallingModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "search_document_nodes",
                            "args": {"query": "风险"},
                            "id": "call-1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="done"),
            ]
        )
    )
    agent = build_agent(
        model,
        tools=build_tools(),
        system_prompt="test",
        context_schema=dict,
    )

    result = agent.invoke(
        {"messages": [{"role": "user", "content": "x"}]},
        context={
            "knowledge_base": FakeKnowledgeBase(),
            "retrieval_collector": collector,
            "tool_logging": False,
        },
    )

    assert result["messages"][-1].content == "done"
    assert len(collector.tool_calls) == 1
    recorded = collector.tool_calls[0]
    assert recorded["name"] == "search_document_nodes"
    assert recorded["args"] == {"query": "风险", "doc_ids": None}
    assert isinstance(recorded["elapsed_ms"], int)
    assert recorded["elapsed_ms"] >= 0
