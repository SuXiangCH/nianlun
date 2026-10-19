// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApplicationDetail } from "./ApplicationDetail";
import { VersionSettings } from "./ApplicationVersions";
import type { Application, KnowledgeBase, ModelProfile, PublishedApplicationConfig } from "../../types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const app: Application = { id: "app", name: "服务助手", description: "", knowledge_base_id: "kb", llm_model_id: "llm", provider: "default", agent_version: "v0", search_mode: "fts", config_version: 1, clarification_enabled: true, created_at: "2026-10-19T00:00:00Z", updated_at: "2026-10-19T00:00:00Z" };
const config: Partial<PublishedApplicationConfig> = { agent_version: "v0", model: "chat-model", llm_model_id: "llm", knowledge_base_id: "kb", clarification_enabled: true, context_window_tokens: 128000, base_url: "https://model.test/v1", knowledge_update_policy: "latest_committed" };
const version = { id: "r1", application_id: "app", sequence: 1, draft_config_version: 1, config_schema_version: 1, config_hash: "hash", description: "首个稳定版本", config, created_at: app.created_at };

describe("Agent version workflow", () => {
  let container: HTMLDivElement;
  let root: Root;
  let requests: Array<{ path: string; method: string; body: Record<string, unknown> }>;
  let releases: typeof version[];
  let channelCreated: boolean;
  let activeRelease: typeof version | null;
  let deploymentVersion: number;
  let deployFailure: boolean;
  let draftReady: boolean;
  let serverApp: Application;
  let serverModels: Array<Partial<ModelProfile>>;
  let onUpdated: ReturnType<typeof vi.fn<(app: Application) => void>>;

  beforeEach(() => {
    container = document.createElement("div"); document.body.appendChild(container); root = createRoot(container);
    requests = []; releases = []; channelCreated = true; activeRelease = null; deploymentVersion = 0; deployFailure = false; draftReady = true; serverApp = { ...app }; serverModels = []; onUpdated = vi.fn();
    vi.stubGlobal("fetch", vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input); const path = url.pathname; const method = init?.method || "GET";
      const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {};
      requests.push({ path, method, body });
      const publication = () => ({ application_id: app.id, api_channel_created: channelCreated, enabled: Boolean(activeRelease), active_release: activeRelease, config_version: serverApp.config_version, deployment_version: deploymentVersion, has_unpublished_changes: true, dependencies_ready: true, dependency_error: null, draft_ready: draftReady, draft_error: draftReady ? null : "草稿模型不可用" });
      let data: unknown; let status = 200;
      if (path.endsWith("/publication")) data = publication();
      else if (path.endsWith("/releases") && method === "POST") {
        if (body.expected_config_version !== serverApp.config_version) { status = 409; data = null; }
        else { const created = { ...version, description: String(body.description || "") }; releases = [created]; data = created; }
      }
      else if (path.endsWith("/releases")) { const offset = Number(url.searchParams.get("offset") || 0); data = { items: releases.slice(offset, offset + 20), total: releases.length }; }
      else if (path.endsWith("/deployments") && method === "POST") {
        if (deployFailure) { status = 409; data = null; }
        else { activeRelease = releases.find((item) => item.id === body.release_id) || null; deploymentVersion++; data = publication(); }
      } else if (path.endsWith("/deployments")) data = { items: [], total: 0 };
      else if (path.endsWith("/restore")) data = { ...app, config_version: 2 };
      else if (path === "/api/v1/apps/app") data = serverApp;
      else if (path === "/api/v1/models") data = serverModels;
      else data = version;
      return new Response(JSON.stringify({ code: status, message: status === 409 ? "渠道已变化，请刷新后重试" : "ok", data }), { status });
    }));
  });
  afterEach(() => { act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); });
  const render = async () => { await act(async () => root.render(<ApplicationDetail app={app} knowledgeBases={[]} llmModels={[]} onBack={vi.fn()} onUse={vi.fn()} onUpdated={onUpdated} />)); };
  const click = async (name: string) => {
    const button = Array.from(container.querySelectorAll<HTMLButtonElement>("button")).find((item) => item.getAttribute("aria-label") === name || item.textContent?.trim() === name);
    expect(button, name).toBeTruthy(); await act(async () => button!.click());
  };
  const change = async (selector: string, value: string) => {
    const input = container.querySelector<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>(selector)!;
    expect(input).toBeTruthy();
    await act(async () => {
      Object.getOwnPropertyDescriptor(Object.getPrototypeOf(input), "value")!.set!.call(input, value);
      input.dispatchEvent(new Event(input instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
    });
  };

  it("shows the saved draft before any version exists and opens it for editing", async () => {
    channelCreated = false; await render(); await click("版本管理");
    const draft = container.querySelector(".application-version-draft");
    expect(draft?.textContent).toContain("当前草稿");
    expect(draft?.textContent).toContain("已保存");
    expect(container.textContent).toContain("0 个版本");
    expect(container.textContent).toContain("尚未创建版本");
    expect(container.textContent).not.toContain("暂无版本");
    expect(container.querySelector(".application-version-table")).toBeNull();
    expect(requests.some((item) => item.method === "POST")).toBe(false);
    await click("编辑草稿");
    expect(container.querySelector("#application-tab-draft")?.getAttribute("aria-selected")).toBe("true");
  });

  it("marks unsaved draft edits in version management and keeps them when returning to edit", async () => {
    await render(); await click("Agent 配置"); await change("#draft-name", "更新后的助手"); await click("版本管理");
    expect(container.querySelector(".application-version-draft")?.textContent).toContain("有未保存修改");
    expect(container.querySelector(".application-unsaved")).toBeNull();
    const create = Array.from(container.querySelectorAll<HTMLButtonElement>("button")).find((item) => item.textContent?.trim() === "创建版本");
    expect(create?.disabled).toBe(true);
    expect(create?.title).toBe("请先保存草稿");
    await click("编辑草稿");
    expect(container.querySelector<HTMLInputElement>("#draft-name")?.value).toBe("更新后的助手");
  });

  it("creates a documented version without creating or deploying a channel", async () => {
    channelCreated = false; await render(); await click("版本管理"); await click("创建版本");
    await change("[aria-label='版本说明']", "完成知识检索优化"); await click("确认创建");
    expect(container.textContent).toContain("完成知识检索优化");
    expect(container.querySelector(".application-version-draft")?.textContent).toContain("已保存");
    expect(requests.find((item) => item.method === "POST" && item.path.endsWith("/releases"))?.body).toEqual({ expected_config_version: 1, description: "完成知识检索优化" });
    expect(requests.some((item) => item.method === "POST" && item.path.endsWith("/deployments"))).toBe(false);
  });

  it("publishes an existing version even when the draft is unavailable", async () => {
    draftReady = false; releases = [version]; await render(); await click("版本管理"); await click("发布 r1");
    expect(container.querySelector("[role=dialog]")?.textContent).toContain("首个稳定版本");
    await click("确认发布");
    expect(requests.find((item) => item.method === "POST" && item.path.endsWith("/deployments"))?.body).toEqual({ channel_type: "api", release_id: "r1", expected_deployment_version: 0 });
    expect(requests.some((item) => item.method === "POST" && item.path.endsWith("/releases"))).toBe(false);
    expect(container.querySelector("[role=dialog]")).toBeNull();
  });

  it("retains a created version after deployment failure and reuses it on retry", async () => {
    deployFailure = true; await render(); await click("发布"); await click("确认发布");
    expect(container.querySelector("[role=dialog] [role=alert]")?.textContent).toContain("渠道已变化");
    expect(container.querySelector("[role=dialog]")?.textContent).toContain("r1");
    deployFailure = false; await click("刷新发布状态"); await click("确认发布");
    expect(requests.filter((item) => item.method === "POST" && item.path.endsWith("/releases"))).toHaveLength(1);
    expect(requests.filter((item) => item.method === "POST" && item.path.endsWith("/deployments"))).toHaveLength(2);
    expect(container.querySelector("[role=dialog]")).toBeNull();
  });

  it("refreshes the draft and preview before retrying a draft-version conflict", async () => {
    await render(); await click("发布");
    serverApp = { ...app, config_version: 2, agent_version: "v1", updated_at: "2026-10-20T00:00:00Z" };
    serverModels = [{ id: "llm", kind: "llm", model: "refreshed-model", base_url: "https://new-model.test/v1", context_window_tokens: 32000 }];
    await click("确认发布");
    expect(container.querySelector("[role=dialog] [role=alert]")).not.toBeNull();
    await click("刷新发布状态");
    expect(container.querySelector(".application-deploy-summary")?.textContent).toContain("v1 · 检索与回答分离");
    expect(container.querySelector(".application-deploy-summary")?.textContent).toContain("refreshed-model");
    expect(container.querySelector(".application-deploy-summary")?.textContent).toContain("https://new-model.test/v1");
    await click("取消"); await click("发布");
    expect(container.querySelector(".application-deploy-summary")?.textContent).toContain("refreshed-model");
    await click("确认发布");
    expect(requests.filter((item) => item.method === "POST" && item.path.endsWith("/releases")).map((item) => item.body.expected_config_version)).toEqual([1, 2]);
    expect(onUpdated).toHaveBeenCalledWith(expect.objectContaining({ config_version: 2 }));
    expect(container.querySelector("[role=dialog]")).toBeNull();
    await click("Agent 配置");
    expect(container.querySelector<HTMLSelectElement>("#draft-version")?.value).toBe("v1");
  });

  it("keeps unsaved draft edits when refreshing a historical-version deployment", async () => {
    releases = [version]; deployFailure = true;
    await render(); await click("Agent 配置"); await change("#draft-name", "尚未保存的名称");
    await click("发布"); await change("#publish-source", "r1"); await click("确认发布");
    serverApp = { ...app, name: "其他页面保存的名称", config_version: 2 };
    await click("刷新发布状态"); await click("取消");
    expect(container.querySelector<HTMLInputElement>("#draft-name")?.value).toBe("尚未保存的名称");
    expect(container.textContent).toContain("有未保存的修改");
    expect(onUpdated).not.toHaveBeenCalled();
  });

  it("detects binding changes even when model settings and knowledge-base names match", () => {
    const knowledge = (id: string): KnowledgeBase => ({
      id, name: "产品文档", description: "", status: "ready", document_count: 0,
      summary_enabled: true, heading_recovery_enabled: true, llm_model_id: null,
      workspace_dir: id, workspace_relpath: id, content_version: 0,
      fts_status: "disabled", fts_revision: null, fts_target_revision: null, fts_collection: null, fts_error: null,
      vector_status: "disabled", vector_enabled: false, vector_revision: null, vector_target_revision: null,
      vector_collection: null, vector_error: null, embedding_model_id: null, vector_model_id: null,
      vector_model_updated_at: null, vector_dimension: null, vector_progress_stage: null,
      vector_documents_total: null, vector_documents_completed: null, vector_records_processed: null,
      created_at: "", updated_at: "",
    });
    act(() => root.render(<VersionSettings config={{ ...version.config, llm_model_id: "llm-2", knowledge_base_id: "kb-2" }} previous={version.config} knowledgeBases={[knowledge("kb"), knowledge("kb-2")]} />));
    const changed = Array.from(container.querySelectorAll(".is-changed"));
    expect(changed.map((row) => row.querySelector("dt")?.textContent)).toEqual(["LLM 模型变更", "知识库变更"]);
    expect(changed[0]?.textContent).toContain("llm-2");
    expect(changed[1]?.textContent).toContain("kb-2");
  });

  it("compares human readable settings and confirms copying into the draft", async () => {
    releases = [version, { ...version, id: "r2", sequence: 2, config: { ...version.config, clarification_enabled: false }, description: "关闭澄清" }];
    activeRelease = releases[1]; await render(); await click("版本管理"); await click("查看 r1");
    expect(container.querySelector("[role=dialog]")?.textContent).toContain("允许澄清问题");
    expect(container.querySelector("[role=dialog]")?.textContent).toContain("变更");
    await click("复制到草稿");
    expect(requests.some((item) => item.path.endsWith("/restore"))).toBe(false);
    await click("确认覆盖草稿");
    expect(requests.find((item) => item.path.endsWith("/restore"))?.body).toEqual({ expected_config_version: 1 });
    expect(onUpdated).toHaveBeenCalledWith(expect.objectContaining({ config_version: 2 }));
  });

  it("blocks draft publication with unsaved edits while allowing an existing version", async () => {
    releases = [version]; await render(); await click("Agent 配置"); await change("#draft-name", "新名称"); await click("发布");
    expect(container.querySelector("[role=dialog]")?.textContent).toContain("请先保存草稿");
    expect(container.querySelector<HTMLButtonElement>("[aria-label='确认发布']")?.disabled).toBe(true);
    await change("#publish-source", "r1"); await click("确认发布");
    expect(requests.filter((item) => item.method === "POST" && item.path.endsWith("/releases"))).toHaveLength(0);
    expect(requests.some((item) => item.method === "POST" && item.path.endsWith("/deployments"))).toBe(true);
  });

  it("loads historical versions beyond the first page before deploying them", async () => {
    releases = Array.from({ length: 21 }, (_, index) => ({ ...version, id: `r${21 - index}`, sequence: 21 - index }));
    await render(); await click("发布");
    expect(container.querySelector("#publish-source option[value=r1]")).toBeNull();
    await click("加载更多版本"); await change("#publish-source", "r1"); await click("确认发布");
    expect(requests.some((item) => item.path.endsWith("/deployments") && item.body.release_id === "r1")).toBe(true);
  });

  it("shows deployment history separately from immutable versions", async () => {
    await render(); await click("管理 API"); await click("发布记录");
    expect(requests.some((item) => item.path.endsWith("/deployments") && item.method === "GET")).toBe(true);
    expect(container.textContent).toContain("暂无发布记录");
    expect(requests.some((item) => item.path.endsWith("/releases"))).toBe(false);
  });
});
