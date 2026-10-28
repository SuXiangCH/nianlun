// @vitest-environment jsdom

import { act, type ComponentProps } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DocumentPreview, KnowledgeBase, KnowledgeBaseDocument, ModelProfile } from "../../types";
import { KnowledgeBaseView } from "./KnowledgeBaseView";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const previewContent = (content: string): DocumentPreview => ({ content, rendered_content: content, content_version: 1, images: [] });

const knowledgeBase = (overrides: Partial<KnowledgeBase>): KnowledgeBase => ({
  id: "kb-1",
  name: "产品知识库",
  description: "",
  status: "ready",
  document_count: 1,
  summary_enabled: true,
  heading_recovery_enabled: true,
  subtree_folding_enabled: true,
  min_subtree_tokens: 1200,
  parser_options: { is_ocr: null, enable_table: null, enable_formula: null, language: null },
  llm_model_id: null,
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
  supports_vision: null,
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

const documentRecord = (overrides: Partial<KnowledgeBaseDocument> = {}): KnowledgeBaseDocument => ({
  id: "doc-1", knowledge_base_id: "kb-1", original_filename: "report.md", file_extension: ".md",
  mime_type: "text/markdown", size_bytes: 64, parser: "native_markdown", status: "ready",
  parsed_content_version: 1, pipeline_generation: 1, current_stage: "complete", stage_state: "succeeded",
  failed_stage: null, progress_completed: null, progress_total: null, progress_unit: null, warnings: [],
  published_content_version: 1, progress: { completed: null, total: null, unit: null },
  parse: { chunks_completed: 0, chunks_total: 0, pages_completed: 0, pages_total: 0 },
  error_code: null, error_message: null, latest_task: null, artifacts: [],
  created_at: "2026-08-15T00:00:00Z", updated_at: "2026-08-15T00:00:00Z", completed_at: "2026-08-15T00:00:00Z",
  ...overrides,
});

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
      llmModels: [],
      embeddingModels: [embeddingModel],
      onCreate: vi.fn(),
      onOpen: vi.fn(),
      onBack: vi.fn(),
      onLoadDocuments: vi.fn(),
      onUpload: vi.fn(),
      onRebuildFts: vi.fn(),
      onRebuildVector: vi.fn(),
      onUpdateSummary: vi.fn(),
      onUpdateHeadingRecovery: vi.fn(),
      onUpdateTreeBuildOptions: vi.fn().mockResolvedValue(undefined),
      onUpdateParserOptions: vi.fn().mockResolvedValue(undefined),
      onUpdateLlmModel: vi.fn().mockResolvedValue(undefined),
      onUpdateReasoningEffort: vi.fn().mockResolvedValue(undefined),
      onUpdateName: vi.fn(),
      onUpdateEmbeddingModel: vi.fn(),
      onUpdateVectorEnabled: vi.fn(),
      onDeleteDocument: vi.fn(),
      onRetryDocument: vi.fn(),
      onReparseDocument: vi.fn().mockResolvedValue(undefined),
      onDeleteKnowledgeBase: vi.fn(),
      onReadContent: vi.fn(),
      onReadTree: vi.fn(),
      ...overrides,
    };
    act(() => root.render(<KnowledgeBaseView {...props} />));
    return props;
  };

  const openSettings = () => act(() => container.querySelector<HTMLButtonElement>("[aria-label='知识库设置']")!.click());

  const changeSelect = async (label: string, value: string) => {
    const select = container.querySelector<HTMLSelectElement>(`[aria-label='${label}']`)!;
    expect(select).not.toBeNull();
    await act(async () => { select.value = value; select.dispatchEvent(new Event("change", { bubbles: true })); });
  };

  it("keeps document reasoning independent of the model and supports every override", async () => {
    const props = renderView({ selectedId: "kb-1" }); openSettings();
    expect(container.querySelector<HTMLSelectElement>("[aria-label='思考程度']")?.value).toBe("inherit");
    for (const value of ["low", "medium", "high", "default"]) {
      await changeSelect("思考程度", value);
      expect(props.onUpdateReasoningEffort).toHaveBeenLastCalledWith("kb-1", value);
    }
    expect(props.onUpdateLlmModel).not.toHaveBeenCalled();
  });

  it("disables pending reasoning changes and exposes failed saves without changing the selection", async () => {
    let reject!: (error: Error) => void;
    const pending = new Promise<void>((_resolve, fail) => { reject = fail; });
    renderView({ selectedId: "kb-1", onUpdateReasoningEffort: vi.fn(() => pending) }); openSettings();
    await changeSelect("思考程度", "high");
    const select = container.querySelector<HTMLSelectElement>("[aria-label='思考程度']")!;
    expect(select.disabled).toBe(true);
    await act(async () => reject(new Error("思考程度保存失败")));
    expect(select.disabled).toBe(false);
    expect(select.value).toBe("inherit");
    expect(container.textContent).toContain("思考程度保存失败");
  });

  it("saves parser overrides immediately while preserving other overrides", async () => {
    const options = { is_ocr: null, enable_table: false, enable_formula: true, language: "en" };
    const props = renderView({ selectedId: "kb-1", items: [knowledgeBase({ parser_options: options })] });
    openSettings();
    await changeSelect("OCR", "true");
    expect(props.onUpdateParserOptions).toHaveBeenCalledWith("kb-1", { ...options, is_ocr: true });
    expect(props.onReparseDocument).not.toHaveBeenCalled();
    expect(props.onRebuildFts).not.toHaveBeenCalled();
  });

  it("supports inherited and disabled parser options", async () => {
    const options = { is_ocr: true, enable_table: true, enable_formula: false, language: "en" };
    const props = renderView({ selectedId: "kb-1", items: [knowledgeBase({ parser_options: options })] });
    openSettings();
    await changeSelect("OCR", "inherit");
    expect(props.onUpdateParserOptions).toHaveBeenLastCalledWith("kb-1", { ...options, is_ocr: null });
    await changeSelect("表格识别", "false");
    expect(props.onUpdateParserOptions).toHaveBeenLastCalledWith("kb-1", { ...options, enable_table: false });
    await changeSelect("公式识别", "true");
    expect(props.onUpdateParserOptions).toHaveBeenLastCalledWith("kb-1", { ...options, enable_formula: true });
  });

  it("disables parser settings during persistence and keeps errors visible", async () => {
    let rejectSave!: (reason: Error) => void;
    const pending = new Promise<void>((_resolve, reject) => { rejectSave = reject; });
    renderView({ selectedId: "kb-1", onUpdateParserOptions: vi.fn(() => pending) });
    openSettings();
    await changeSelect("OCR", "true");
    expect(container.querySelector<HTMLSelectElement>("[aria-label='OCR']")!.disabled).toBe(true);
    expect(container.querySelector<HTMLSelectElement>("[aria-label='表格识别']")!.disabled).toBe(true);
    await act(async () => rejectSave(new Error("解析设置保存失败")));
    expect(container.querySelector("[role='alert']")!.textContent).toContain("解析设置保存失败");
    expect(container.querySelector<HTMLSelectElement>("[aria-label='OCR']")!.value).toBe("inherit");
  });

  it.each(["success", "failure"])("keeps settings open until a parser save settles with %s", async (outcome) => {
    let resolveSave!: () => void;
    let rejectSave!: (reason: Error) => void;
    const pending = new Promise<void>((resolve, reject) => { resolveSave = resolve; rejectSave = reject; });
    renderView({ selectedId: "kb-1", onUpdateParserOptions: vi.fn(() => pending) });
    openSettings();
    await changeSelect("OCR", "true");
    const done = Array.from(container.querySelectorAll<HTMLButtonElement>("[role='dialog'] button")).find((button) => button.textContent === "完成")!;
    expect(done.disabled).toBe(true);
    act(() => done.click());
    expect(container.querySelector("[role='dialog']")).not.toBeNull();
    act(() => container.querySelector<HTMLButtonElement>("[aria-label='关闭']")!.click());
    expect(container.querySelector("[role='dialog']")).not.toBeNull();
    act(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(container.querySelector("[role='dialog']")).not.toBeNull();
    act(() => container.querySelector(".modal-backdrop")!.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })));
    expect(container.querySelector("[role='dialog']")).not.toBeNull();
    await act(async () => { if (outcome === "success") resolveSave(); else rejectSave(new Error("解析设置保存失败")); });
    expect(done.disabled).toBe(false);
    if (outcome === "failure") expect(container.querySelector("[role='alert']")?.textContent).toContain("解析设置保存失败");
    act(() => done.click());
    expect(container.querySelector("[role='dialog']")).toBeNull();
  });

  it("blocks Done when a language blur starts saving in the same event batch", async () => {
    let finish!: () => void;
    const onUpdateParserOptions = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
    renderView({ selectedId: "kb-1", onUpdateParserOptions });
    openSettings();
    const language = container.querySelector<HTMLInputElement>("[aria-label='解析语言']")!;
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(language, "en");
      language.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const done = Array.from(container.querySelectorAll<HTMLButtonElement>("[role='dialog'] button")).find((button) => button.textContent === "完成")!;
    act(() => {
      language.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
      done.click();
    });
    expect(onUpdateParserOptions).toHaveBeenCalledTimes(1);
    expect(container.querySelector("[role='dialog']")).not.toBeNull();
    await act(async () => finish());
    act(() => done.click());
    expect(container.querySelector("[role='dialog']")).toBeNull();
  });

  it("saves the language on blur and clears it to inherit", async () => {
    const props = renderView({ selectedId: "kb-1" });
    openSettings();
    const language = container.querySelector<HTMLInputElement>("[aria-label='解析语言']");
    expect(language).not.toBeNull();
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(language, " en ");
      language!.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(props.onUpdateParserOptions).not.toHaveBeenCalled();
    await act(async () => language!.dispatchEvent(new FocusEvent("focusout", { bubbles: true })));
    expect(props.onUpdateParserOptions).toHaveBeenCalledWith("kb-1", { is_ocr: null, enable_table: null, enable_formula: null, language: "en" });
    act(() => root.render(<KnowledgeBaseView {...props} items={[knowledgeBase({ parser_options: { is_ocr: null, enable_table: null, enable_formula: null, language: "en" } })]} />));
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(language, "");
      language!.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => language!.dispatchEvent(new FocusEvent("focusout", { bubbles: true })));
    expect(props.onUpdateParserOptions).toHaveBeenLastCalledWith("kb-1", { is_ocr: null, enable_table: null, enable_formula: null, language: null });
  });

  it.each(["ready", "failed"] as const)("reparses a %s MinerU document using an explicit OCR override", async (status) => {
    const props = renderView({ selectedId: "kb-1", documents: [documentRecord({ parser: "mineru", status })] });
    act(() => container.querySelector<HTMLButtonElement>("[aria-label='report.md的更多操作']")!.click());
    const command = Array.from(container.querySelectorAll<HTMLButtonElement>("[role='menuitem']")).find((button) => button.textContent === "重新解析");
    expect(command).toBeDefined();
    act(() => command!.click());
    await changeSelect("重新解析 OCR", "true");
    await act(async () => Array.from(container.querySelectorAll<HTMLButtonElement>("[role='dialog'] button")).find((button) => button.textContent === "重新解析")!.click());
    expect(props.onReparseDocument).toHaveBeenCalledWith("kb-1", "doc-1", true);
    expect(container.querySelector("[role='dialog']")).toBeNull();
  });

  it("keeps the reparse dialog open after failure and can retry with inherited OCR", async () => {
    const props = renderView({ selectedId: "kb-1", documents: [documentRecord({ parser: "mineru" })], onReparseDocument: vi.fn().mockRejectedValue(new Error("解析服务不可用")) });
    act(() => container.querySelector<HTMLButtonElement>("[aria-label='report.md的更多操作']")!.click());
    const command = Array.from(container.querySelectorAll<HTMLButtonElement>("[role='menuitem']")).find((button) => button.textContent === "重新解析");
    expect(command).toBeDefined();
    act(() => command!.click());
    await act(async () => Array.from(container.querySelectorAll<HTMLButtonElement>("[role='dialog'] button")).find((button) => button.textContent === "重新解析")!.click());
    expect(props.onReparseDocument).toHaveBeenCalledWith("kb-1", "doc-1", null);
    expect(container.querySelector("[role='dialog'] [role='alert']")!.textContent).toContain("解析服务不可用");
  });

  it("keeps the reparse dialog locked while submitting automatic OCR", async () => {
    let finish!: () => void;
    const props = renderView({ selectedId: "kb-1", documents: [documentRecord({ parser: "mineru" })], onReparseDocument: vi.fn(() => new Promise<void>((resolve) => { finish = resolve; })) });
    act(() => container.querySelector<HTMLButtonElement>("[aria-label='report.md的更多操作']")!.click());
    act(() => Array.from(container.querySelectorAll<HTMLButtonElement>("[role='menuitem']")).find((button) => button.textContent === "重新解析")!.click());
    await changeSelect("重新解析 OCR", "false");
    await act(async () => container.querySelector<HTMLFormElement>("[role='dialog'] form")!.requestSubmit());
    expect(props.onReparseDocument).toHaveBeenCalledWith("kb-1", "doc-1", false);
    expect(container.querySelector<HTMLSelectElement>("[aria-label='重新解析 OCR']")!.disabled).toBe(true);
    expect(container.querySelector<HTMLButtonElement>("[role='dialog'] button[type='submit']")!.disabled).toBe(true);
    act(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(container.querySelector("[role='dialog']")).not.toBeNull();
    await act(async () => finish());
    expect(container.querySelector("[role='dialog']")).toBeNull();
  });

  it.each([
    { parser: "native_markdown" as const, status: "ready" as const },
    { parser: "mineru" as const, status: "parsing" as const },
    { parser: "mineru" as const, status: "indexing" as const },
  ])("hides reparse for $parser documents in $status state", (record) => {
    renderView({ selectedId: "kb-1", documents: [documentRecord(record)] });
    act(() => container.querySelector<HTMLButtonElement>("[aria-label='report.md的更多操作']")!.click());
    expect(Array.from(container.querySelectorAll("[role='menuitem']")).some((button) => button.textContent === "重新解析")).toBe(false);
  });

  it("saves the subtree token threshold independently of heading recovery", async () => {
    const onUpdateTreeBuildOptions = vi.fn().mockResolvedValue(undefined);
    renderView({ selectedId: "kb-1", onUpdateTreeBuildOptions });
    openSettings();
    const threshold = container.querySelector<HTMLInputElement>('[aria-label="合并阈值（tokens）"]')!;
    expect(threshold.value).toBe("1200");
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(threshold, "600");
      threshold.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => container.querySelector<HTMLFormElement>(".tree-build-settings")!.requestSubmit());
    expect(onUpdateTreeBuildOptions).toHaveBeenCalledWith("kb-1", { subtree_folding_enabled: true, min_subtree_tokens: 600 });
  });

  it("retains the threshold while disabling folding and shows save errors", async () => {
    renderView({ selectedId: "kb-1", onUpdateTreeBuildOptions: vi.fn().mockRejectedValue(new Error("合并设置保存失败")) });
    openSettings();
    act(() => container.querySelector<HTMLInputElement>('[aria-label="小子树合并"]')!.click());
    expect(container.querySelector<HTMLInputElement>('[aria-label="合并阈值（tokens）"]')!.disabled).toBe(true);
    await act(async () => container.querySelector<HTMLFormElement>(".tree-build-settings")!.requestSubmit());
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("合并设置保存失败");
    expect(container.querySelector<HTMLInputElement>('[aria-label="合并阈值（tokens）"]')!.value).toBe("1200");
  });

  it("validates token thresholds and disables controls while saving", async () => {
    let finish!: () => void;
    const onUpdateTreeBuildOptions = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
    renderView({ selectedId: "kb-1", onUpdateTreeBuildOptions });
    openSettings();
    const threshold = container.querySelector<HTMLInputElement>('[aria-label="合并阈值（tokens）"]')!;
    const button = container.querySelector<HTMLButtonElement>(".tree-build-settings button")!;
    const setValue = (value: string) => act(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(threshold, value);
      threshold.dispatchEvent(new Event("input", { bubbles: true }));
    });
    for (const value of ["", "0", "-1", "1.5"]) {
      setValue(value);
      expect(button.disabled).toBe(true);
    }
    setValue("800");
    await act(async () => container.querySelector<HTMLFormElement>(".tree-build-settings")!.requestSubmit());
    expect(threshold.disabled).toBe(true);
    expect(button.disabled).toBe(true);
    await act(async () => finish());
    expect(threshold.disabled).toBe(false);
    expect(onUpdateTreeBuildOptions).toHaveBeenCalledTimes(1);
  });

  it("selects a document LLM independently and can return to the system default", async () => {
    const llm = { ...embeddingModel, id: "llm-1", kind: "llm" as const, name: "文档模型", model: "document-chat" };
    const props = renderView({ selectedId: "kb-1", llmModels: [llm] });
    openSettings();
    const select = container.querySelector<HTMLSelectElement>("[aria-label='LLM 模型']");
    expect(select).not.toBeNull();
    expect(select!.value).toBe("");
    expect(select!.options[0].textContent).toContain("跟随系统默认");
    expect(select!.options[0].textContent).toContain("document-chat");
    await act(async () => {
      select!.value = "llm-1";
      select!.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(props.onUpdateLlmModel).toHaveBeenCalledWith("kb-1", "llm-1");
    act(() => root.render(<KnowledgeBaseView {...props} items={[knowledgeBase({ llm_model_id: "llm-1" })]} />));
    expect(select!.value).toBe("llm-1");
    await act(async () => {
      select!.value = "";
      select!.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(props.onUpdateLlmModel).toHaveBeenLastCalledWith("kb-1", null);
  });

  it("disables the LLM selector while saving and keeps the saved selection after failure", async () => {
    let rejectSave!: (reason: Error) => void;
    const save = new Promise<void>((_resolve, reject) => { rejectSave = reject; });
    renderView({
      selectedId: "kb-1",
      llmModels: [{ ...embeddingModel, id: "llm-1", kind: "llm", model: "document-chat" }],
      onUpdateLlmModel: vi.fn(() => save),
    });
    openSettings();
    const select = container.querySelector<HTMLSelectElement>("[aria-label='LLM 模型']");
    expect(select).not.toBeNull();
    await act(async () => {
      select!.value = "llm-1";
      select!.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(select!.disabled).toBe(true);
    await act(async () => rejectSave(new Error("模型切换失败")));
    expect(select!.disabled).toBe(false);
    expect(select!.value).toBe("");
    expect(container.querySelector("[role='alert']")?.textContent).toContain("模型切换失败");
  });

  it("shows a missing default without preventing selection of another LLM", () => {
    renderView({ selectedId: "kb-1", llmModels: [] });
    openSettings();
    const select = container.querySelector<HTMLSelectElement>("[aria-label='LLM 模型']");
    expect(select).not.toBeNull();
    expect(select!.options[0].textContent).toContain("未配置");
  });

  it("uses a compact searchable list without repeated headings or placeholder descriptions", () => {
    renderView({ items: [knowledgeBase({}), knowledgeBase({ id: "kb-2", name: "财务资料" })] });
    expect(container.querySelector("nav[aria-label='知识库功能导航']")?.textContent).toContain("知识库");
    expect(container.textContent).not.toContain("未填写描述");
    expect(container.textContent).not.toContain("为 Agent 准备");
    const input = container.querySelector<HTMLInputElement>("[aria-label='搜索知识库']")!;
    expect(input).toBeTruthy();
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "财务");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(container.textContent).toContain("财务资料");
    expect(container.textContent).not.toContain("产品知识库");
  });

  it("keeps index controls in settings and leaves document actions in the detail view", () => {
    renderView({
      items: [knowledgeBase({ id: "existing" })],
      selectedId: "existing",
    });

    expect(container.textContent).not.toContain("重建向量索引");
    expect(container.textContent).not.toContain("知识库详情");
    expect(container.querySelector("[aria-label='刷新文档']")).toBeTruthy();
    openSettings();
    const labels = Array.from(container.querySelectorAll("button")).map((button) => button.textContent);
    expect(labels).toContain("重建向量索引");
    expect(labels).toContain("重建全文索引");
    expect(labels).toContain("上传文档");
    expect(labels).not.toContain("删除知识库");
  });

  it("filters documents, keeps errors visible and moves deletion into the document menu", () => {
    const props = renderView({ selectedId: "kb-1", documents: [documentRecord(), documentRecord({ id: "doc-2", original_filename: "failed.pdf", status: "failed", current_stage: "parse", stage_state: "failed", failed_stage: "parse", error_message: "解析失败", published_content_version: null })] });
    expect(container.textContent).not.toContain("删除文档");
    expect(container.textContent).toContain("解析失败");
    expect(container.querySelector("[aria-label='查看 failed.pdf']")).toBeNull();
    act(() => container.querySelector<HTMLButtonElement>("[aria-label='report.md的更多操作']")!.click());
    act(() => container.querySelector<HTMLButtonElement>("[role=menuitem]")!.click());
    expect(props.onDeleteDocument).toHaveBeenCalledWith("kb-1", "doc-1");
    act(() => container.querySelector<HTMLButtonElement>("[aria-label='刷新文档']")!.click());
    expect(props.onLoadDocuments).toHaveBeenCalledWith("kb-1");
    const input = container.querySelector<HTMLInputElement>("[aria-label='搜索文档']")!;
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "missing");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(container.querySelector(".knowledge-document-label [role=status]")?.textContent).toBe("0");
    expect(container.textContent).toContain("没有匹配的文档");
  });

  it("preserves upload progress and abort when uploading from the compact toolbar", async () => {
    const onUpload = vi.fn<ComponentProps<typeof KnowledgeBaseView>["onUpload"]>().mockImplementation((_id, _files, progress, signal) => {
      progress?.(5, 10);
      return new Promise((_resolve, reject) => signal?.addEventListener("abort", () => reject(new DOMException("Cancelled", "AbortError")), { once: true }));
    });
    renderView({ selectedId: "kb-1", onUpload });
    act(() => Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "上传文档")!.click());
    const input = container.querySelector<HTMLInputElement>("input[type=file]")!;
    Object.defineProperty(input, "files", { value: [new File(["hello"], "notes.md", { type: "text/markdown" })] });
    act(() => input.dispatchEvent(new Event("change", { bubbles: true })));
    await act(async () => { Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "开始上传")!.click(); });
    expect(container.querySelector(".upload-progress")?.textContent).toContain("50%");
    const signal = onUpload.mock.calls[0][3]!;
    expect(signal.aborted).toBe(false);
    await act(async () => { Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "取消上传")!.click(); });
    expect(signal.aborted).toBe(true);
    expect(container.querySelector(".upload-error")).toBeNull();
    expect(container.querySelector("[role=dialog]")).toBeTruthy();
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
        llmModels={[]}
        embeddingModels={[embeddingModel]}
        onCreate={vi.fn()}
        onOpen={vi.fn()}
        onBack={vi.fn()}
        onLoadDocuments={vi.fn()}
        onUpload={vi.fn()}
        onRebuildFts={vi.fn()}
        onRebuildVector={vi.fn()}
        onUpdateSummary={vi.fn()}
        onUpdateHeadingRecovery={vi.fn()}
        onUpdateTreeBuildOptions={vi.fn()}
        onUpdateParserOptions={vi.fn()}
        onUpdateLlmModel={vi.fn()}
        onUpdateReasoningEffort={vi.fn()}
        onUpdateName={vi.fn()}
        onUpdateEmbeddingModel={vi.fn()}
        onUpdateVectorEnabled={vi.fn()}
        onDeleteDocument={vi.fn()}
        onRetryDocument={vi.fn()}
        onReparseDocument={vi.fn()}
        onDeleteKnowledgeBase={vi.fn()}
        onReadContent={vi.fn()}
        onReadTree={vi.fn()}
      />,
    ));

    openSettings();
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
        llmModels={[]}
        embeddingModels={[embeddingModel]}
        onCreate={vi.fn()}
        onOpen={vi.fn()}
        onBack={vi.fn()}
        onLoadDocuments={vi.fn()}
        onUpload={vi.fn()}
        onRebuildFts={vi.fn()}
        onRebuildVector={vi.fn()}
        onUpdateSummary={vi.fn()}
        onUpdateHeadingRecovery={vi.fn()}
        onUpdateTreeBuildOptions={vi.fn()}
        onUpdateParserOptions={vi.fn()}
        onUpdateLlmModel={vi.fn()}
        onUpdateReasoningEffort={vi.fn()}
        onUpdateName={vi.fn()}
        onUpdateEmbeddingModel={vi.fn()}
        onUpdateVectorEnabled={onUpdateVectorEnabled}
        onDeleteDocument={vi.fn()}
        onRetryDocument={vi.fn()}
        onReparseDocument={vi.fn()}
        onDeleteKnowledgeBase={vi.fn()}
        onReadContent={vi.fn()}
        onReadTree={vi.fn()}
      />,
    ));

    openSettings();
    const toggle = container.querySelector<HTMLInputElement>(".detail-vector-switch input")!;
    expect(toggle.checked).toBe(true);
    expect(container.querySelector<HTMLSelectElement>(".detail-vector-model select")!.value).toBe("embedding-1");
    act(() => toggle.click());
    expect(onUpdateVectorEnabled).toHaveBeenCalledWith("kb-1", false);
  });

  it("updates the heading recovery setting from the knowledge base detail", async () => {
    const onUpdateHeadingRecovery = vi.fn().mockResolvedValue(undefined);
    renderView({
      selectedId: "kb-1",
      onUpdateHeadingRecovery,
    });

    openSettings();
    expect(container.textContent).toContain("修复标题层级");
    const toggle = container.querySelector<HTMLInputElement>(
      ".detail-heading-recovery-switch input",
    )!;
    expect(toggle.checked).toBe(true);
    await act(async () => {
      toggle.click();
    });
    expect(onUpdateHeadingRecovery).toHaveBeenCalledWith("kb-1", false);
  });

  it("opens the reader with the index tree and jumps to a heading on click", async () => {
    const onReadContent = vi.fn().mockResolvedValue(previewContent("# 概述\n\n正文第一段。\n\n## 方案\n\n正文第二段。"));
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
        pipeline_generation: 1,
        current_stage: "complete",
        stage_state: "succeeded",
        failed_stage: null,
        progress_completed: null,
        progress_total: null,
        progress_unit: null,
        warnings: [],
        published_content_version: 1,
        progress: { completed: null, total: null, unit: null },
        parse: { chunks_completed: 0, chunks_total: 0, pages_completed: 0, pages_total: 0 },
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
    const onReadContent = vi.fn().mockResolvedValue(previewContent("# 概述\n\n<table>\n<tr><th>参数</th></tr>\n<tr><td>数值</td></tr>\n</table>\n\n## 方案\n\n正文。\n"));
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
        pipeline_generation: 1,
        current_stage: "complete",
        stage_state: "succeeded",
        failed_stage: null,
        progress_completed: null,
        progress_total: null,
        progress_unit: null,
        warnings: [],
        published_content_version: 1,
        progress: { completed: null, total: null, unit: null },
        parse: { chunks_completed: 0, chunks_total: 0, pages_completed: 0, pages_total: 0 },
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
    const onReadContent = vi.fn().mockResolvedValue(previewContent("# 概述\n\n## 方案\n\n### 细节\n\n#### 子细节\n"));
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
        pipeline_generation: 1,
        current_stage: "complete",
        stage_state: "succeeded",
        failed_stage: null,
        progress_completed: null,
        progress_total: null,
        progress_unit: null,
        warnings: [],
        published_content_version: 1,
        progress: { completed: null, total: null, unit: null },
        parse: { chunks_completed: 0, chunks_total: 0, pages_completed: 0, pages_total: 0 },
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
    const onReadContent = vi.fn().mockResolvedValue(previewContent("# 只有正文\n"));
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
        pipeline_generation: 1,
        current_stage: "complete",
        stage_state: "succeeded",
        failed_stage: null,
        progress_completed: null,
        progress_total: null,
        progress_unit: null,
        warnings: [],
        published_content_version: 1,
        progress: { completed: null, total: null, unit: null },
        parse: { chunks_completed: 0, chunks_total: 0, pages_completed: 0, pages_total: 0 },
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
