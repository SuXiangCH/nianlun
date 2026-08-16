"""tree_index 算法管线：正文切片 / token 计数 / thinning / 建树 / 格式化。

纯算法、自包含，唯一外部依赖为本模块 ``.llm.count_tokens``。

无标题文档由 LLM 语义规划器生成 section tree，再适配为 structure 契约；
LLM 不可用时按 chunk 粒度降级为规则兜底（见 untitled.planner），不整篇失败。
有标题文档走正文算法路径；存在超大隐含 section 的伪有标题文档同样
重路由到语义规划器（``max_titled_node_tokens``）。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
from typing import Any, cast

from nianlun.indexing.tree.llm import (
    build_chat_model,
    count_tokens,
    describe_document,
    summarize_node,
)
from nianlun.indexing.tree.parser import extract_headings
from nianlun.indexing.tree.untitled import make_source, plan_untitled
from nianlun.indexing.tree.untitled.models import SectionPlan

logger = logging.getLogger(__name__)

_UNTITLED_NODE_TITLE = "无标题"
# Keep untitled chunks within the vector embedding input budget as well as the
# workspace tree contract, so FTS and dense search index the same node text.
_UNTITLED_NODE_CHAR_LIMIT = 4_000
_SUMMARY_CONCURRENCY = 8

# ============ 正文切片 ============


def slice_node_text(headings: list[dict], lines: list[str]) -> list[dict]:
    """按 ``line_num`` 切原始源码行，附 ``text``。

    ``level`` 已在 parser 算好，无需二次正则。切片规则 ``[本标题行, 下一标题行)``
    （文档顺序，不看层级）。

    无标题文档按源码行切成有限大小的顶层兜底节点，避免整篇长文只产生一个超大
    节点，导致向量/全文索引截断正文。每个兜底节点仍从真实源码行开始定位。
    """
    if not headings:
        return _slice_untitled_text(lines)

    all_nodes = [
        {"title": h["title"], "line_num": h["line_num"], "level": h["level"]}
        for h in headings
    ]
    for i, node in enumerate(all_nodes):
        start_line = node["line_num"] - 1
        if i + 1 < len(all_nodes):
            end_line = all_nodes[i + 1]["line_num"] - 1
        else:
            end_line = len(lines)
        node["text"] = "\n".join(lines[start_line:end_line]).strip()
    return all_nodes


def _slice_untitled_text(lines: list[str]) -> list[dict]:
    """切分无标题 Markdown，保留全文并让每个节点适合下游索引。"""
    if not any(line.strip() for line in lines):
        return []

    chunks: list[dict] = []
    chunk_text = ""
    chunk_start = 1

    def append_chunk(start_line: int, text: str) -> None:
        if not text:
            return
        chunks.append(
            {
                "title": _UNTITLED_NODE_TITLE,
                "line_num": start_line,
                "level": 1,
                "text": text,
            }
        )

    for line_num, line in enumerate(lines, start=1):
        # Keep each source newline in the stream. A final empty split item does not
        # add a character, while a trailing newline was already attached to the
        # preceding line.
        remaining = line + ("\n" if line_num < len(lines) else "")
        if not remaining:
            continue

        # Prefer source-line boundaries when the line itself fits the limit.
        if (
            chunk_text
            and len(remaining) <= _UNTITLED_NODE_CHAR_LIMIT
            and len(chunk_text) + len(remaining) > _UNTITLED_NODE_CHAR_LIMIT
        ):
            append_chunk(chunk_start, chunk_text)
            chunk_text = ""

        while remaining:
            if not chunk_text:
                chunk_start = line_num
            available = _UNTITLED_NODE_CHAR_LIMIT - len(chunk_text)
            if available == 0:
                append_chunk(chunk_start, chunk_text)
                chunk_text = ""
                continue
            take = min(available, len(remaining))
            chunk_text += remaining[:take]
            remaining = remaining[take:]
            if remaining:
                append_chunk(chunk_start, chunk_text)
                chunk_text = ""

    append_chunk(chunk_start, chunk_text)

    # Match the existing section slicing behavior: trim only document boundaries,
    # while preserving whitespace between chunks and source lines.
    if chunks:
        chunks[0]["text"] = chunks[0]["text"].lstrip()
        chunks[-1]["text"] = chunks[-1]["text"].rstrip()
    return [chunk for chunk in chunks if chunk["text"]]


# ============ token 计数 ============


def _find_all_children(
    parent_index: int, parent_level: int, node_list: list[dict]
) -> list[int]:
    """找 parent 之后所有后代（直到遇到同/更高级别）的索引。"""
    children_indices = []
    for i in range(parent_index + 1, len(node_list)):
        current_level = node_list[i]["level"]
        if current_level <= parent_level:
            break
        children_indices.append(i)
    return children_indices


def compute_token_counts(nodes: list[dict], model: str | None = None) -> list[dict]:
    """自身+后代文本 token 求和，逆序遍历。

    原地写入 ``nodes[i]['text_token_count']``（自身文本 + 全部后代文本的 token 数）。
    """
    result_list = nodes
    for i in range(len(result_list) - 1, -1, -1):
        current_node = result_list[i]
        current_level = current_node["level"]
        children_indices = _find_all_children(i, current_level, result_list)
        total_text = current_node.get("text", "")
        for child_index in children_indices:
            child_text = result_list[child_index].get("text", "")
            if child_text:
                total_text += "\n" + child_text
        result_list[i]["text_token_count"] = count_tokens(total_text, model=model)
    return result_list


# ============ 小节点合并 / thinning ============


def thin_tree(
    node_list: list[dict], min_node_token, model: str | None = None
) -> list[dict]:
    """总 token < 阈值则把子节点文本并入父节点、删子节点。"""
    result_list = node_list.copy()
    nodes_to_remove: set[int] = set()

    for i in range(len(result_list) - 1, -1, -1):
        if i in nodes_to_remove:
            continue
        current_node = result_list[i]
        current_level = current_node["level"]
        total_tokens = current_node.get("text_token_count", 0)

        if total_tokens < min_node_token:
            children_indices = _find_all_children(i, current_level, result_list)
            children_texts = []
            for child_index in sorted(children_indices):
                if child_index not in nodes_to_remove:
                    child_text = result_list[child_index].get("text", "")
                    if child_text.strip():
                        children_texts.append(child_text)
                    nodes_to_remove.add(child_index)

            if children_texts:
                merged_text = current_node.get("text", "")
                for child_text in children_texts:
                    if merged_text and not merged_text.endswith("\n"):
                        merged_text += "\n\n"
                    merged_text += child_text
                result_list[i]["text"] = merged_text
                result_list[i]["text_token_count"] = count_tokens(
                    merged_text, model=model
                )

    for index in sorted(nodes_to_remove, reverse=True):
        result_list.pop(index)
    return result_list


# ============ 建树 ============


def build_tree(node_list: list[dict]) -> list[dict]:
    """按 ``level`` 栈式嵌套，分配 ``node_id``（``zfill(4)``）。"""
    if not node_list:
        return []

    stack: list[tuple[dict, int]] = []
    root_nodes = []
    node_counter = 1

    for node in node_list:
        current_level = node["level"]
        tree_node = {
            "title": node["title"],
            "node_id": str(node_counter).zfill(4),
            "text": node["text"],
            "line_num": node["line_num"],
            "nodes": [],
        }
        node_counter += 1

        while stack and stack[-1][1] >= current_level:
            stack.pop()

        if not stack:
            root_nodes.append(tree_node)
        else:
            parent_node, _parent_level = stack[-1]
            parent_node["nodes"].append(tree_node)

        stack.append((tree_node, current_level))

    return root_nodes


# ============ node_id 重排 ============


def write_node_id(data, node_id: int = 0) -> int:
    """DFS 重排 ``node_id``（``zfill(4)``）。"""
    if isinstance(data, dict):
        data["node_id"] = str(node_id).zfill(4)
        node_id += 1
        for key in list(data.keys()):
            if "nodes" in key:
                node_id = write_node_id(data[key], node_id)
    elif isinstance(data, list):
        for index in range(len(data)):
            node_id = write_node_id(data[index], node_id)
    return node_id


# ============ 展平 / 清洗 / 格式化 ============


def structure_to_list(structure) -> list:
    """树展平为节点列表（DFS 前序）。"""
    if isinstance(structure, dict):
        nodes = [structure]
        if "nodes" in structure:
            nodes.extend(structure_to_list(structure["nodes"]))
        return nodes
    elif isinstance(structure, list):
        nodes = []
        for item in structure:
            nodes.extend(structure_to_list(item))
        return nodes
    return []


def reorder_dict(data: dict, key_order: list[str]) -> dict:
    """按 key_order 重排键（仅保留存在的键）。"""
    if not key_order:
        return data
    return {key: data[key] for key in key_order if key in data}


def format_structure(structure, order: list[str] | None = None):
    """按 ``order`` 重排键、删空 ``nodes``。"""
    if not order:
        return structure
    if isinstance(structure, dict):
        if "nodes" in structure:
            structure["nodes"] = format_structure(structure["nodes"], order)
        if not structure.get("nodes"):
            structure.pop("nodes", None)
        structure = reorder_dict(structure, order)
    elif isinstance(structure, list):
        structure = [format_structure(item, order) for item in structure]
    return structure


def create_clean_structure_for_description(structure):
    """仅保留 title/node_id/summary/prefix_summary。"""
    if isinstance(structure, dict):
        clean_node = {}
        for key in ["title", "node_id", "summary", "prefix_summary"]:
            if key in structure:
                clean_node[key] = structure[key]
        if structure.get("nodes"):
            clean_node["nodes"] = create_clean_structure_for_description(
                structure["nodes"]
            )
        return clean_node
    elif isinstance(structure, list):
        return [create_clean_structure_for_description(item) for item in structure]
    return structure


def clean_tree_for_output(tree_nodes: list[dict]) -> list[dict]:
    """输出清洗（title/node_id/text/line_num + nodes）。"""
    cleaned_nodes = []
    for node in tree_nodes:
        cleaned_node = {
            "title": node["title"],
            "node_id": node["node_id"],
            "text": node["text"],
            "line_num": node["line_num"],
        }
        if node["nodes"]:
            cleaned_node["nodes"] = clean_tree_for_output(node["nodes"])
        cleaned_nodes.append(cleaned_node)
    return cleaned_nodes


# ============ 编排入口 ============

_WITH_TEXT_ORDER = [
    "title",
    "node_id",
    "line_num",
    "summary",
    "prefix_summary",
    "text",
    "nodes",
]
_NO_TEXT_ORDER = [
    "title",
    "node_id",
    "line_num",
    "summary",
    "prefix_summary",
    "nodes",
]


def _ensure_llm(llm, model: str | None):
    """llm 缺省时按 model 构建；仅摘要/描述需要时才调用（无摘要路径零 LLM）。"""
    if llm is not None:
        return llm
    return build_chat_model(model=model)


def _has_oversized_section(
    headings: list[dict],
    lines: list[str],
    model: str | None,
    max_titled_node_tokens: int | None,
) -> bool:
    """伪有标题检测：任一隐含 section 超过 token 阈值即需要语义规划。

    切片规则与 ``slice_node_text`` 一致（``[本标题行, 下一标题行)``，外加文首
    preamble）。只要存在一个超大节点（会被下游向量/全文索引截断），就把整篇
    重路由到无标题语义规划器，而不是产出确定性但截断的结构。
    ``max_titled_node_tokens=None`` 关闭检测，恢复纯二元行为。
    """
    if max_titled_node_tokens is None:
        return False
    boundaries = [h["line_num"] - 1 for h in headings] + [len(lines)]
    starts = [0] + boundaries[:-1]
    for start, end in zip(starts, boundaries):
        if (
            count_tokens("\n".join(lines[start:end]), model=model)
            > max_titled_node_tokens
        ):
            return True
    return False


def _untitled_tree_to_legacy_structure(
    sections: tuple[SectionPlan, ...], raw_markdown: str
) -> list[dict]:
    """将 semantic section tree 适配为 workspace structure 契约。

    下游读取 ``title/node_id/text/line_num/nodes`` 字段；正文由本地 source span
    读取，绝不使用 LLM 正文。

    父节点 ``text`` 只含 ``own_content_spans``（不属于任何后代的自有正文），
    与有标题路径 ``[本标题, 下一标题)`` 的语义对齐——避免 FTS/Vector 把父节点
    完整 span（含全部后代正文）重复索引。无自有正文的父节点 text 为空，
    下游 ``text.strip()`` 判空后自然成为纯导航节点。
    """
    by_id = {section.section_id: section for section in sections}

    def make_node(section: SectionPlan) -> dict:
        children = sorted(
            (by_id[child_id] for child_id in section.child_ids),
            key=lambda child: child.start_char,
        )
        if children:
            text = "\n\n".join(
                raw_markdown[start:end] for start, end in section.own_content_spans
            )
        else:
            text = raw_markdown[section.start_char : section.end_char]
        return {
            "title": section.title,
            "node_id": section.section_id,
            "text": text,
            "line_num": section.start_line,
            "nodes": [make_node(child) for child in children],
        }

    roots = sorted(
        (section for section in sections if section.parent_id is None),
        key=lambda section: section.start_char,
    )
    tree = [make_node(section) for section in roots]
    write_node_id(tree)
    return tree


async def _generate_summaries(structure, llm, model, summary_token_threshold: int):
    """树展平后逐节点摘要。

    token < 阈值直接用原文；否则调 ``summarize_node``。
    叶子写 ``summary``、非叶写 ``prefix_summary``。
    """
    nodes = structure_to_list(structure)
    semaphore = asyncio.Semaphore(_SUMMARY_CONCURRENCY)

    async def _one(node):
        text = node.get("text", "")
        if count_tokens(text, model=model) < summary_token_threshold:
            return text
        if llm is None:
            # 无标题降级路径：LLM 不可用时对齐 summarize_node 的失败回落。
            return ""
        async with semaphore:
            return await summarize_node(llm, text)

    summaries = await asyncio.gather(*[_one(n) for n in nodes])
    for node, summary in zip(nodes, summaries):
        if not node.get("nodes"):
            node["summary"] = summary
        else:
            node["prefix_summary"] = summary
    return structure


async def build_md_index(
    md_path: str,
    *,
    model: str | None = None,
    llm=None,
    add_node_summary: bool = False,
    summary_token_threshold: int = 200,
    add_doc_description: bool = False,
    add_node_text: bool = False,
    add_node_id: bool = True,
    thin: bool = False,
    min_node_token=None,
    atx_only: bool = True,
    max_titled_node_tokens: int | None = 8_000,
) -> dict:
    """读 md -> 解析标题 -> 切正文 -> (可选 thin) -> 建树 -> (可选 摘要/描述) -> 格式化。

    返回 ``{doc_name, doc_description, line_count, structure}``：
    - ``doc_description`` 始终返回：仅在 ``add_node_summary`` 且 ``add_doc_description`` 时生成，
      否则 ``None``（下游以 ``.get()`` 读取，缺省不受影响）。
    - 无摘要/描述时不构建 LLM（纯结构索引零 LLM 调用）。
    - ``max_titled_node_tokens``：伪有标题检测阈值（默认 8000 token）。有标题文档
      中任一隐含 section 超阈值即整篇重路由到无标题语义规划器；``None`` 关闭。
    """
    with open(md_path, "r", encoding="utf-8", newline="") as f:
        md_text = f.read()
    line_count = md_text.count("\n") + 1
    lines = md_text.split("\n")
    tree: list[dict] = []
    nodes: list[dict] | None = None

    # Full CommonMark detection decides whether this is truly an untitled
    # document. The ``atx_only`` option still governs extraction for titled docs.
    # 伪有标题文档（存在超大隐含 section）同样重路由到语义规划器。
    all_headings = extract_headings(md_text, atx_only=False)
    if not all_headings or _has_oversized_section(
        all_headings, lines, model, max_titled_node_tokens
    ):
        try:
            llm = _ensure_llm(llm, model)
        except Exception as exc:
            # LLM 未配置/构造失败不应让整篇文档索引失败：降级为纯规则兜底。
            logger.warning(
                "untitled planner LLM unavailable for %s, rule-only fallback: %s",
                md_path,
                exc,
            )
            llm = None
        source = make_source(str(md_path), md_text, md_path)
        untitled_tree = await plan_untitled(source, llm=cast(Any, llm))
        if untitled_tree.diagnostics:
            logger.warning(
                "untitled tree used local recovery for %s: %s",
                md_path,
                "; ".join(untitled_tree.diagnostics),
            )
        tree = _untitled_tree_to_legacy_structure(untitled_tree.sections, md_text)
        line_count = source.document.line_count
        # Untitled semantic nodes must not be physically thinned. Existing
        # summary/description stages can still operate on the adapted structure.
        nodes = None
    else:
        headings = extract_headings(md_text, atx_only=atx_only)
        nodes = slice_node_text(headings, lines)

    if nodes is not None and thin:
        compute_token_counts(nodes, model=model)
        nodes = thin_tree(nodes, min_node_token, model=model)

    if nodes is not None:
        tree = build_tree(nodes)
    if nodes is not None and add_node_id:
        write_node_id(tree)

    doc_name = os.path.splitext(os.path.basename(str(md_path)))[0]
    doc_description = None

    if add_node_summary:
        try:
            llm = _ensure_llm(llm, model)
        except Exception as exc:
            if nodes is not None:
                # 有标题路径：摘要需要 LLM，构造失败即报错。
                raise
            # 无标题降级路径：规划已规则兜底，摘要同步降级而非整篇失败。
            logger.warning(
                "node summaries degraded for %s: LLM unavailable: %s", md_path, exc
            )
            llm = None
        # 摘要阶段始终带 text（summarize 需要正文参与），之后按 add_node_text 剥离。
        tree = format_structure(tree, order=_WITH_TEXT_ORDER)
        tree = await _generate_summaries(tree, llm, model, summary_token_threshold)
        if not add_node_text:
            tree = format_structure(tree, order=_NO_TEXT_ORDER)
        if add_doc_description:
            clean = create_clean_structure_for_description(tree)
            doc_description = (
                await describe_document(llm, clean) if llm is not None else ""
            )
    else:
        tree = format_structure(
            tree, order=_WITH_TEXT_ORDER if add_node_text else _NO_TEXT_ORDER
        )

    return {
        "doc_name": doc_name,
        "doc_description": doc_description,
        "line_count": line_count,
        "structure": tree,
    }


def build_md_index_sync(md_path: str, **kwargs) -> dict:
    """``build_md_index`` 的同步包装。

    检测到当前线程已有 running loop 时提交 ``ThreadPoolExecutor`` 执行 ``asyncio.run``
    （避免在已有事件循环的线程里嵌套 ``asyncio.run`` 报错），否则直接 ``asyncio.run``。
    """
    coro = build_md_index(md_path, **kwargs)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()
