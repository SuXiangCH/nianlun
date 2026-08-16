"""llm 层单测：mock LLM 验证 summarize_node / describe_document / content_to_text。

不发任何真实模型请求（用 FakeLLM 桩）。

用法::

    PYTHONPATH=<root> /opt/miniconda3/bin/python3 <root>/tests/indexing/tree/test_llm_unit.py
"""

from __future__ import annotations

import asyncio

from nianlun.indexing.tree.llm import (
    _doc_description_prompt,
    _node_summary_prompt,
    content_to_text,
    describe_document,
    summarize_node,
)


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeLLM:
    def __init__(self, response="SUMM", raise_on=False):
        self.response = response
        self.raise_on = raise_on
        self.calls = []

    async def ainvoke(self, prompt, **kw):
        self.calls.append(("ainvoke", prompt, kw))
        if self.raise_on:
            raise RuntimeError("boom")
        return FakeMessage(self.response)

    def invoke(self, prompt, **kw):
        self.calls.append(("invoke", prompt, kw))
        if self.raise_on:
            raise RuntimeError("boom")
        return FakeMessage(self.response)


def test_summarize_node_basic():
    llm = FakeLLM(response="这是摘要")
    out = asyncio.run(summarize_node(llm, "正文内容"))
    assert out == "这是摘要", out
    kind, prompt, kw = llm.calls[0]
    assert kind == "ainvoke"
    assert "正文内容" in prompt
    assert kw.get("temperature") == 0


def test_summarize_node_prompt_matches_old():
    p = _node_summary_prompt("XYZ")
    assert p.startswith("You are given a part of a document")
    assert "Partial Document Text: XYZ" in p
    assert "Directly return the description" in p


def test_summarize_node_empty_text_skips_call():
    llm = FakeLLM()
    assert asyncio.run(summarize_node(llm, "")) == ""
    assert llm.calls == [], llm.calls  # 空 text 不调用 LLM


def test_summarize_node_failure_returns_empty():
    llm = FakeLLM(raise_on=True)
    assert asyncio.run(summarize_node(llm, "正文")) == ""


def test_describe_document_basic():
    llm = FakeLLM(response="一句话描述")
    struct = {"doc_name": "x", "structure": [{"title": "A"}]}
    out = asyncio.run(describe_document(llm, struct))
    assert out == "一句话描述", out
    kind, prompt, kw = llm.calls[0]
    assert kind == "ainvoke"
    assert "Document Structure:" in prompt
    assert "structure" in prompt  # structure 被 f-string 插入
    assert kw.get("temperature") == 0


def test_describe_document_prompt_matches_old():
    p = _doc_description_prompt({"a": 1})
    assert p.startswith("Your are an expert")  # 刻意保留拼写 "Your are"
    assert "Document Structure: {'a': 1}" in p


def test_describe_document_failure_returns_empty():
    llm = FakeLLM(raise_on=True)
    assert asyncio.run(describe_document(llm, {})) == ""


def test_content_to_text_variants():
    assert content_to_text(None) == ""
    assert content_to_text("plain") == "plain"
    assert content_to_text(FakeMessage("msg")) == "msg"

    class M:
        pass

    # content 为 list（content blocks，dict 形态）：块间用 \n 连接（保留块边界）
    m = M()
    m.content = [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]
    assert content_to_text(m) == "hello\nworld"

    # content_blocks 优先于 content（部分后端把输出放这里）
    m2 = M()
    m2.content = None
    m2.content_blocks = [{"text": "cb"}]
    assert content_to_text(m2) == "cb"

    # content 为 list 含 str 元素：\n 连接
    m3 = M()
    m3.content = ["a", "b"]
    assert content_to_text(m3) == "a\nb"


def test_node_summary_prompt_exact_bytes():
    """逐字节锁定 prompt（含空行缩进空格），防编辑器剥离尾随空格。"""
    expected = (
        "You are given a part of a document, your task is to generate a description of the "
        "partial document about what are main points covered in the partial document.\n"
        "\n"
        "    Partial Document Text: XYZ\n"
        "    \n"
        "    Directly return the description, do not include any other text.\n"
        "    "
    )
    assert _node_summary_prompt("XYZ") == expected


def test_doc_description_prompt_exact_bytes():
    """逐字节锁定 prompt（含既有拼写 "Your are" 与空行缩进空格）。"""
    expected = (
        "Your are an expert in generating descriptions for a document.\n"
        "    You are given a structure of a document. Your task is to generate a one-sentence "
        "description for the document, which makes it easy to distinguish the document from "
        "other documents.\n"
        "        \n"
        "    Document Structure: {'a': 1}\n"
        "    \n"
        "    Directly return the description, do not include any other text.\n"
        "    "
    )
    assert _doc_description_prompt({"a": 1}) == expected


def test_content_to_text_empty_content_message_returns_empty():
    """思考模型 content 被吃空的故障形态：返回 ""，绝不返回对象 repr 垃圾串。"""

    class _EmptyMsg:
        content = None
        content_blocks = None

    assert content_to_text(_EmptyMsg()) == ""


TESTS = [
    test_summarize_node_basic,
    test_summarize_node_prompt_matches_old,
    test_summarize_node_empty_text_skips_call,
    test_summarize_node_failure_returns_empty,
    test_describe_document_basic,
    test_describe_document_prompt_matches_old,
    test_describe_document_failure_returns_empty,
    test_content_to_text_variants,
    test_node_summary_prompt_exact_bytes,
    test_doc_description_prompt_exact_bytes,
    test_content_to_text_empty_content_message_returns_empty,
]


def main() -> int:
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{'PASS' if failed == 0 else 'FAIL'}: {len(TESTS) - failed}/{len(TESTS)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
