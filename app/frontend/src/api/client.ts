import type { Application, BatchUploadResult, ConversationMessageRecord, ConversationRecord, DocumentIndexTree, KnowledgeBase, KnowledgeBaseDocument, ModelConfigTestRequest, ModelConfigTestResult, ModelKind, ModelProfile, ModelProfileRequest, SourceSnippet } from "../types";

const configuredBase = window.NIANLUN_API_BASE || import.meta.env.VITE_API_BASE || "http://127.0.0.1:8000";
export const API_BASE = configuredBase.replace(/\/$/, "");

interface ApiEnvelope<T> { code?: number; message?: string; data?: T; }

export class ApiClientError extends Error {
  readonly status: number;
  readonly requestId: string | null;
  constructor(message: string, status: number, requestId: string | null = null) {
    super(message); this.name = "ApiClientError"; this.status = status; this.requestId = requestId;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { Accept: "application/json", ...(init.body instanceof FormData ? {} : { "Content-Type": "application/json" }), ...init.headers },
  });
  let payload: ApiEnvelope<T> | T | null = null;
  try { payload = await response.json() as ApiEnvelope<T> | T; } catch { payload = null; }
  const envelope = payload && typeof payload === "object" && "data" in payload ? payload as ApiEnvelope<T> : null;
  if (!response.ok || (envelope?.code !== undefined && envelope.code >= 400)) {
    throw new ApiClientError(envelope?.message || `请求失败（${response.status}）`, response.status, response.headers.get("X-Request-Id"));
  }
  return (envelope ? envelope.data : payload) as T;
}

async function requestText(path: string, init: RequestInit = {}): Promise<string> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { Accept: "text/markdown, text/plain", ...init.headers },
  });
  const text = await response.text();
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try {
      const payload = JSON.parse(text) as ApiEnvelope<unknown>;
      message = payload.message || message;
    } catch { /* plain text error */ }
    throw new ApiClientError(message, response.status, response.headers.get("X-Request-Id"));
  }
  return text;
}

function uploadRequest<T>(
  path: string,
  body: FormData,
  onProgress?: (loaded: number, total: number) => void,
  signal?: AbortSignal,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const abort = () => xhr.abort();
    if (signal?.aborted) { reject(new DOMException("请求已取消", "AbortError")); return; }
    signal?.addEventListener("abort", abort, { once: true });
    xhr.open("POST", `${API_BASE}${path}`);
    xhr.setRequestHeader("Accept", "application/json");
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) onProgress?.(event.loaded, event.total);
    });
    xhr.addEventListener("load", () => {
      signal?.removeEventListener("abort", abort);
      let payload: ApiEnvelope<T> | T | null = null;
      try { payload = JSON.parse(xhr.responseText) as ApiEnvelope<T> | T; } catch { /* invalid JSON */ }
      const envelope = payload && typeof payload === "object" && "data" in payload ? payload as ApiEnvelope<T> : null;
      if (xhr.status < 200 || xhr.status >= 300 || (envelope?.code !== undefined && envelope.code >= 400)) {
        reject(new ApiClientError(envelope?.message || `请求失败（${xhr.status}）`, xhr.status, xhr.getResponseHeader("X-Request-Id")));
        return;
      }
      resolve((envelope ? envelope.data : payload) as T);
    });
    xhr.addEventListener("error", () => { signal?.removeEventListener("abort", abort); reject(new ApiClientError("网络连接失败", xhr.status, xhr.getResponseHeader("X-Request-Id"))); });
    xhr.addEventListener("abort", () => { signal?.removeEventListener("abort", abort); reject(new DOMException("请求已取消", "AbortError")); });
    xhr.send(body);
  });
}

export const api = {
  listApps: () => request<Application[]>("/api/v1/apps"),
  createApp: (body: { name: string; description: string; knowledge_base_id: string; llm_model_id: string }) => request<Application>("/api/v1/apps", { method: "POST", body: JSON.stringify(body) }),
  deleteApp: (id: string) => request<null>(`/api/v1/apps/${encodeURIComponent(id)}`, { method: "DELETE" }),
  listKnowledgeBases: () => request<KnowledgeBase[]>("/api/v1/knowledge-bases"),
  updateKnowledgeBase: (id: string, body: { name?: string; summary_enabled?: boolean; embedding_model_id?: string; vector_enabled?: boolean }) => request<KnowledgeBase>(`/api/v1/knowledge-bases/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteKnowledgeBase: (id: string) => request<null>(`/api/v1/knowledge-bases/${encodeURIComponent(id)}`, { method: "DELETE" }),
  listModels: (kind?: ModelKind) => request<ModelProfile[]>(`/api/v1/models${kind ? `?kind=${encodeURIComponent(kind)}` : ""}`),
  createModel: (body: ModelProfileRequest) => request<ModelProfile>("/api/v1/models", { method: "POST", body: JSON.stringify(body) }),
  updateModel: (id: string, body: ModelProfileRequest) => request<ModelProfile>(`/api/v1/models/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteModel: (id: string) => request<null>(`/api/v1/models/${encodeURIComponent(id)}`, { method: "DELETE" }),
  setDefaultModel: (id: string) => request<ModelProfile>(`/api/v1/models/${encodeURIComponent(id)}/default`, { method: "POST" }),
  testModel: (id: string, body?: ModelConfigTestRequest) => request<ModelConfigTestResult>(`/api/v1/models/${encodeURIComponent(id)}/test`, { method: "POST", ...(body ? { body: JSON.stringify(body) } : {}) }),
  testModelDraft: (body: ModelConfigTestRequest) => request<ModelConfigTestResult>("/api/v1/models/test", { method: "POST", body: JSON.stringify(body) }),
  listConversations: (applicationId: string) => request<ConversationRecord[]>(`/api/v1/apps/${encodeURIComponent(applicationId)}/conversations`),
  getConversationMessages: (applicationId: string, conversationId: string) => request<ConversationMessageRecord[]>(`/api/v1/apps/${encodeURIComponent(applicationId)}/conversations/${encodeURIComponent(conversationId)}/messages`),
  getSource: (applicationId: string, conversationId: string, messageId: string, sourceId: string) => request<SourceSnippet>(`/api/v1/apps/${encodeURIComponent(applicationId)}/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(messageId)}/sources/${encodeURIComponent(sourceId)}`),
  deleteConversation: (applicationId: string, conversationId: string) => request<null>(`/api/v1/apps/${encodeURIComponent(applicationId)}/conversations/${encodeURIComponent(conversationId)}`, { method: "DELETE" }),
  createKnowledgeBase: (body: Record<string, unknown>) => request<KnowledgeBase>("/api/v1/knowledge-bases", { method: "POST", body: JSON.stringify(body) }),
  uploadDocument: (knowledgeBaseId: string, file: File, signal?: AbortSignal) => { const form = new FormData(); form.append("file", file); return request<KnowledgeBase>(`/api/v1/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/documents`, { method: "POST", body: form, signal }); },
  uploadDocuments: (knowledgeBaseId: string, files: File[], onProgress?: (loaded: number, total: number) => void, signal?: AbortSignal) => { const form = new FormData(); files.forEach((file) => form.append("files", file)); return uploadRequest<BatchUploadResult>(`/api/v1/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/documents/batch`, form, onProgress, signal); },
  listKnowledgeBaseDocuments: (knowledgeBaseId: string) => request<KnowledgeBaseDocument[]>(`/api/v1/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/documents`),
  getKnowledgeBaseDocument: (knowledgeBaseId: string, documentId: string) => request<KnowledgeBaseDocument>(`/api/v1/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/documents/${encodeURIComponent(documentId)}`),
  retryKnowledgeBaseDocument: (knowledgeBaseId: string, documentId: string) => request<KnowledgeBaseDocument>(`/api/v1/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/documents/${encodeURIComponent(documentId)}/retry`, { method: "POST" }),
  deleteKnowledgeBaseDocument: (knowledgeBaseId: string, documentId: string) => request<null>(`/api/v1/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/documents/${encodeURIComponent(documentId)}`, { method: "DELETE" }),
  getKnowledgeBaseDocumentContent: (knowledgeBaseId: string, documentId: string) => requestText(`/api/v1/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/documents/${encodeURIComponent(documentId)}/content`),
  getKnowledgeBaseDocumentTree: (knowledgeBaseId: string, documentId: string) => request<DocumentIndexTree>(`/api/v1/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/documents/${encodeURIComponent(documentId)}/tree`),
  rebuildFts: (knowledgeBaseId: string) => request<KnowledgeBase>(`/api/v1/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/fts`, { method: "POST" }),
  rebuildVector: (knowledgeBaseId: string) => request<KnowledgeBase>(`/api/v1/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/vector`, { method: "POST" }),
  chat: (applicationId: string, message: string, conversationId: string, clarificationEnabled: boolean, signal?: AbortSignal) => fetch(`${API_BASE}/api/v1/apps/${encodeURIComponent(applicationId)}/chat`, { method: "POST", signal, headers: { Accept: "text/event-stream", "Content-Type": "application/json" }, body: JSON.stringify({ message, conversation_id: conversationId, response_mode: "streaming", clarification_enabled: clarificationEnabled }) }),
  docsUrl: () => `${API_BASE}/docs`,
};
