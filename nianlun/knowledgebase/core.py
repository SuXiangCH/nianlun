"""多文档知识库的加载与检索操作。

KnowledgeBase 封装 _meta.json 注册表与单文档懒加载缓存，并提供 Nianlun 风格
的检索工具（list / across / info / outline / line / nodes）。状态收敛在实例上
而非模块级全局，方便指向不同的 workspace 目录、或在测试中替换为桩实现，
而无需改动上层代码。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nianlun.knowledgebase.config import (
    NODE_HINT_SUMMARY_LIMIT,
    NODE_MATCH_LIMIT,
    WORKSPACE_DIR,
)
from nianlun.knowledgebase.workspace_view import WorkspaceView
from nianlun.knowledgebase.retrieval_ports import DocumentSearchPort, NodeSearchPort
from nianlun.knowledgebase.image_syntax import parse_image_syntax


MAX_LINE_SPEC_LINES = 500


def sanitize_text(text: str) -> str:
    """移除 surrogate 字符，防止序列化时报错。"""
    if not text:
        return text
    return text.encode("utf-8", errors="replace").decode("utf-8")


def parse_line_spec(line_spec: str) -> list[int]:
    """解析行号范围字符串（"5-7" / "3,8,12" / "1-10,50-60"）。"""
    result: set[int] = set()
    for part in line_spec.replace(" ", "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = int(part.split("-", 1)[0]), int(part.split("-", 1)[1])
            if start > end:
                raise ValueError(f"无效范围 '{part}': 起始行必须 <= 结束行")
            if end - start + 1 > MAX_LINE_SPEC_LINES:
                raise ValueError(f"行号范围最多允许 {MAX_LINE_SPEC_LINES} 行")
            result.update(range(start, end + 1))
        else:
            result.add(int(part))
        if len(result) > MAX_LINE_SPEC_LINES:
            raise ValueError(f"行号范围最多允许 {MAX_LINE_SPEC_LINES} 行")
    return sorted(result)


class KnowledgeBase:
    """单份多文档知识库的只读视图与检索器。"""

    def __init__(
        self,
        workspace_dir: Path | str = WORKSPACE_DIR,
        *,
        full_text_retriever: NodeSearchPort | None = None,
        semantic_document_retriever: DocumentSearchPort | None = None,
        snapshot_relpath: str | None = None,
        snapshot_manifest_sha256: str | None = None,
        knowledge_base_id: str | None = None,
        content_version: int | None = None,
    ) -> None:
        self.workspace_dir = Path(workspace_dir)
        self.knowledge_base_id = knowledge_base_id
        self.content_version = content_version
        self.meta_path = self.workspace_dir / "_meta.json"
        if snapshot_relpath is None:
            self._workspace_view = WorkspaceView.legacy(self.workspace_dir)
        else:
            if (
                snapshot_manifest_sha256 is None
                or knowledge_base_id is None
                or content_version is None
            ):
                raise ValueError(
                    "V2 snapshot 读取缺少 manifest hash、知识库 ID 或 revision"
                )
            self._workspace_view = WorkspaceView.revision(
                self.workspace_dir,
                snapshot_relpath,
                snapshot_manifest_sha256,
                knowledge_base_id=knowledge_base_id,
                content_version=content_version,
            )
        self._meta: dict[str, Any] = self._workspace_view.meta
        self._doc_cache: dict[str, dict] = {}
        self._full_text_retriever = full_text_retriever
        self._semantic_document_retriever = semantic_document_retriever

    @property
    def has_fts(self) -> bool:
        """当前知识库是否绑定了全文节点检索器。"""
        return self._full_text_retriever is not None

    @property
    def has_vector(self) -> bool:
        """Whether semantic document routing is available."""
        return self._semantic_document_retriever is not None

    def close(self) -> None:
        """Release immutable snapshot readers owned by this runtime."""
        self._workspace_view.close()
        for retriever in (self._full_text_retriever, self._semantic_document_retriever):
            close = getattr(retriever, "close", None)
            if callable(close):
                close()

    # ============ 数据加载 ============

    def _load_meta(self) -> dict[str, Any]:
        """加载知识库注册表 _meta.json。"""
        return WorkspaceView.legacy(self.workspace_dir).meta

    @property
    def meta(self) -> dict[str, Any]:
        """文档注册表（{doc_id: {doc_name, doc_description, line_count, ...}}）。"""
        return self._meta

    def load_doc(self, doc_id: str) -> dict:
        """按需懒加载单文档索引 <doc_id>.json（带实例级缓存）。"""
        if doc_id not in self._meta:
            raise KeyError(doc_id)
        if doc_id in self._doc_cache:
            return self._doc_cache[doc_id]
        data = self._workspace_view.load_document(doc_id)
        doc = {
            "doc_id": doc_id,
            "doc_name": data.get("doc_name", "unknown"),
            "doc_description": data.get("doc_description", ""),
            "type": data.get("type", ""),
            "line_count": data.get("line_count", 0),
            "structure": data.get("structure", []),
        }
        self._doc_cache[doc_id] = doc
        return doc

    def _safe_load_doc(self, doc_id: str):
        """加载文档，失败返回 (None, error_json)。"""
        try:
            return self.load_doc(doc_id), None
        except (KeyError, FileNotFoundError):
            return None, self._doc_err(doc_id)

    @staticmethod
    def _doc_err(doc_id: str) -> str:
        """统一的无效 doc_id 错误返回。"""
        return json.dumps(
            {
                "error": f"未知文档 id '{doc_id}'，请先调用 list_documents() 获取合法 doc_id。"
            },
            ensure_ascii=False,
        )

    # ============ 检索工具 ============

    def list_documents(self, detailed: bool = True) -> str:
        """列出知识库中的所有文档（doc_id / 文档名 / 摘要 / 行数）。

        detailed=True：indent JSON，供 CLI `list` 命令等人类查看场景。
        detailed=False：一行一条紧凑格式，供 system prompt 注入——
        indent JSON 的缩进/字段名/括号约占 37% 字符，是纯 token 浪费；
        描述保留（选文档的语义依据），仅压缩内部空白为一行。
        """
        items = []
        for doc_id, info in self._meta.items():
            desc = sanitize_text(info.get("doc_description", ""))
            if len(desc) > 150:
                desc = desc[:150] + "..."
            items.append(
                {
                    "doc_id": doc_id,
                    "doc_name": sanitize_text(info.get("doc_name", "")),
                    "description": desc,
                    "line_count": info.get("line_count", 0),
                }
            )
        if detailed:
            return json.dumps(
                {"total": len(items), "documents": items}, ensure_ascii=False, indent=2
            )

        lines = [
            "{} | {} | {} 行 | {}".format(
                item["doc_id"],
                item["doc_name"],
                item["line_count"],
                " ".join(item["description"].split()),
            )
            for item in items
        ]
        return "共 {} 份文档（doc_id | 文档名 | 行数 | 描述）\n{}".format(
            len(lines), "\n".join(lines)
        )

    @staticmethod
    def _empty_document_search_result(query: str) -> str:
        return json.dumps(
            {
                "query": query,
                "documents": [],
                "truncated": False,
            },
            ensure_ascii=False,
            indent=2,
        )

    def search_document_nodes(
        self, query: str, doc_ids: list[str] | None = None
    ) -> str:
        """全文检索并在每个命中文档中返回可读取的节点提示。"""
        if self._full_text_retriever is None:
            raise RuntimeError("全文检索未启用。")
        return self._search_via_fts(query, doc_ids=doc_ids)

    def find_semantic_documents(self, query: str, top_k: int = 5) -> list[dict]:
        """返回语义相关的文档及其节点提示，不读取正文。"""
        if self._semantic_document_retriever is None:
            raise RuntimeError("向量检索未启用，当前知识库没有语义文档路由。")
        if not query.strip():
            return []
        return self._semantic_document_retriever.search(query, limit=top_k)

    def _search_via_fts(self, query: str, doc_ids: list[str] | None = None) -> str:
        """使用 Milvus BM25 命中节点，并转换为 Nianlun 返回结构。"""
        if not query.strip():
            return self._empty_document_search_result(query)

        from nianlun.indexing.fts.config import (
            DOC_DERIVE_LIMIT,
            DOC_TOP_N,
            NODE_PER_DOC,
        )
        from nianlun.indexing.fts.postprocess import postprocess_node_hits, top_doc_ids

        if doc_ids is None:
            hits = self._full_text_retriever.search(query, limit=DOC_DERIVE_LIMIT)
        else:
            hits = self._full_text_retriever.search(
                query, limit=DOC_DERIVE_LIMIT, doc_ids=doc_ids
            )
        doc_desc_hits = [hit for hit in hits if hit.get("source_type") == "doc_desc"]
        summary_doc_ids = top_doc_ids(doc_desc_hits, doc_top_n=DOC_TOP_N)

        raw_node_hits = [
            hit
            for hit in hits
            if hit.get("node_id") and isinstance(hit.get("doc_id"), str)
        ]
        processed_nodes = postprocess_node_hits(
            raw_node_hits,
            per_doc_cap=NODE_PER_DOC,
        )
        truncated = len(processed_nodes) > NODE_MATCH_LIMIT
        selected_nodes = processed_nodes[:NODE_MATCH_LIMIT]

        # 文档摘要和节点是两条独立召回通道；节点命中的文档不必位于摘要 Top-N。
        document_scores: dict[str, float] = {}
        for hit in doc_desc_hits:
            doc_id = hit.get("doc_id")
            if doc_id not in summary_doc_ids:
                continue
            score = float(hit.get("score") or 0)
            document_scores[doc_id] = max(document_scores.get(doc_id, score), score)
        for hit in selected_nodes:
            doc_id = hit["doc_id"]
            score = float(hit.get("score") or 0)
            document_scores[doc_id] = max(document_scores.get(doc_id, score), score)

        selected_doc_ids = sorted(
            document_scores,
            key=lambda doc_id: document_scores[doc_id],
            reverse=True,
        )
        documents_by_id: dict[str, dict[str, Any]] = {}
        for doc_id in selected_doc_ids:
            info = self._meta.get(doc_id, {})
            doc_name = info.get("doc_name", "")
            if not doc_name:
                doc_name = next(
                    (h.get("doc_name", "") for h in hits if h.get("doc_id") == doc_id),
                    "",
                )
            documents_by_id[doc_id] = {
                "doc_id": doc_id,
                "doc_name": sanitize_text(str(doc_name)),
                "node_hints": [],
            }

        for hit_index, hit in enumerate(selected_nodes):
            doc_id = hit["doc_id"]
            document = documents_by_id[doc_id]
            node_id = hit.get("node_id")
            hint = {
                "node_id": node_id,
                "title": sanitize_text(str(hit.get("title", ""))),
                "line_num": hit.get("line_num"),
            }
            if hit_index < NODE_HINT_SUMMARY_LIMIT:
                summary = hit.get("node_summary")
                if isinstance(summary, str) and summary:
                    hint["summary"] = sanitize_text(summary)
                    hint["summary_truncated"] = (
                        hit.get("node_summary_truncated") is True
                    )
            document["node_hints"].append(hint)
        return json.dumps(
            {
                "query": query,
                "documents": list(documents_by_id.values()),
                "truncated": truncated,
            },
            ensure_ascii=False,
            indent=2,
        )

    def get_document(self, doc_id: str) -> str:
        """获取指定文档的元信息：doc_id、名称、完整描述、类型(type)、状态、行数。

        与系统提示中的文档清单相比，这里给出单篇文档的【完整描述】（清单里截断到 150 字）
        以及 type（md / pdf）等元信息；status 对静态知识库恒为 completed。
        属于可选工具，基本概况已在文档清单中，通常无需调用。
        """
        doc, err = self._safe_load_doc(doc_id)
        if err:
            return err
        return json.dumps(
            {
                "doc_id": doc_id,
                "doc_name": sanitize_text(doc.get("doc_name", "")),
                "doc_description": sanitize_text(doc.get("doc_description", "")),
                "type": doc.get("type", ""),
                "status": "completed",
                "line_count": doc.get("line_count", 0),
            },
            ensure_ascii=False,
        )

    def get_structure_outline(self, doc_id: str) -> str:
        """获取指定文档的完整目录结构（节点 ID、标题、行号）--不含正文。

        定位内容的唯一导航层：每个节点给出 line_num 供 get_line_content 取正文。
        结构刻意不含正文/摘要（对齐 Nianlun 正典：结构是纯导航索引，内容现取），
        避免模型拿结构里的内容直接作答而漏取正文、或脑补截断处之外的细节。
        """
        doc, err = self._safe_load_doc(doc_id)
        if err:
            return err

        def walk(nodes, depth=0):
            lines = []
            for node in nodes:
                indent = "  " * depth
                title = sanitize_text(node.get("title", "无标题"))
                line_num = node.get("line_num", "N/A")
                node_id = node.get("node_id", "N/A")
                lines.append(f"{indent}[{node_id}] 第 {line_num} 行: {title}")
                if node.get("nodes"):
                    lines.extend(walk(node["nodes"], depth + 1))
            return lines

        return "\n".join(walk(doc["structure"]))

    def _images_for_node(
        self,
        catalog: dict[str, object] | None,
        node_id: object,
        char_offset: int,
        char_limit: int | None,
    ) -> list[dict[str, Any]]:
        if not catalog:
            return []
        raw_entries = catalog.get("entries")
        if not isinstance(raw_entries, list):
            return []
        window_end = char_offset + char_limit if char_limit is not None else None
        selected: list[dict[str, Any]] = []
        for raw in raw_entries:
            if not isinstance(raw, dict):
                continue
            occurrences = raw.get("occurrences")
            if not isinstance(occurrences, list):
                continue
            for occurrence in occurrences:
                if not isinstance(occurrence, dict):
                    continue
                if occurrence.get("node_id") != node_id:
                    continue
                start = occurrence.get("char_offset")
                end = occurrence.get("char_end")
                if (
                    not isinstance(start, int)
                    or not isinstance(end, int)
                    or start < char_offset
                ):
                    continue
                if window_end is not None and end > window_end:
                    continue
                selected.append(
                    {
                        "image_id": raw.get("image_id"),
                        "asset_relpath": raw.get("asset_relpath"),
                        "sha256": raw.get("sha256"),
                        "media_type": raw.get("media_type"),
                        "size_bytes": raw.get("size_bytes"),
                        "width": raw.get("width"),
                        "height": raw.get("height"),
                        "orientation": raw.get("orientation", 1),
                        "alt_text": raw.get("alt_text", ""),
                        "char_offset": start,
                        "char_end": end,
                        "source_start": start,
                        "source_end": end,
                        "generation": catalog.get("generation"),
                        "content_version": self.content_version,
                        "knowledge_base_id": self.knowledge_base_id,
                    }
                )
        return selected

    def _image_spans_for_node(
        self,
        catalog: dict[str, object] | None,
        node_id: object,
        text: str,
        start: int,
        end: int,
    ) -> list[dict[str, Any]]:
        syntax = parse_image_syntax(text)
        spans = {
            (image.start, image.end): {
                "source_start": image.start,
                "source_end": image.end,
                "kind": image.kind,
            }
            for image in syntax.images
        }
        for hidden in syntax.hidden:
            spans.setdefault(
                (hidden.start, hidden.end),
                {
                    "source_start": hidden.start,
                    "source_end": hidden.end,
                    "kind": "hidden",
                },
            )
        # Reference definitions may live in a different node; the catalog has
        # already resolved those against this immutable document version.
        if catalog:
            entries = catalog.get("entries", [])
            if not isinstance(entries, list):
                raise ValueError("图片目录缺少 entries")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError("图片目录 entry 格式无效")
                for occurrence in entry.get("occurrences", []):
                    first, last = (
                        occurrence.get("char_offset"),
                        occurrence.get("char_end"),
                    )
                    if (
                        occurrence.get("node_id") != node_id
                        or not isinstance(first, int)
                        or not isinstance(last, int)
                    ):
                        continue
                    spans.setdefault(
                        (first, last),
                        {"source_start": first, "source_end": last, "kind": "markdown"},
                    )
        return [
            span
            for (first, last), span in sorted(spans.items())
            if first < end and last > start
        ]

    def get_line_content(
        self,
        doc_id: str,
        line_spec: str,
        char_offset: int = 0,
        char_limit: int | None = None,
    ) -> str:
        """获取节点正文，支持按字符窗口滑动读取长节点。

        ``line_spec`` 定位节点；``char_offset``/``char_limit`` 作用于每个命中的
        节点。未提供 ``char_limit`` 时返回完整节点，不做固定长度截断；传入窗口
        后，结果会返回 ``next_char_offset``，供下一次调用继续读取。
        """
        doc, err = self._safe_load_doc(doc_id)
        if err:
            return err

        if not isinstance(char_offset, int) or char_offset < 0:
            return json.dumps(
                {"error": "char_offset 必须是大于等于 0 的整数。"},
                ensure_ascii=False,
            )
        if char_limit is not None and (
            not isinstance(char_limit, int) or char_limit <= 0
        ):
            return json.dumps(
                {"error": "char_limit 必须是正整数，或省略以读取完整节点。"},
                ensure_ascii=False,
            )

        try:
            line_nums = parse_line_spec(line_spec)
        except (ValueError, AttributeError) as exc:
            return json.dumps(
                {
                    "error": f"无效行号格式 '{line_spec}'，请使用 '5-7', '3,8' 或 '12'。错误: {exc}"
                },
                ensure_ascii=False,
            )

        min_line, max_line = min(line_nums), max(line_nums)
        target_lines = set(line_nums)
        results = []
        catalog: dict[str, object] | None = None
        catalog_loaded = False

        def find_content(nodes):
            nonlocal catalog, catalog_loaded
            for node in nodes:
                ln = node.get("line_num", 0)
                if ln and min_line <= ln <= max_line and ln in target_lines:
                    if not catalog_loaded:
                        # Reuse validation within this call only, so later reads
                        # still detect damaged artifacts in the same snapshot.
                        catalog = self._workspace_view.read_image_catalog(doc_id)
                        catalog_loaded = True
                    full_text = sanitize_text(node.get("text", ""))
                    total_chars = len(full_text)
                    end = total_chars
                    if char_limit is not None:
                        end = min(total_chars, char_offset + char_limit)
                    text = full_text[char_offset:end]
                    text_truncated = end < total_chars
                    results.append(
                        {
                            "node_id": node.get("node_id"),
                            "title": sanitize_text(node.get("title", "")),
                            "line_num": ln,
                            "text": text,
                            "char_offset": char_offset,
                            "char_limit": char_limit,
                            "total_chars": total_chars,
                            "text_truncated": text_truncated,
                            "next_char_offset": end if text_truncated else None,
                            "images": self._images_for_node(
                                catalog,
                                node.get("node_id"),
                                char_offset,
                                char_limit,
                            ),
                            "image_spans": self._image_spans_for_node(
                                catalog,
                                node.get("node_id"),
                                full_text,
                                char_offset,
                                end,
                            ),
                        }
                    )
                if node.get("nodes"):
                    find_content(node["nodes"])

        find_content(doc["structure"])
        results.sort(key=lambda item: item["line_num"])

        return json.dumps(
            {
                "doc_id": doc_id,
                "doc_name": sanitize_text(doc.get("doc_name", "")),
                "line_spec": line_spec,
                "matches": len(results),
                "has_more": any(item["text_truncated"] for item in results),
                "content": results,
            },
            ensure_ascii=False,
            indent=2,
        )


if __name__ == "__main__":
    from nianlun.knowledgebase.workspace_ownership import require_legacy_workspace

    require_legacy_workspace(WORKSPACE_DIR)
    kb = KnowledgeBase()
    print(kb.list_documents(detailed=True))
    print(kb.list_documents(detailed=False))
