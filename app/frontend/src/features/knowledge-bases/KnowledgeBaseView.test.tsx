// @vitest-environment jsdom

import { act, type ComponentProps } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { KnowledgeBase, ModelProfile } from "../../types";
import { KnowledgeBaseView } from "./KnowledgeBaseView";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const knowledgeBase = (overrides: Partial<KnowledgeBase>): KnowledgeBase => ({
  id: "kb-1",
  name: "产品知识库",
  description: "",
  status: "ready",
  document_count: 1,
  summary_enabled: true,
  workspace_dir: "/tmp/kb-1",
  workspace_relpath: "kb-1",
  content_version: 2,
  fts_status: "ready",
  fts_revision: 2,
  fts_target_revision: 2,
  fts_collection: "fts-kb-1",
  fts_error: null,
  vector_status: "ready",
  vector_enabled: true,
  vector_revision: 1,
  vector_target_revision: 1,
  vector_collection: "vector-kb-1",
  vector_error: null,
  embedding_model_id: "embedding-1",
  vector_model_id: "embedding-1",
  vector_model_updated_at: "2026-08-15T00:00:00Z",
  vector_dimension: 1024,
  vector_progress_stage: "completed",
  vector_documents_total: 1,
  vector_documents_completed: 1,
  vector_records_processed: 10,
  created_at: "2026-08-15T00:00:00Z",
  updated_at: "2026-08-15T00:00:00Z",
  ...overrides,
});

const embeddingModel: ModelProfile = {
  id: "embedding-1",
  kind: "embedding",
  name: "测试 Embedding",
  model: "text-embedding",
  context_window_tokens: null,
  base_url: "https://embedding.test/v1",
  api_key_configured: true,
  enabled: true,
  api_mode: "saas_precision",
  dimension: 1024,
  model_version: "pipeline",
  language: "ch",
  is_ocr: true,
  enable_table: true,
  enable_formula: true,
  page_ranges: "",
  is_default: true,
  created_at: "2026-08-15T00:00:00Z",
  updated_at: "2026-08-15T00:00:00Z",
};

describe("KnowledgeBaseView", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  const renderView = (overrides: Partial<ComponentProps<typeof KnowledgeBaseView>> = {}) => {
    const props: ComponentProps<typeof KnowledgeBaseView> = {
      items: [knowledgeBase({})],
      selectedId: null,
      documents: [],
      documentsLoading: false,
      documentsError: null,
      embeddingModels: [embeddingModel],
      onCreate: vi.fn(),
      onOpen: vi.fn(),
      onBack: vi.fn(),
      onLoadDocuments: vi.fn(),
      onUpload: vi.fn(),
      onRebuildFts: vi.fn(),
      onRebuildVector: vi.fn(),
      onUpdateSummary: vi.fn(),
      onUpdateName: vi.fn(),
      onUpdateEmbeddingModel: vi.fn(),
      onUpdateVectorEnabled: vi.fn(),
      onDeleteDocument: vi.fn(),
      onRetryDocument: vi.fn(),
      onDeleteKnowledgeBase: vi.fn(),
      onReadContent: vi.fn(),
      onReadTree: vi.fn(),
      ...overrides,
    };
    act(() => root.render(<KnowledgeBaseView {...props} />));
    return props;
  };

  it("keeps index actions in the detail view", () => {
    renderView({
      items: [knowledgeBase({ id: "existing" })],
      selectedId: "existing",
    });

    const labels = Array.from(container.querySelectorAll("button")).map((button) => button.textContent);
    expect(labels).toContain("重建向量索引");
    expect(labels).toContain("重建全文索引");
    expect(labels).toContain("上传文档");
    expect(labels).not.toContain("删除知识库");
  });

  it("keeps list cards focused on navigation, rename, and delete", async () => {
    const onOpen = vi.fn();
    const onUpdateName = vi.fn().mockResolvedValue(undefined);
    const onDeleteKnowledgeBase = vi.fn().mockResolvedValue(undefined);
    renderView({ onOpen, onUpdateName, onDeleteKnowledgeBase });

    expect(container.textContent).not.toContain("上传文档");
    expect(container.textContent).not.toContain("构建全文索引");
    expect(container.textContent).not.toContain("重建向量索引");
    expect(container.textContent).not.toContain("编辑名称");
    expect(container.textContent).not.toContain("删除知识库");

    const menuTrigger = container.querySelector('[aria-haspopup="menu"]') as HTMLButtonElement;
    expect(menuTrigger).not.toBeNull();
    act(() => menuTrigger.click());
    expect(container.textContent).toContain("编辑名称");
    expect(container.textContent).toContain("删除知识库");
    act(() => (Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "编辑名称") as HTMLButtonElement).click());
    expect(onOpen).not.toHaveBeenCalled();
    const input = container.querySelector<HTMLInputElement>("#kb-edit-name")!;
    input.value = "新知识库名称";
    await act(async () => {
      container.querySelector<HTMLFormElement>(".modal form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    expect(onUpdateName).toHaveBeenCalledWith("kb-1", "新知识库名称");

    act(() => menuTrigger.click());
    act(() => (Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "删除知识库") as HTMLButtonElement).click());
    expect(onDeleteKnowledgeBase).toHaveBeenCalledWith("kb-1");
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("shows rebuild for a previously built but stale vector index", () => {
    act(() => root.render(
      <KnowledgeBaseView
        items={[knowledgeBase({ id: "existing" })]}
        selectedId="existing"
        documents={[]}
        documentsLoading={false}
        documentsError={null}
        embeddingModels={[embeddingModel]}
        onCreate={vi.fn()}
        onOpen={vi.fn()}
        onBack={vi.fn()}
        onLoadDocuments={vi.fn()}
        onUpload={vi.fn()}
        onRebuildFts={vi.fn()}
        onRebuildVector={vi.fn()}
        onUpdateSummary={vi.fn()}
        onUpdateName={vi.fn()}
        onUpdateEmbeddingModel={vi.fn()}
        onUpdateVectorEnabled={vi.fn()}
        onDeleteDocument={vi.fn()}
        onRetryDocument={vi.fn()}
        onDeleteKnowledgeBase={vi.fn()}
        onReadContent={vi.fn()}
        onReadTree={vi.fn()}
      />,
    ));

    const labels = Array.from(container.querySelectorAll("button")).map((button) => button.textContent);
    expect(labels).toContain("重建向量索引");
  });

  it("uses a separate toggle without clearing the selected model", () => {
    const onUpdateVectorEnabled = vi.fn();
    act(() => root.render(
      <KnowledgeBaseView
        items={[knowledgeBase({})]}
        selectedId="kb-1"
        documents={[]}
        documentsLoading={false}
        documentsError={null}
        embeddingModels={[embeddingModel]}
        onCreate={vi.fn()}
        onOpen={vi.fn()}
        onBack={vi.fn()}
        onLoadDocuments={vi.fn()}
        onUpload={vi.fn()}
        onRebuildFts={vi.fn()}
        onRebuildVector={vi.fn()}
        onUpdateSummary={vi.fn()}
        onUpdateName={vi.fn()}
        onUpdateEmbeddingModel={vi.fn()}
        onUpdateVectorEnabled={onUpdateVectorEnabled}
        onDeleteDocument={vi.fn()}
        onRetryDocument={vi.fn()}
        onDeleteKnowledgeBase={vi.fn()}
        onReadContent={vi.fn()}
        onReadTree={vi.fn()}
      />,
    ));

    const toggle = container.querySelector<HTMLInputElement>(".detail-vector-switch input")!;
    expect(toggle.checked).toBe(true);
    expect(container.querySelector<HTMLSelectElement>(".detail-vector-model select")!.value).toBe("embedding-1");
    act(() => toggle.click());
    expect(onUpdateVectorEnabled).toHaveBeenCalledWith("kb-1", false);
  });

  it("opens the reader with the index tree and jumps to a heading on click", async () => {
    const onReadContent = vi.fn().mockResolvedValue("# 概述\n\n正文第一段。\n\n## 方案\n\n正文第二段。");
    const onReadTree = vi.fn().mockResolvedValue({
      doc_name: "report.md",
      doc_description: null,
      line_count: 7,
      structure: [
        {
          title: "概述",
          node_id: "0001",
          line_num: 1,
          nodes: [{ title: "方案", node_id: "0002", line_num: 5 }],
        },
      ],
    });
    const scrolled: Element[] = [];
    Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this); };
    renderView({
      selectedId: "kb-1",
      documents: [{
        id: "doc-1",
        knowledge_base_id: "kb-1",
        original_filename: "report.md",
        file_extension: ".md",
        mime_type: "text/markdown",
        size_bytes: 64,
        parser: "native_markdown",
        status: "ready",
        parsed_content_version: 1,
        error_code: null,
        error_message: null,
        latest_task: null,
        artifacts: [],
        created_at: "2026-08-15T00:00:00Z",
        updated_at: "2026-08-15T00:00:00Z",
        completed_at: "2026-08-15T00:00:00Z",
      }],
      onReadContent,
      onReadTree,
    });

    await act(async () => {
      (Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "查看解析内容") as HTMLButtonElement).click();
    });
    expect(onReadContent).toHaveBeenCalledWith("kb-1", "doc-1");
    expect(onReadTree).toHaveBeenCalledWith("kb-1", "doc-1");

    const tree = container.querySelector(".reader-tree")!;
    expect(tree.textContent).toContain("概述");
    expect(tree.textContent).toContain("索引树");

    const headings = Array.from(container.querySelectorAll<HTMLElement>(".reader-markdown [data-line]"));
    expect(headings.map((heading) => heading.dataset.line)).toEqual(["1", "5"]);
    // The reader opens inline directly under its document row, not above the list.
    const readerRow = container.querySelector(".document-reader")!.parentElement!;
    expect(readerRow.children[0].classList.contains("document-row")).toBe(true); expect(readerRow.children[1].classList.contains("document-reader")).toBe(true);

    await act(async () => {
      (Array.from(tree.querySelectorAll("button")).find((button) => button.textContent === "方案") as HTMLButtonElement).click();
    });
    const jumpTargets = scrolled.filter((element) => element.hasAttribute("data-line"));
    expect(jumpTargets).toHaveLength(1);
    expect(jumpTargets[0]).toBe(headings[1]);

    act(() => {
      (Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "源码") as HTMLButtonElement).click();
    });
    const sourceLines = Array.from(container.querySelectorAll<HTMLElement>(".reader-source [data-line]"));
    expect(sourceLines.map((line) => line.dataset.line)).toEqual(["1", "2", "3", "4", "5", "6", "7"]);
  });

  it("uses original Markdown lines when a rendered HTML table changes line count", async () => {
    const onReadContent = vi.fn().mockResolvedValue("# 概述\n\n<table>\n<tr><th>参数</th></tr>\n<tr><td>数值</td></tr>\n</table>\n\n## 方案\n\n正文。\n");
    const onReadTree = vi.fn().mockResolvedValue({
      doc_name: "report.md",
      doc_description: null,
      line_count: 10,
      structure: [{
        title: "概述",
        node_id: "0001",
        line_num: 1,
        nodes: [{ title: "方案", node_id: "0002", line_num: 8 }],
      }],
    });
    const scrolled: Element[] = [];
    Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this); };
    renderView({
      selectedId: "kb-1",
      documents: [{
        id: "doc-1",
        knowledge_base_id: "kb-1",
        original_filename: "report.md",
        file_extension: ".md",
        mime_type: "text/markdown",
        size_bytes: 64,
        parser: "native_markdown",
        status: "ready",
        parsed_content_version: 1,
        error_code: null,
        error_message: null,
        latest_task: null,
        artifacts: [],
        created_at: "2026-08-15T00:00:00Z",
        updated_at: "2026-08-15T00:00:00Z",
        completed_at: "2026-08-15T00:00:00Z",
      }],
      onReadContent,
      onReadTree,
    });

    await act(async () => {
      (Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "查看解析内容") as HTMLButtonElement).click();
    });
    const headings = Array.from(container.querySelectorAll<HTMLElement>(".reader-markdown [data-line]"));
    expect(headings.map((heading) => heading.dataset.line)).toEqual(["1", "8"]);

    await act(async () => {
      (Array.from(container.querySelector(".reader-tree")!.querySelectorAll("button")).find((button) => button.textContent === "方案") as HTMLButtonElement).click();
    });
    expect(scrolled.filter((element) => element.hasAttribute("data-line"))).toEqual([headings[1]]);
  });

  it("expands and collapses the complete document index tree", async () => {
    Element.prototype.scrollIntoView = () => {};
    const onReadContent = vi.fn().mockResolvedValue("# 概述\n\n## 方案\n\n### 细节\n\n#### 子细节\n");
    const onReadTree = vi.fn().mockResolvedValue({
      doc_name: "report.md",
      doc_description: null,
      line_count: 7,
      structure: [{
        title: "概述",
        node_id: "0001",
        line_num: 1,
        nodes: [{
          title: "方案",
          node_id: "0002",
          line_num: 3,
          nodes: [{
            title: "细节",
            node_id: "0003",
            line_num: 5,
            nodes: [{ title: "子细节", node_id: "0004", line_num: 7 }],
          }],
        }],
      }],
    });
    renderView({
      selectedId: "kb-1",
      documents: [{
        id: "doc-1",
        knowledge_base_id: "kb-1",
        original_filename: "report.md",
        file_extension: ".md",
        mime_type: "text/markdown",
        size_bytes: 64,
        parser: "native_markdown",
        status: "ready",
        parsed_content_version: 1,
        error_code: null,
        error_message: null,
        latest_task: null,
        artifacts: [],
        created_at: "2026-08-15T00:00:00Z",
        updated_at: "2026-08-15T00:00:00Z",
        completed_at: "2026-08-15T00:00:00Z",
      }],
      onReadContent,
      onReadTree,
    });

    await act(async () => {
      (Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "查看解析内容") as HTMLButtonElement).click();
    });
    const tree = container.querySelector(".reader-tree")!;
    expect(tree.textContent).toContain("方案");
    expect(tree.textContent).not.toContain("子细节");

    await act(async () => {
      (Array.from(tree.querySelectorAll("button")).find((button) => button.textContent === "展开") as HTMLButtonElement).click();
    });
    expect(tree.textContent).toContain("子细节");

    await act(async () => {
      (Array.from(tree.querySelectorAll("button")).find((button) => button.textContent === "收起") as HTMLButtonElement).click();
    });
    expect(tree.textContent).not.toContain("方案");
  });

  it("keeps showing content when the tree request fails", async () => {
    Element.prototype.scrollIntoView = () => {};
    const onReadContent = vi.fn().mockResolvedValue("# 只有正文\n");
    const onReadTree = vi.fn().mockRejectedValue(new Error("文档索引树不存在"));
    renderView({
      selectedId: "kb-1",
      documents: [{
        id: "doc-1",
        knowledge_base_id: "kb-1",
        original_filename: "notes.md",
        file_extension: ".md",
        mime_type: "text/markdown",
        size_bytes: 16,
        parser: "native_markdown",
        status: "ready",
        parsed_content_version: 1,
        error_code: null,
        error_message: null,
        latest_task: null,
        artifacts: [],
        created_at: "2026-08-15T00:00:00Z",
        updated_at: "2026-08-15T00:00:00Z",
        completed_at: "2026-08-15T00:00:00Z",
      }],
      onReadContent,
      onReadTree,
    });

    await act(async () => {
      (Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "查看解析内容") as HTMLButtonElement).click();
    });
    const reader = container.querySelector(".document-reader")!;
    expect(reader.textContent).toContain("文档索引树不存在");
    expect(container.querySelector(".reader-markdown")!.textContent).toContain("只有正文");
  });
});
