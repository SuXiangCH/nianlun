"""FTS 命中后处理：节点去重 + 每文档限额，保持扁平返回（不嵌套）。

解决节点去重和每文档限额问题，输出内部节点候选，最终由 KnowledgeBase 组装为
``documents[].node_hints``：

- **节点重复**：同一节点因 ``node_text`` + ``node_summary`` 两条记录分别命中（词汇重叠），
  按 ``(doc_id, node_id)`` 合并取最高分，来源收集进 ``matched_sources``。
- **文档霸榜**：单文档占满 top-K，按 ``cap_per_doc`` 每文档限 N 个，释放名额给其他文档。

输入是 ``NodeFtsStore.search`` 返回的**节点级**命中（``node_id`` 非空；``doc_desc`` 由调用方
分离处理文档级候选，输出扁平 list，供 ``KnowledgeBase`` 组装文档结果。
"""

from __future__ import annotations

from typing import Any


def dedup_node_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 ``(doc_id, node_id)`` 去重节点级命中。

    同一节点的 ``node_text`` 与 ``node_summary`` 记录可能分别命中 -> 合并为一条：

    - ``score`` 取最高（最相关）；
    - 新增 ``matched_sources`` 收集全部命中来源（``source_type`` 列表），``source_type``
      保留最高分那条的；
    - ``title``/``line_num``/``doc_name`` 同节点相同，取最高分那条。

    Args:
        hits: ``NodeFtsStore.search`` 返回的命中。``node_id`` 为 ``None`` 的 ``doc_desc``
            记录会被跳过（调用方应已分离，此处防御性过滤）。

    Returns:
        去重后的命中（扁平 list），按 ``score`` 降序。每条多一个 ``matched_sources`` 字段。
    """
    by_node: dict[tuple[str, str], dict[str, Any]] = {}
    for h in hits:
        nid = h.get("node_id")
        if not nid:
            continue  # doc_desc 记录不参与节点去重
        key = (h["doc_id"], nid)
        cur = by_node.get(key)
        if cur is None:
            by_node[key] = {**h, "matched_sources": [h["source_type"]]}
        else:
            if h["source_type"] not in cur["matched_sources"]:
                cur["matched_sources"].append(h["source_type"])
            if h["score"] > cur["score"]:
                cur["score"] = h["score"]
                cur["source_type"] = h["source_type"]
                cur["title"] = h["title"]
                cur["line_num"] = h["line_num"]
                cur["doc_name"] = h["doc_name"]
    result = list(by_node.values())
    result.sort(key=lambda h: h["score"], reverse=True)
    return result


def cap_per_doc(hits: list[dict[str, Any]], per_doc_cap: int) -> list[dict[str, Any]]:
    """每文档最多保留 ``per_doc_cap`` 个节点命中（按 score 降序），防单文档霸榜。

    输入应已去重（否则同节点的两条会各占一个名额）。保留全局 score 降序，仅对超配额文档截断。

    Args:
        hits: 节点级命中（建议已去重）。
        per_doc_cap: 每文档保留的节点上限。

    Returns:
        限额后的命中（扁平 list），保持 score 降序。
    """
    counts: dict[str, int] = {}
    result: list[dict[str, Any]] = []
    for h in sorted(hits, key=lambda x: x["score"], reverse=True):
        doc_id = h["doc_id"]
        if counts.get(doc_id, 0) >= per_doc_cap:
            continue
        counts[doc_id] = counts.get(doc_id, 0) + 1
        result.append(h)
    return result


def postprocess_node_hits(
    hits: list[dict[str, Any]],
    *,
    per_doc_cap: int | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """节点级命中后处理：去重 ->（可选）每文档限额 ->（可选）截断。保持扁平返回。

    Args:
        hits: ``NodeFtsStore.search`` 返回的节点级命中（``node_id`` 非空）。
        per_doc_cap: 每文档节点上限；``None`` 不限额（仅去重）。
        limit: 返回上限；``None`` 不截断。

    Returns:
        扁平 list（不嵌套），按 score 降序。每条含 ``matched_sources``。
    """
    out = dedup_node_hits(hits)
    if per_doc_cap is not None:
        out = cap_per_doc(out, per_doc_cap)
    if limit is not None:
        out = out[:limit]
    return out


def top_doc_ids(
    hits: list[dict[str, Any]],
    doc_top_n: int | None = None,
) -> list[str]:
    """BM25 命中文档按最高分取 top-N（文档级结果上限）。

    文档分 = 该文档所有命中记录（``doc_desc`` + ``node_text`` + ``node_summary``）的最高
    BM25 分。按文档分降序返回前 ``doc_top_n`` 个 ``doc_id``；``None`` 不封顶（全部 distinct
    文档，按分降序）。

    与 ``cap_per_doc``（节点级深度）正交：本函数控文档广度，``cap_per_doc`` 控节点深度，
    合起来即"文档 top-N × 每文档 k 节点"两阶段。

    Args:
        hits: ``NodeFtsStore.search`` 返回的全部命中（含 ``doc_desc`` 等所有 source_type）。
        doc_top_n: 文档上限；``None`` 不截断。

    Returns:
        ``doc_id`` 列表（按文档分降序，最多 ``doc_top_n`` 个）。
    """
    doc_score: dict[str, float] = {}
    for h in hits:
        did = h.get("doc_id")
        if not did:
            continue
        sc = h.get("score", 0)
        if did not in doc_score or sc > doc_score[did]:
            doc_score[did] = sc
    ordered = sorted(doc_score, key=lambda d: doc_score[d], reverse=True)
    if doc_top_n is not None:
        ordered = ordered[:doc_top_n]
    return ordered
