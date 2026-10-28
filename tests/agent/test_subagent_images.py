from __future__ import annotations

import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nianlun.agent.lead_agent.runner import AgentStatusSink, RetrievalCollector
from nianlun.agent.subagents.config import DeepSearchConfig
from nianlun.agent.subagents.executor import DeepSearchRunner
from nianlun.agent.subagents.result import bound_result, result_from_agent_output
from nianlun.agent.subagents.result import failed_result
from nianlun.agent.subagents.tools import build_deep_search_tools
from nianlun.agent.tools import get_line_content_tool
from nianlun.agent.tools.answer_images import select_answer_images_tool


class ImageKnowledgeBase:
    def __init__(self):
        self.generation = 3
        self.content_version = 8
        self.image_id = "img_" + "a" * 24
        self.reads = []

    def get_line_content(self, doc_id, line_spec, char_offset=0, char_limit=None):
        self.reads.append((doc_id, line_spec, char_offset, char_limit))
        text = "Before ![Original](secret/private.png) after"
        first, last = 7, 38
        end = min(len(text), char_offset + (char_limit or len(text)))
        image = {
            "image_id": self.image_id,
            "generation": self.generation,
            "knowledge_base_id": "kb",
            "content_version": self.content_version,
            "asset_relpath": "secret/archive.png",
            "sha256": "a" * 64,
            "size_bytes": 100,
            "media_type": "image/png",
            "width": 8,
            "height": 6,
            "alt_text": "Original",
            "source_start": first,
            "source_end": last,
        }
        return json.dumps(
            {
                "doc_id": doc_id,
                "doc_name": "Document",
                "line_spec": line_spec,
                "content": [
                    {
                        "node_id": "n1",
                        "line_num": 1,
                        "text": text[char_offset:end],
                        "char_offset": char_offset,
                        "char_limit": char_limit,
                        "total_chars": len(text),
                        "text_truncated": end < len(text),
                        "next_char_offset": end if end < len(text) else None,
                        "images": [image]
                        if char_offset <= first and last <= end
                        else [],
                        "image_spans": [
                            {
                                "source_start": first,
                                "source_end": last,
                                "kind": "markdown",
                            }
                        ],
                    }
                ],
            }
        )


class ReadingAgent:
    def __init__(self, *, start=0, limit=4000, mutate=None):
        self.contexts = []
        self.start, self.limit, self.mutate = start, limit, mutate

    def invoke(self, payload, **kwargs):
        context = kwargs["context"]
        self.contexts.append(context)
        result = json.loads(
            get_line_content_tool.func(
                runtime=SimpleNamespace(context=context, tool_call_id="read"),
                doc_id="child-doc",
                line_spec="1",
                char_offset=self.start,
                char_limit=self.limit,
            )
        )
        item = result["content"][0]
        evidence = {**item, "doc_id": "child-doc", "line_spec": "1"}
        output = {"answer": "Conclusion", "evidence": [evidence]}
        if self.mutate:
            self.mutate(context, output)
        return output


def runner_for(agent, kb, **kwargs):
    return DeepSearchRunner(
        lambda: agent,
        context_factory=lambda: {"knowledge_base": kb, "tool_logging": False},
        tool_schema_version=5,
        image_capability=True,
        **kwargs,
    )


def test_each_child_owns_state_even_when_parent_context_is_supplied():
    kb, agent = ImageKnowledgeBase(), ReadingAgent()
    parent = RetrievalCollector(tool_schema_version=5, image_capability=True)
    parent.add_line_content_result(kb.get_line_content("parent-doc", "1"))
    parent.image_context.freeze_tool_batch(
        [{"name": "select_answer_images", "id": "parent"}]
    )
    parent.image_context.select_answer_images("parent", ["img_01"])
    sink = AgentStatusSink()
    dedup = {"documents": {"old"}, "nodes": {"old"}}
    runner = runner_for(agent, kb)
    for _ in range(2):
        execution = runner.run(
            "task",
            context={
                "retrieval_collector": parent,
                "retrieval_deduplication_state": dedup,
                "status_sink": sink,
                "input_messages": [{"content": "private parent history"}],
                "trusted_history": "private parent image history",
                "knowledge_base": object(),
                "image_capability": False,
            },
        )
        assert execution.result.status == "completed"
    one, two = agent.contexts
    assert one["retrieval_collector"] is not parent
    assert two["retrieval_collector"] is not one["retrieval_collector"]
    assert one["retrieval_deduplication_state"] == {"documents": set(), "nodes": set()}
    assert one["status_sink"] is not sink
    assert "input_messages" not in one
    assert "trusted_history" not in one
    assert len(parent.snippets) == 1
    assert parent.image_context.selected_images[0]["doc_id"] == "parent-doc"
    assert sink.events == []


def test_child_tools_project_v5_but_selection_is_denied_even_if_called_directly():
    names = [
        tool.name
        for tool in build_deep_search_tools(
            tool_schema_version=5, image_capability=True
        )
    ]
    assert "select_answer_images" not in names

    def mutate(context, output):
        collector = context["retrieval_collector"]
        collector.image_context.freeze_tool_batch(
            [{"name": "select_answer_images", "id": "select"}]
        )
        with pytest.raises(ValueError, match="child|子"):
            select_answer_images_tool.func(
                mode="images",
                image_refs=["img_01"],
                runtime=SimpleNamespace(context=context, tool_call_id="select"),
            )

    execution = runner_for(ReadingAgent(mutate=mutate), ImageKnowledgeBase()).run(
        "task"
    )
    assert execution.result.status == "completed"
    evidence = execution.to_dict()["evidence"][0]
    assert evidence["text"] == "Before ![Original](nianlun-image:img_01) after"
    assert evidence["image_refs"][0]["source_start"] == 7
    assert "secret/" not in json.dumps(execution.to_dict())


def test_parent_rereads_and_remaps_child_refs_without_overwriting_its_choice():
    kb = ImageKnowledgeBase()
    execution = runner_for(ReadingAgent(), kb).run("task")
    parent = RetrievalCollector(tool_schema_version=5, image_capability=True)
    parent.add_line_content_result(kb.get_line_content("parent-doc", "1"))
    parent.image_context.freeze_tool_batch(
        [{"name": "select_answer_images", "id": "select"}]
    )
    parent.image_context.select_answer_images("select", ["img_01"])
    mapped = execution.reread_image_refs(knowledge_base=kb, collector=parent)
    assert mapped == {"img_01": "img_02"}
    assert parent.image_context.candidates["img_02"]["doc_id"] == "child-doc"
    binding = parent.image_context.candidates["img_02"]["source_bindings"][0]
    assert binding["citation_id"] == 2
    assert binding["char_offset"] == 0
    assert binding["source_start"] == 7
    assert parent.image_context.selected_images[0]["ref_id"] == "img_01"
    assert kb.reads[-1] == ("child-doc", "1", 0, 44)


@pytest.mark.parametrize(
    "field,value",
    [("generation", 4), ("content_version", 9), ("image_id", "img_" + "b" * 24)],
)
def test_stale_full_identity_never_registers_parent_candidates(field, value):
    kb = ImageKnowledgeBase()
    execution = runner_for(ReadingAgent(), kb).run("task")
    setattr(kb, field, value)
    parent = RetrievalCollector(tool_schema_version=5, image_capability=True)
    assert execution.reread_image_refs(knowledge_base=kb, collector=parent) == {}
    assert parent.image_context.candidates == {}
    assert parent.snippets == []


def test_model_metadata_cannot_authorize_unread_or_fabricated_images():
    def fabricate(context, output):
        output["evidence"][0]["image_refs"] = [{"ref_id": "img_99", "generation": 999}]
        output["evidence"].append(
            {
                "doc_id": "unread",
                "node_id": "n1",
                "char_offset": 0,
                "text": "![Fake](nianlun-image:img_99)",
                "image_refs": [{"ref_id": "img_99"}],
            }
        )

    execution = runner_for(ReadingAgent(mutate=fabricate), ImageKnowledgeBase()).run(
        "task"
    )
    evidence = execution.to_dict()["evidence"]
    assert evidence[0]["image_refs"][0]["generation"] == 3
    assert evidence[0]["image_refs"][0]["ref_id"] == "img_01"
    assert evidence[1].get("image_refs", []) == []
    untrusted = result_from_agent_output(execution.to_dict())
    assert all(not item.image_refs for item in untrusted.evidence)


def test_clipped_or_partial_evidence_drops_image_authority_before_handoff():
    kb = ImageKnowledgeBase()
    partial = runner_for(ReadingAgent(start=22, limit=6), kb).run("task")
    assert partial.result.evidence[0].image_refs == ()
    assert "secret" not in partial.to_dict()["evidence"][0]["text"]
    config = replace(DeepSearchConfig(), max_evidence_text_chars=20)
    clipped = runner_for(ReadingAgent(), kb, config=config).run("task")
    assert clipped.result.evidence[0].image_refs == ()
    parent = RetrievalCollector(tool_schema_version=5, image_capability=True)
    assert clipped.reread_image_refs(knowledge_base=kb, collector=parent) == {}


def test_post_enrichment_total_limit_and_ref_count_are_materialized():
    def repeat(context, output):
        output["evidence"] *= 20

    config = replace(DeepSearchConfig(), max_result_chars=1800, max_answer_chars=100)
    execution = runner_for(
        ReadingAgent(mutate=repeat), ImageKnowledgeBase(), config=config
    ).run("task")
    assert len(execution.result.to_json(config)) <= 1800
    assert sum(len(item.image_refs) for item in execution.result.evidence) <= 8
    assert bound_result(execution.result, config) == execution.result


def test_concurrent_children_keep_colliding_local_refs_and_status_separate():
    barrier = threading.Barrier(2)
    contexts = []

    class ConcurrentAgent:
        def invoke(self, payload, **kwargs):
            context = kwargs["context"]
            contexts.append(context)
            barrier.wait(timeout=2)
            doc = payload["messages"][1].content
            result = json.loads(
                get_line_content_tool.func(
                    runtime=SimpleNamespace(context=context, tool_call_id="read"),
                    doc_id=doc,
                    line_spec="1",
                )
            )["content"][0]
            context["retrieval_deduplication_state"]["documents"].add(doc)
            context["status_sink"].emit("read", doc)
            return {
                "answer": doc,
                "evidence": [{**result, "doc_id": doc, "line_spec": "1"}],
            }

    runner = runner_for(ConcurrentAgent(), ImageKnowledgeBase())
    executions = []
    threads = [
        threading.Thread(target=lambda doc=doc: executions.append(runner.run(doc)))
        for doc in ("doc-a", "doc-b")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
    assert len(executions) == 2
    assert {item.result.evidence[0].doc_id for item in executions} == {"doc-a", "doc-b"}
    assert all(
        item.result.evidence[0].image_refs[0].ref_id == "img_01" for item in executions
    )
    assert contexts[0]["retrieval_collector"] is not contexts[1]["retrieval_collector"]
    assert all(len(item["status_sink"].events) == 1 for item in contexts)
    assert all(
        len(item["retrieval_deduplication_state"]["documents"]) == 1
        for item in contexts
    )


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_or_cancel_never_exports_late_worker_refs_or_parent_state(cancel):
    entered, release, finished, cancellation = (threading.Event() for _ in range(4))
    parent = RetrievalCollector(tool_schema_version=5, image_capability=True)

    class LateAgent(ReadingAgent):
        def invoke(self, payload, **kwargs):
            entered.set()
            release.wait(timeout=2)
            try:
                return super().invoke(payload, **kwargs)
            finally:
                finished.set()

    agent = LateAgent()
    runner = runner_for(
        agent,
        ImageKnowledgeBase(),
        config=replace(DeepSearchConfig(), timeout_seconds=0.15),
    )
    results = []
    thread = threading.Thread(
        target=lambda: results.append(
            runner.run(
                "task",
                cancel_event=cancellation,
                context={"retrieval_collector": parent},
            )
        )
    )
    thread.start()
    assert entered.wait(timeout=1)
    if cancel:
        cancellation.set()
    thread.join(timeout=1)
    assert results[0].result.error_code == ("cancelled" if cancel else "timeout")
    assert cancellation.is_set() is cancel
    assert results[0].result.evidence == ()
    release.set()
    assert finished.wait(timeout=1)
    assert parent.image_context.candidates == {}
    assert parent.snippets == []
    assert results[0].result.evidence == ()


def test_v4_and_disabled_runs_never_export_model_constructed_refs():
    trusted = runner_for(ReadingAgent(), ImageKnowledgeBase()).run("task").result

    class FabricatingAgent:
        def invoke(self, payload, **kwargs):
            return trusted

    for schema in (4, 5):
        execution = DeepSearchRunner(
            lambda: FabricatingAgent(), tool_schema_version=schema
        ).run("task")
        assert execution.result.evidence[0].image_refs == ()


def test_per_evidence_and_global_ref_bounds_drop_whole_ref_metadata():
    def repeat(context, output):
        output["evidence"] *= 16

    config = replace(
        DeepSearchConfig(),
        max_result_chars=20000,
        max_image_refs=3,
        max_evidence_image_refs=1,
    )
    execution = runner_for(
        ReadingAgent(mutate=repeat), ImageKnowledgeBase(), config=config
    ).run("task")
    assert sum(len(item.image_refs) for item in execution.result.evidence) == 3
    assert execution.result.evidence[3].image_refs == ()


def test_parent_reread_with_no_images_or_wrong_doc_cannot_widen_candidates():
    kb = ImageKnowledgeBase()
    execution = runner_for(ReadingAgent(), kb).run("task")

    class WrongDocument(ImageKnowledgeBase):
        def get_line_content(self, *args, **kwargs):
            payload = json.loads(super().get_line_content(*args, **kwargs))
            payload["doc_id"] = "another-doc"
            return json.dumps(payload)

    parent = RetrievalCollector(tool_schema_version=5, image_capability=True)
    assert (
        execution.reread_image_refs(knowledge_base=WrongDocument(), collector=parent)
        == {}
    )
    assert parent.image_context.candidates == {}


def test_parent_identity_conflict_does_not_register_stale_binding():
    kb = ImageKnowledgeBase()
    execution = runner_for(ReadingAgent(), kb).run("task")
    parent = RetrievalCollector(tool_schema_version=5, image_capability=True)
    kb.content_version = 7
    parent.add_line_content_result(kb.get_line_content("child-doc", "1"))
    kb.content_version = 8
    before = parent.image_context.candidates
    assert execution.reread_image_refs(knowledge_base=kb, collector=parent) == {}
    assert parent.image_context.candidates == before
    assert len(parent.snippets) == 1


def test_total_result_clipping_removes_image_authority_and_parent_reads():
    config = replace(DeepSearchConfig(), max_result_chars=400, max_answer_chars=100)
    kb = ImageKnowledgeBase()
    execution = runner_for(ReadingAgent(), kb, config=config).run("task")
    assert execution.result.evidence == ()
    assert len(execution.result.to_json(config)) <= 400
    reads_before = len(kb.reads)
    parent = RetrievalCollector(tool_schema_version=5, image_capability=True)
    assert execution.reread_image_refs(knowledge_base=kb, collector=parent) == {}
    assert len(kb.reads) == reads_before


def test_error_handoff_also_obeys_the_configured_total_limit():
    config = replace(DeepSearchConfig(), max_result_chars=100, max_answer_chars=50)
    result = failed_result("model_error", "sensitive failure detail " * 100)
    assert len(result.to_json(config)) <= 100
    assert result.to_dict(config)["error"]["code"] == "model_error"


def test_unrepresentable_total_limit_is_rejected_instead_of_overflowing():
    with pytest.raises(ValueError, match="max_result_chars"):
        DeepSearchConfig(max_result_chars=10, max_answer_chars=1)


@pytest.mark.parametrize(
    "gap,expected_ranges",
    [
        (650, [(7, 38, 7, 40)]),
        (10, [(7, 38, 7, 40), (48, 79, 50, 83)]),
    ],
)
def test_repeated_image_occurrences_keep_their_own_projected_coordinates(
    gap, expected_ranges
):
    class RepeatedImageKnowledgeBase(ImageKnowledgeBase):
        def get_line_content(self, doc_id, line_spec, char_offset=0, char_limit=None):
            payload = json.loads(
                super().get_line_content(doc_id, line_spec, char_offset, char_limit)
            )
            syntax = "![Original](secret/private.png)"
            text = "Before " + syntax + "x" * gap + syntax + " after"
            item = payload["content"][0]
            first_image = item["images"][0]
            second_image = {
                **first_image,
                "source_start": 38 + gap,
                "source_end": 69 + gap,
            }
            end = min(len(text), char_offset + (char_limit or len(text)))
            item.update(
                text=text[char_offset:end],
                total_chars=len(text),
                text_truncated=end < len(text),
                next_char_offset=end if end < len(text) else None,
                images=[first_image, second_image],
                image_spans=[
                    {
                        "source_start": image["source_start"],
                        "source_end": image["source_end"],
                        "kind": "markdown",
                    }
                    for image in (first_image, second_image)
                ],
            )
            return json.dumps(payload)

    kb = RepeatedImageKnowledgeBase()
    execution = runner_for(ReadingAgent(), kb).run("task")
    evidence = execution.result.evidence[0]
    assert execution.result.status == "completed"
    assert evidence.text[7:40] == "![Original](nianlun-image:img_01)"
    assert [
        (ref.source_start, ref.source_end, ref.text_start, ref.text_end)
        for ref in evidence.image_refs
    ] == expected_ranges
    for ref in evidence.image_refs:
        assert evidence.text[ref.text_start : ref.text_end] == (
            "![Original](nianlun-image:img_01)"
        )
    parent = RetrievalCollector(tool_schema_version=5, image_capability=True)
    assert execution.reread_image_refs(knowledge_base=kb, collector=parent) == {
        "img_01": "img_01"
    }
    binding = parent.image_context.candidates["img_01"]["source_bindings"][0]
    assert (binding["source_start"], binding["source_end"]) == (7, 38)
