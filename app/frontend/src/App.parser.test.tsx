// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "./App";
import { api } from "./api/client";
import type { KnowledgeBase, KnowledgeBaseDocument, ModelProfile } from "./types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("./api/client", async (original) => {
  const module = await original<typeof import("./api/client")>();
  return { ...module, api: { ...module.api, listApps: vi.fn().mockResolvedValue([]), listModels: vi.fn().mockResolvedValue([]), listKnowledgeBases: vi.fn(), listKnowledgeBaseDocuments: vi.fn(), updateKnowledgeBase: vi.fn(), reparseKnowledgeBaseDocument: vi.fn(), rebuildFts: vi.fn().mockResolvedValue({}), rebuildVector: vi.fn() } };
});

const kb: KnowledgeBase = {
  id: "kb-1", name: "资料库", description: "", status: "ready", document_count: 1,
  summary_enabled: false, heading_recovery_enabled: false, subtree_folding_enabled: false, min_subtree_tokens: 1200, llm_model_id: null,
  parser_options: { is_ocr: null, enable_table: null, enable_formula: null, language: null },
  workspace_dir: "", workspace_relpath: null, content_version: 1, fts_status: "ready", fts_revision: 1, fts_target_revision: 1, fts_collection: null, fts_error: null,
  vector_status: "disabled", vector_enabled: false, vector_revision: null, vector_target_revision: null, vector_collection: null, vector_error: null, embedding_model_id: null,
  vector_model_id: null, vector_model_updated_at: null, vector_dimension: null, vector_progress_stage: null, vector_documents_total: null, vector_documents_completed: null, vector_records_processed: null,
  created_at: "", updated_at: "",
};
const documentRecord: KnowledgeBaseDocument = {
  id: "doc-1", knowledge_base_id: kb.id, original_filename: "资料.pdf", file_extension: ".pdf", mime_type: "application/pdf", size_bytes: 42,
  parser: "mineru", status: "ready", pipeline_generation: 1, current_stage: "complete", stage_state: "succeeded", failed_stage: null,
  progress_completed: null, progress_total: null, progress_unit: null, warnings: [], parsed_content_version: 1, published_content_version: 1,
  progress: { completed: null, total: null, unit: null }, parse: { chunks_completed: 0, chunks_total: 0, pages_completed: 0, pages_total: 0 },
  error_code: null, error_message: null, latest_task: null, artifacts: [], created_at: "", updated_at: "", completed_at: "",
};
const embeddingModel: ModelProfile = {
  supports_vision: null, id: "embedding-2", kind: "embedding", name: "Second", model: "second",
  context_window_tokens: null, base_url: "https://embedding.test/v1", api_key_configured: true,
  enabled: true, api_mode: "saas_precision", dimension: 1024, model_version: "pipeline",
  language: "ch", is_ocr: false, enable_table: true, enable_formula: true, page_ranges: "",
  is_default: true, created_at: "", updated_at: "",
};
let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  localStorage.clear(); window.location.hash = "knowledge";
  vi.mocked(api.listKnowledgeBases).mockResolvedValue([kb]);
  vi.mocked(api.listModels).mockResolvedValue([]);
  vi.mocked(api.listKnowledgeBaseDocuments).mockResolvedValue([documentRecord]);
  container = document.createElement("div"); document.body.appendChild(container); root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); vi.clearAllMocks(); vi.restoreAllMocks(); vi.useRealTimers(); });
const open = async () => {
  await act(async () => root.render(<App />));
  await act(async () => container.querySelector<HTMLElement>(".knowledge-list [role='button']")!.click());
};
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
};
const submitReparse = async (filename = documentRecord.original_filename) => {
  act(() => container.querySelector<HTMLButtonElement>(`[aria-label='${filename}的更多操作']`)!.click());
  act(() => Array.from(container.querySelectorAll<HTMLButtonElement>("[role='menuitem']")).find((item) => item.textContent === "重新解析")!.click());
  await act(async () => container.querySelector<HTMLFormElement>("[role='dialog'] form")!.requestSubmit());
};
const queuedDocument: KnowledgeBaseDocument = { ...documentRecord, status: "uploaded", current_stage: "parse", stage_state: "queued", pipeline_generation: 2 };

it("merges concurrent reasoning and parser saves without replacing either setting", async () => {
  const reasoning = deferred<KnowledgeBase>();
  const parser = deferred<KnowledgeBase>();
  vi.mocked(api.updateKnowledgeBase).mockReturnValueOnce(reasoning.promise).mockReturnValueOnce(parser.promise);
  await open();
  act(() => container.querySelector<HTMLButtonElement>("[aria-label='知识库设置']")!.click());
  const select = container.querySelector<HTMLSelectElement>("[aria-label='思考程度']");
  expect(select).not.toBeNull();
  await act(async () => { select!.value = "high"; select!.dispatchEvent(new Event("change", { bubbles: true })); });
  const ocr = container.querySelector<HTMLSelectElement>("[aria-label='OCR']")!;
  await act(async () => { ocr.value = "true"; ocr.dispatchEvent(new Event("change", { bubbles: true })); });
  await act(async () => reasoning.resolve({ ...kb, reasoning_effort: "high" }));
  await act(async () => parser.resolve({ ...kb, parser_options: { ...kb.parser_options, is_ocr: true } }));
  expect(api.updateKnowledgeBase).toHaveBeenCalledWith(kb.id, { reasoning_effort: "high" });
  expect(select!.value).toBe("high");
  expect(ocr.value).toBe("true");
});

it.each(["inherit", "default", "low", "medium", "high"])("creates an Agent with reasoning override %s", async (effort) => {
  window.location.hash = "applications";
  vi.mocked(api.listModels).mockResolvedValue([{ ...embeddingModel, id: "llm-1", kind: "llm" }]);
  const create = vi.spyOn(api, "createApp").mockResolvedValue({ id: "app-1", name: "助手", description: "", knowledge_base_id: kb.id, llm_model_id: "llm-1", provider: "default", agent_version: "v0", search_mode: "fts", created_at: "", updated_at: "" });
  await act(async () => root.render(<App />));
  const button = Array.from(container.querySelectorAll<HTMLButtonElement>("button")).find((item) => item.textContent?.trim() === "创建 Agent" || item.textContent?.trim() === "新建 Agent")!;
  await act(async () => button.click());
  const select = container.querySelector<HTMLSelectElement>("#app-reasoning");
  expect(select?.value).toBe("inherit");
  await act(async () => {
    select!.value = effort; select!.dispatchEvent(new Event("change", { bubbles: true }));
    const name = container.querySelector<HTMLInputElement>("#app-name")!;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(name, "助手");
    name.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => container.querySelector<HTMLFormElement>("[role='dialog'] form")!.requestSubmit());
  expect(create).toHaveBeenCalledWith(expect.objectContaining({ reasoning_effort: effort }));
});

it("applies a reparse response after the final old-generation poll and resumes polling", async () => {
  vi.useFakeTimers();
  const parsingDocument: KnowledgeBaseDocument = { ...documentRecord, id: "doc-2", original_filename: "其他.pdf", status: "parsing", current_stage: "parse", stage_state: "running" };
  vi.mocked(api.listKnowledgeBaseDocuments).mockResolvedValueOnce([documentRecord, parsingDocument]).mockResolvedValue([documentRecord, { ...parsingDocument, status: "ready" }]);
  const response = deferred<KnowledgeBaseDocument>();
  vi.mocked(api.reparseKnowledgeBaseDocument).mockReturnValue(response.promise);
  await open();
  await submitReparse();
  await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
  expect(container.querySelectorAll(".document-row .knowledge-status.ready")).toHaveLength(2);
  await act(async () => response.resolve(queuedDocument));
  expect(container.querySelector(".document-row")?.textContent).toContain("解析 · 排队中");
  vi.mocked(api.listKnowledgeBaseDocuments).mockResolvedValue([queuedDocument, { ...parsingDocument, status: "ready" }]);
  await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
  expect(api.listKnowledgeBaseDocuments).toHaveBeenCalledTimes(3);
});

it("does not let an in-flight poll overwrite a successful reparse response", async () => {
  vi.useFakeTimers();
  const parsingDocument: KnowledgeBaseDocument = { ...documentRecord, id: "doc-2", original_filename: "其他.pdf", status: "parsing" };
  const poll = deferred<KnowledgeBaseDocument[]>();
  vi.mocked(api.listKnowledgeBaseDocuments).mockResolvedValueOnce([documentRecord, parsingDocument]).mockReturnValueOnce(poll.promise);
  const response = deferred<KnowledgeBaseDocument>();
  vi.mocked(api.reparseKnowledgeBaseDocument).mockReturnValue(response.promise);
  await open();
  await submitReparse();
  await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
  await act(async () => response.resolve(queuedDocument));
  await act(async () => poll.resolve([documentRecord, { ...parsingDocument, status: "ready" }]));
  expect(container.querySelector(".document-row")?.textContent).toContain("解析 · 排队中");
});

it("clears manual refresh loading when a reparse supersedes its response", async () => {
  const refresh = deferred<KnowledgeBaseDocument[]>();
  vi.mocked(api.listKnowledgeBaseDocuments).mockResolvedValueOnce([documentRecord]).mockReturnValueOnce(refresh.promise);
  const response = deferred<KnowledgeBaseDocument>();
  vi.mocked(api.reparseKnowledgeBaseDocument).mockReturnValue(response.promise);
  await open();
  await submitReparse();
  const refreshButton = container.querySelector<HTMLButtonElement>("[aria-label='刷新文档']")!;
  act(() => refreshButton.click());
  expect(refreshButton.disabled).toBe(true);
  await act(async () => response.resolve(queuedDocument));
  await act(async () => refresh.resolve([documentRecord]));
  expect(refreshButton.disabled).toBe(false);
  expect(container.querySelector(".document-row")?.textContent).toContain("解析 · 排队中");
});

it("ignores a reparse response after closing and reopening the same knowledge base", async () => {
  vi.useFakeTimers();
  const response = deferred<KnowledgeBaseDocument>();
  vi.mocked(api.reparseKnowledgeBaseDocument).mockReturnValue(response.promise);
  await open();
  await submitReparse();
  act(() => container.querySelector<HTMLButtonElement>("[aria-label='返回知识库']")!.click());
  await act(async () => container.querySelector<HTMLElement>(".knowledge-list [role='button']")!.click());
  await act(async () => response.resolve(queuedDocument));
  expect(container.querySelector(".document-row")?.textContent).toContain("可用");
  expect(container.querySelector(".document-row")?.textContent).not.toContain("排队中");
});

it("ignores a reparse response after switching knowledge bases", async () => {
  const otherKb = { ...kb, id: "kb-2", name: "其他库" };
  const otherDocument = { ...documentRecord, knowledge_base_id: otherKb.id, original_filename: "其他库.pdf" };
  vi.mocked(api.listKnowledgeBases).mockResolvedValue([kb, otherKb]);
  vi.mocked(api.listKnowledgeBaseDocuments).mockImplementation(async (id) => id === kb.id ? [documentRecord] : [otherDocument]);
  const response = deferred<KnowledgeBaseDocument>();
  vi.mocked(api.reparseKnowledgeBaseDocument).mockReturnValue(response.promise);
  await open();
  await submitReparse();
  act(() => container.querySelector<HTMLButtonElement>("[aria-label='返回知识库']")!.click());
  await act(async () => container.querySelectorAll<HTMLElement>(".knowledge-list [role='button']")[1].click());
  await act(async () => response.resolve(queuedDocument));
  expect(container.querySelector(".document-row")?.textContent).toContain(otherDocument.original_filename);
  expect(container.querySelector(".document-row")?.textContent).toContain("可用");
});

it("applies concurrent reparse responses for different documents independently", async () => {
  const otherDocument = { ...documentRecord, id: "doc-2", original_filename: "其他.pdf" };
  vi.mocked(api.listKnowledgeBaseDocuments).mockResolvedValue([documentRecord, otherDocument]);
  const first = deferred<KnowledgeBaseDocument>();
  const second = deferred<KnowledgeBaseDocument>();
  vi.mocked(api.reparseKnowledgeBaseDocument).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
  await open();
  await submitReparse();
  await submitReparse(otherDocument.original_filename);
  await act(async () => first.resolve(queuedDocument));
  await act(async () => second.resolve({ ...queuedDocument, id: otherDocument.id, original_filename: otherDocument.original_filename }));
  expect(Array.from(container.querySelectorAll(".document-row .knowledge-status")).map((item) => item.textContent)).toEqual(["解析 · 排队中", "解析 · 排队中"]);
});

it("does not replace a newer generation observed by polling with an older command response", async () => {
  vi.useFakeTimers();
  const response = deferred<KnowledgeBaseDocument>();
  vi.mocked(api.reparseKnowledgeBaseDocument).mockReturnValue(response.promise);
  vi.mocked(api.listKnowledgeBaseDocuments).mockResolvedValueOnce([documentRecord, { ...documentRecord, id: "doc-2", status: "parsing" }]).mockResolvedValue([{ ...documentRecord, pipeline_generation: 3 }]);
  await open();
  await submitReparse();
  await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
  await act(async () => response.resolve(queuedDocument));
  expect(container.querySelector(".document-row")?.textContent).toContain("可用");
});

it("persists parser options without automatically reparsing existing documents", async () => {
  vi.mocked(api.updateKnowledgeBase).mockResolvedValue({ ...kb, parser_options: { ...kb.parser_options, is_ocr: true } });
  await open();
  act(() => container.querySelector<HTMLButtonElement>("[aria-label='知识库设置']")!.click());
  const select = container.querySelector<HTMLSelectElement>("[aria-label='OCR']");
  expect(select).not.toBeNull();
  await act(async () => { select!.value = "true"; select!.dispatchEvent(new Event("change", { bubbles: true })); });
  expect(api.updateKnowledgeBase).toHaveBeenCalledWith(kb.id, { parser_options: { ...kb.parser_options, is_ocr: true } });
  expect(select!.value).toBe("true");
  expect(api.reparseKnowledgeBaseDocument).not.toHaveBeenCalled();
});

it("retains OCR after a delayed unrelated settings response", async () => {
  const summary = deferred<KnowledgeBase>();
  vi.mocked(api.updateKnowledgeBase).mockImplementation(async (_id, body) => {
    if ("summary_enabled" in body) return summary.promise;
    return { ...kb, ...body };
  });
  await open();
  act(() => container.querySelector<HTMLButtonElement>("[aria-label='知识库设置']")!.click());
  act(() => container.querySelector<HTMLInputElement>(".detail-summary-switch input")!.click());
  const ocr = container.querySelector<HTMLSelectElement>("[aria-label='OCR']")!;
  await act(async () => {
    ocr.value = "true";
    ocr.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(ocr.value).toBe("true");
  await act(async () => summary.resolve({ ...kb, summary_enabled: true }));
  const table = container.querySelector<HTMLSelectElement>("[aria-label='表格识别']")!;
  await act(async () => {
    table.value = "false";
    table.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(api.updateKnowledgeBase).toHaveBeenLastCalledWith(kb.id, {
    parser_options: { ...kb.parser_options, is_ocr: true, enable_table: false },
  });
});

it("does not let an old polling snapshot undo saved parser options", async () => {
  vi.useFakeTimers();
  const poll = deferred<KnowledgeBase[]>();
  const parsingDocument: KnowledgeBaseDocument = { ...documentRecord, status: "parsing" };
  vi.mocked(api.listKnowledgeBaseDocuments).mockResolvedValue([parsingDocument]);
  vi.mocked(api.listKnowledgeBases).mockResolvedValueOnce([kb]).mockReturnValueOnce(poll.promise).mockResolvedValue([kb]);
  vi.mocked(api.updateKnowledgeBase).mockResolvedValue({ ...kb, parser_options: { ...kb.parser_options, is_ocr: true } });
  await open();
  act(() => container.querySelector<HTMLButtonElement>("[aria-label='知识库设置']")!.click());
  await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
  const ocr = container.querySelector<HTMLSelectElement>("[aria-label='OCR']")!;
  await act(async () => {
    ocr.value = "true";
    ocr.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await act(async () => poll.resolve([kb]));
  expect(ocr.value).toBe("true");
});

it("retains saved parser options when a workspace refresh returns an older snapshot", async () => {
  const parser = deferred<KnowledgeBase>();
  const refresh = deferred<KnowledgeBase[]>();
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.mocked(api.listKnowledgeBases).mockResolvedValueOnce([kb]).mockReturnValueOnce(refresh.promise);
  vi.mocked(api.updateKnowledgeBase).mockReturnValue(parser.promise);
  await open();
  act(() => container.querySelector<HTMLButtonElement>("[aria-label='知识库设置']")!.click());
  const ocr = container.querySelector<HTMLSelectElement>("[aria-label='OCR']")!;
  act(() => {
    ocr.value = "true";
    ocr.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await act(async () => Array.from(container.querySelectorAll<HTMLButtonElement>("button")).find((button) => button.textContent === "重建全文索引")!.click());
  await act(async () => parser.resolve({ ...kb, parser_options: { ...kb.parser_options, is_ocr: true } }));
  await act(async () => refresh.resolve([{ ...kb, fts_status: "building" }, { ...kb, id: "kb-2", name: "新资料库" }]));
  act(() => container.querySelector<HTMLButtonElement>("[aria-label='知识库设置']")!.click());
  expect(container.querySelector<HTMLSelectElement>("[aria-label='OCR']")!.value).toBe("true");
  expect(container.querySelector(".knowledge-index-status .building")).not.toBeNull();
  act(() => Array.from(container.querySelectorAll<HTMLButtonElement>("button")).find((button) => button.textContent === "完成")!.click());
  act(() => container.querySelector<HTMLButtonElement>("[aria-label='返回知识库']")!.click());
  expect(container.querySelectorAll(".knowledge-list [role='button']")).toHaveLength(2);
});

it("keeps a successfully saved summary when an older parser response arrives", async () => {
  const parser = deferred<KnowledgeBase>();
  vi.mocked(api.updateKnowledgeBase).mockImplementation(async (_id, body) => {
    if ("parser_options" in body) return parser.promise;
    return { ...kb, ...body };
  });
  await open();
  act(() => container.querySelector<HTMLButtonElement>("[aria-label='知识库设置']")!.click());
  const ocr = container.querySelector<HTMLSelectElement>("[aria-label='OCR']")!;
  act(() => {
    ocr.value = "true";
    ocr.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await act(async () => container.querySelector<HTMLInputElement>(".detail-summary-switch input")!.click());
  await act(async () => parser.resolve({ ...kb, parser_options: { ...kb.parser_options, is_ocr: true } }));
  expect(container.querySelector<HTMLInputElement>(".detail-summary-switch input")!.checked).toBe(true);
  expect(ocr.value).toBe("true");
});

it("keeps saved settings and reports a failed parser update", async () => {
  vi.mocked(api.updateKnowledgeBase).mockRejectedValue(new Error("解析设置保存失败"));
  await open();
  act(() => container.querySelector<HTMLButtonElement>("[aria-label='知识库设置']")!.click());
  const ocr = container.querySelector<HTMLSelectElement>("[aria-label='OCR']")!;
  await act(async () => {
    ocr.value = "true";
    ocr.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(ocr.value).toBe("inherit");
  expect(container.querySelector("[role='alert']")?.textContent).toContain("解析设置保存失败");
});

it("finishes switching the embedding model before disabling vector search", async () => {
  const enabledKb: KnowledgeBase = { ...kb, embedding_model_id: "embedding-1", vector_enabled: true, vector_status: "ready" };
  const changedKb: KnowledgeBase = { ...enabledKb, embedding_model_id: embeddingModel.id, vector_status: "building" };
  const changed = deferred<KnowledgeBase>();
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.mocked(api.listKnowledgeBases).mockResolvedValue([enabledKb]);
  vi.mocked(api.listModels).mockResolvedValue([{ ...embeddingModel, id: "embedding-1" }, embeddingModel]);
  vi.mocked(api.updateKnowledgeBase).mockImplementation(async (_id, body) => {
    if ("embedding_model_id" in body) return changed.promise;
    return { ...changedKb, vector_enabled: false, vector_status: "disabled" };
  });
  await open();
  act(() => container.querySelector<HTMLButtonElement>("[aria-label='知识库设置']")!.click());
  const model = container.querySelector<HTMLSelectElement>(".detail-vector-model select")!;
  act(() => { model.value = embeddingModel.id; model.dispatchEvent(new Event("change", { bubbles: true })); });
  const toggle = container.querySelector<HTMLInputElement>(".detail-vector-switch input")!;
  await act(async () => toggle.click());
  expect(api.updateKnowledgeBase).toHaveBeenCalledTimes(1);
  await act(async () => changed.resolve(changedKb));
  expect(api.updateKnowledgeBase).toHaveBeenLastCalledWith(kb.id, { vector_enabled: false });
  expect(toggle.checked).toBe(false);
  expect(model.value).toBe(embeddingModel.id);
});

it("continues queued vector changes after a failed model switch", async () => {
  const enabledKb: KnowledgeBase = { ...kb, embedding_model_id: "embedding-1", vector_enabled: true, vector_status: "ready" };
  const changed = deferred<KnowledgeBase>();
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.mocked(api.listKnowledgeBases).mockResolvedValue([enabledKb]);
  vi.mocked(api.listModels).mockResolvedValue([{ ...embeddingModel, id: "embedding-1" }, embeddingModel]);
  vi.mocked(api.updateKnowledgeBase).mockImplementation(async (_id, body) => {
    if ("embedding_model_id" in body) return changed.promise;
    return { ...enabledKb, vector_enabled: false, vector_status: "disabled" };
  });
  await open();
  act(() => container.querySelector<HTMLButtonElement>("[aria-label='知识库设置']")!.click());
  const model = container.querySelector<HTMLSelectElement>(".detail-vector-model select")!;
  act(() => { model.value = embeddingModel.id; model.dispatchEvent(new Event("change", { bubbles: true })); });
  const toggle = container.querySelector<HTMLInputElement>(".detail-vector-switch input")!;
  await act(async () => toggle.click());
  expect(api.updateKnowledgeBase).toHaveBeenCalledTimes(1);
  await act(async () => changed.reject(new Error("模型切换失败")));
  expect(api.updateKnowledgeBase).toHaveBeenLastCalledWith(kb.id, { vector_enabled: false });
  expect(toggle.checked).toBe(false);
  expect(model.value).toBe("embedding-1");
  expect(container.querySelector(".toast-root")?.textContent).toContain("模型切换失败");
});

it("saves parser settings independently while a vector update is pending", async () => {
  const enabledKb: KnowledgeBase = { ...kb, embedding_model_id: "embedding-1", vector_enabled: true, vector_status: "ready" };
  const changed = deferred<KnowledgeBase>();
  vi.mocked(api.listKnowledgeBases).mockResolvedValue([enabledKb]);
  vi.mocked(api.updateKnowledgeBase).mockImplementation(async (_id, body) => {
    if ("vector_enabled" in body) return changed.promise;
    return { ...enabledKb, ...body };
  });
  await open();
  act(() => container.querySelector<HTMLButtonElement>("[aria-label='知识库设置']")!.click());
  act(() => container.querySelector<HTMLInputElement>(".detail-vector-switch input")!.click());
  const ocr = container.querySelector<HTMLSelectElement>("[aria-label='OCR']")!;
  await act(async () => { ocr.value = "true"; ocr.dispatchEvent(new Event("change", { bubbles: true })); });
  expect(ocr.value).toBe("true");
  await act(async () => changed.resolve({ ...enabledKb, vector_enabled: false, vector_status: "disabled" }));
  expect(ocr.value).toBe("true");
});

it("waits for a model switch before rebuilding its vector index", async () => {
  const enabledKb: KnowledgeBase = { ...kb, embedding_model_id: "embedding-1", vector_enabled: true, vector_status: "ready" };
  const changedKb: KnowledgeBase = { ...enabledKb, embedding_model_id: embeddingModel.id, vector_status: "building" };
  const changed = deferred<KnowledgeBase>();
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.mocked(api.listKnowledgeBases).mockResolvedValueOnce([enabledKb]).mockResolvedValue([changedKb]);
  vi.mocked(api.listModels).mockResolvedValue([{ ...embeddingModel, id: "embedding-1" }, embeddingModel]);
  vi.mocked(api.updateKnowledgeBase).mockReturnValue(changed.promise);
  vi.mocked(api.rebuildVector).mockResolvedValue(changedKb);
  await open();
  act(() => container.querySelector<HTMLButtonElement>("[aria-label='知识库设置']")!.click());
  const model = container.querySelector<HTMLSelectElement>(".detail-vector-model select")!;
  act(() => { model.value = embeddingModel.id; model.dispatchEvent(new Event("change", { bubbles: true })); });
  await act(async () => Array.from(container.querySelectorAll<HTMLButtonElement>("button")).find((button) => button.textContent === "构建向量索引")!.click());
  expect(api.rebuildVector).not.toHaveBeenCalled();
  await act(async () => changed.resolve(changedKb));
  expect(api.rebuildVector).toHaveBeenCalledWith(kb.id);
  expect(container.querySelector(".knowledge-index-status .building")).not.toBeNull();
});

it("does not block vector writes for a different knowledge base", async () => {
  const firstKb: KnowledgeBase = { ...kb, embedding_model_id: "embedding-1", vector_enabled: true, vector_status: "ready" };
  const secondKb: KnowledgeBase = { ...firstKb, id: "kb-2", name: "Other" };
  const first = deferred<KnowledgeBase>();
  vi.mocked(api.listKnowledgeBases).mockResolvedValue([firstKb, secondKb]);
  vi.mocked(api.updateKnowledgeBase).mockImplementation(async (id) => id === kb.id ? first.promise : { ...secondKb, vector_enabled: false, vector_status: "disabled" });
  await open();
  act(() => container.querySelector<HTMLButtonElement>("[aria-label='知识库设置']")!.click());
  act(() => container.querySelector<HTMLInputElement>(".detail-vector-switch input")!.click());
  act(() => Array.from(container.querySelectorAll<HTMLButtonElement>("button")).find((button) => button.textContent === "完成")!.click());
  act(() => container.querySelector<HTMLButtonElement>("[aria-label='返回知识库']")!.click());
  await act(async () => container.querySelectorAll<HTMLElement>(".knowledge-list [role='button']")[1].click());
  act(() => container.querySelector<HTMLButtonElement>("[aria-label='知识库设置']")!.click());
  await act(async () => container.querySelector<HTMLInputElement>(".detail-vector-switch input")!.click());
  expect(api.updateKnowledgeBase).toHaveBeenLastCalledWith(secondKb.id, { vector_enabled: false });
  expect(container.querySelector<HTMLInputElement>(".detail-vector-switch input")!.checked).toBe(false);
  await act(async () => first.resolve({ ...firstKb, vector_enabled: false, vector_status: "disabled" }));
});

it("updates the document row after submitting a reparse command", async () => {
  vi.mocked(api.reparseKnowledgeBaseDocument).mockResolvedValue({ ...documentRecord, status: "uploaded", current_stage: "parse", stage_state: "queued", pipeline_generation: 2 });
  await open();
  act(() => container.querySelector<HTMLButtonElement>("[aria-label='资料.pdf的更多操作']")!.click());
  const command = Array.from(container.querySelectorAll<HTMLButtonElement>("[role='menuitem']")).find((item) => item.textContent === "重新解析");
  expect(command).toBeDefined(); act(() => command!.click());
  await act(async () => container.querySelector<HTMLFormElement>("[role='dialog'] form")!.requestSubmit());
  expect(api.reparseKnowledgeBaseDocument).toHaveBeenCalledWith(kb.id, documentRecord.id, null);
  expect(container.querySelector(".document-row")?.textContent).toContain("解析 · 排队中");
  expect(container.querySelector(".toast-root")?.textContent).toContain("已重新提交解析");
});

it("shows a failed reparse in both the dialog and toast without discarding the document", async () => {
  vi.mocked(api.reparseKnowledgeBaseDocument).mockRejectedValue(new Error("解析服务不可用"));
  await open();
  act(() => container.querySelector<HTMLButtonElement>("[aria-label='资料.pdf的更多操作']")!.click());
  act(() => Array.from(container.querySelectorAll<HTMLButtonElement>("[role='menuitem']")).find((item) => item.textContent === "重新解析")!.click());
  await act(async () => container.querySelector<HTMLFormElement>("[role='dialog'] form")!.requestSubmit());
  expect(container.querySelector("[role='dialog'] [role='alert']")?.textContent).toContain("解析服务不可用");
  expect(container.querySelector(".toast-root")?.textContent).toContain("解析服务不可用");
  expect(container.querySelector(".document-row")?.textContent).toContain("可用");
});
