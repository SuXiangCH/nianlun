// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApplicationView } from "./ApplicationView";
import type { Application } from "../../types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const application: Application = { id: "app-1", name: "产品助手", description: "", knowledge_base_id: "kb-1", llm_model_id: "llm-1", provider: "default", agent_version: "v0", search_mode: "fts", created_at: "2026-10-15T00:00:00Z", updated_at: "2026-10-15T00:00:00Z" };
const release = { id: "release-1", application_id: "app-1", sequence: 1, draft_config_version: 1, description: "", config_schema_version: 1, config_hash: "hash", config: {}, created_at: application.created_at };
const previousRelease = { ...release, id: "release-0", sequence: 0 };
const wecomChannel = { id: "wecom-1", bot_id: "bot-1", has_secret: true, default_response: "稍后再试", enabled: false, active_release: null as typeof release | null, deployment_version: 0, config_revision: 1, status: "offline" as "offline" | "online" | "error", error_code: null as "AUTH_FAILED" | "NETWORK_ERROR" | "CONNECTION_CONFLICT" | null, expires_at: null as string | null, has_unpublished_changes: false, dependencies_ready: true, dependency_error: null as string | null, last_deployed_at: null as string | null, last_reported_at: null as string | null };
const publication = { application_id: "app-1", api_channel_created: true, enabled: false, active_release: null, deployment_version: 0, config_version: 1, has_unpublished_changes: true, dependencies_ready: true, dependency_error: null as string | null, draft_ready: true, draft_error: null as string | null, wecom_channel: null as typeof wecomChannel | null };
const client = { id: "client-1", application_id: "app-1", name: "业务后台", enabled: true, created_at: application.created_at, updated_at: application.updated_at };
const existingKey = { key_id: "existing-key", client_id: client.id, name: "生产 Key", expires_at: null, revoked_at: null, created_at: application.created_at };

describe("Application publishing", () => {
  let container: HTMLDivElement;
  let root: Root;
  let requests: Array<{ path: string; query: string; method: string; body: Record<string, unknown>; signal?: AbortSignal | null }>;
  let keyFailure: boolean;
  let publishFailure: boolean;
  let channelFailure: boolean;
  let keyGate: Promise<void> | null;
  let draftVersion: number;
  let publicationGate: Promise<void> | null;
  let publicationFailure: boolean;
  let withExistingKey: boolean;
  let unknownMetrics: boolean;
  let appUpdates: Application[];
  let currentPublication: Omit<typeof publication, "active_release"> & { active_release: typeof release | null };
  beforeEach(() => {
    container = document.createElement("div"); document.body.appendChild(container); root = createRoot(container);
    requests = []; appUpdates = []; keyFailure = false; publishFailure = false; channelFailure = false; keyGate = null; publicationGate = null; publicationFailure = false; draftVersion = 1; withExistingKey = false; unknownMetrics = false; currentPublication = { ...publication };
    vi.stubGlobal("fetch", vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input); const path = url.pathname; const method = init?.method || "GET";
      const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {};
      requests.push({ path, query: url.search, method, body, signal: init?.signal });
      let data: unknown = null; let status = 200;
      if (path.endsWith("/channels")) {
        if (channelFailure) status = 409;
        else if (body.type === "wecom") currentPublication = { ...currentPublication, wecom_channel: { ...wecomChannel, bot_id: String(body.bot_id || "bot-1"), enabled: false } };
        else currentPublication = { ...currentPublication, api_channel_created: true };
        data = currentPublication;
      } else if (path.endsWith("/channels/wecom") && method === "PUT") {
        if (channelFailure) status = 409;
        else currentPublication = { ...currentPublication, wecom_channel: { ...currentPublication.wecom_channel!, bot_id: String(body.bot_id ?? currentPublication.wecom_channel?.bot_id ?? "bot-1"), default_response: String(body.default_response ?? currentPublication.wecom_channel?.default_response ?? ""), expires_at: body.expires_at === undefined ? currentPublication.wecom_channel?.expires_at ?? null : body.expires_at as string | null, config_revision: (currentPublication.wecom_channel?.config_revision ?? 0) + 1 } };
        data = currentPublication;
      } else if (path.endsWith("/channels/wecom") && method === "PATCH") {
        currentPublication = { ...currentPublication, wecom_channel: { ...currentPublication.wecom_channel!, enabled: Boolean(body.enabled), deployment_version: (currentPublication.wecom_channel?.deployment_version ?? 0) + 1 } };
        data = currentPublication;
      } else if (path.endsWith("/channels/wecom/connect")) {
        data = { channel_id: "wecom-1", revision: 1, status: "online", error_code: null };
      } else if (path.endsWith("/releases") && method === "POST") data = release;
      else if (path.endsWith("/deployments") && method === "POST") {
        if (publishFailure) { status = 409; } else if (body.channel_type === "wecom") currentPublication = { ...currentPublication, wecom_channel: { ...currentPublication.wecom_channel!, enabled: true, deployment_version: (currentPublication.wecom_channel?.deployment_version ?? 0) + 1, active_release: release } };
        else currentPublication = { ...currentPublication, enabled: true, deployment_version: 1, active_release: release, has_unpublished_changes: false };
        data = currentPublication;
      } else if (path.endsWith("/publication")) {
        if (method === "GET" && publicationGate) await publicationGate;
        if (method === "GET" && publicationFailure) status = 503;
        if (method === "PATCH") currentPublication = { ...currentPublication, enabled: Boolean(body.enabled), deployment_version: currentPublication.deployment_version + 1 };
        data = currentPublication;
      } else if (path.endsWith("/releases")) { const items = Array.from(new Map([currentPublication.active_release, currentPublication.wecom_channel?.active_release].filter((item): item is typeof release => Boolean(item)).map((item) => [item.id, item])).values()); data = { items, total: items.length }; }
      else if (path.endsWith("/deployments")) data = { items: [], total: 0 };
      else if (path.endsWith("/api-clients")) data = [client];
      else if (path.endsWith("/keys")) {
        if (method === "POST") { if (keyGate) await keyGate; status = keyFailure ? 409 : 200; data = { key_id: "registered", client_id: client.id, name: body.name, expires_at: null, revoked_at: null, created_at: application.created_at }; }
        else data = withExistingKey ? [existingKey] : [];
      } else if (path.endsWith("/runs")) data = { items: [{ run_id: url.searchParams.get("offset") === "20" ? "run-second-page" : "run-first-page", app_id: application.id, release_id: release.id, client_id: unknownMetrics ? "removed-client" : client.id, knowledge_base_content_version: 7, request_id: unknownMetrics ? null : "request-123", duration_ms: unknownMetrics ? null : 1250, usage: unknownMetrics ? null : { input_tokens: 100, output_tokens: 30, total_tokens: 130 }, conversation_id: "conversation-1", status: "completed", error_type: null, error_status: null, error_message: null, result: null, created_at: application.created_at, updated_at: application.updated_at }], total: 21 };
      else if (method === "DELETE") data = { ...existingKey, revoked_at: application.updated_at };
      else if (method === "PATCH") { if (body.expected_config_version !== draftVersion) status = 409; else { const { search_mode: _searchMode, ...serverApp } = application; data = { ...serverApp, ...body, config_version: ++draftVersion }; currentPublication = { ...currentPublication, config_version: draftVersion, has_unpublished_changes: true }; } }
      else if (path.endsWith("/app-1")) data = { ...application, config_version: draftVersion };
      return new Response(JSON.stringify({ code: status, message: status === 409 ? "配置已变化，请刷新后重试" : status === 503 ? "发布状态暂时不可用" : "ok", data }), { status });
    }));
  });
  afterEach(() => { act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); vi.useRealTimers(); });
  const click = async (label: string) => {
    const button = Array.from(container.querySelectorAll("button")).find((item) => item.getAttribute("aria-label") === label || item.textContent?.trim() === label);
    expect(button, `button ${label}`).toBeTruthy();
    await act(async () => { button!.click(); });
  };
  const open = async (apiDetail = true) => {
    await act(async () => { root.render(<ApplicationView apps={[application]} knowledgeBases={[]} llmModels={[]} selectedAppId="app-1" onCreate={vi.fn()} onUse={vi.fn()} onDelete={vi.fn()} onKnowledge={vi.fn()} onModels={vi.fn()} onUpdated={(item) => appUpdates.push(item)} />); });
    await click("产品助手");
    if (apiDetail) await click("管理 API");
  };
  const publish = async () => { await click("发布"); await click("确认发布"); };
  const submit = async () => { await act(async () => { container.querySelector("[role=dialog] form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); }); };
  const button = (label: string) => Array.from(container.querySelectorAll<HTMLButtonElement>("button")).find((item) => item.getAttribute("aria-label") === label || item.textContent?.trim() === label);
  const change = async (select: HTMLSelectElement, value: string) => {
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(select, value);
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
  };
  const fill = (input: HTMLInputElement | HTMLTextAreaElement, value: string) => {
    const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  };

  it("groups list search, count and creation in one toolbar without a duplicate heading", async () => {
    await open(false); await click("全部 Agent");
    const toolbar = container.querySelector(".application-list-toolbar");
    const navigation = toolbar?.querySelector("nav[aria-label='Agent 功能导航']");
    expect(navigation?.querySelector("a[aria-current='page']")?.textContent).toBe("Agent");
    expect(navigation?.querySelector("a")?.getAttribute("href")).toBe("#applications");
    expect(toolbar?.querySelector("[aria-label='搜索 Agent']")).toBeTruthy();
    expect(toolbar?.querySelector("button")?.textContent).toContain("新建 Agent");
    expect(toolbar?.querySelector("[role=status]")?.textContent).toBe("1 个 Agent");
    expect(container.querySelector("h2")).toBeNull();

    await act(async () => {
      const input = container.querySelector<HTMLInputElement>("[aria-label='搜索 Agent']")!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "missing");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(toolbar?.querySelector("[role=status]")?.textContent).toBe("0 个 Agent");
    expect(container.textContent).toContain("没有匹配的 Agent");
  });

  it("keeps the compact toolbar available when there are no Agents", async () => {
    await act(async () => { root.render(<ApplicationView apps={[]} knowledgeBases={[]} llmModels={[]} selectedAppId="" onCreate={vi.fn()} onUse={vi.fn()} onDelete={vi.fn()} onKnowledge={vi.fn()} onModels={vi.fn()} />); });
    expect(container.querySelector(".application-list-toolbar [role=status]")?.textContent).toBe("0 个 Agent");
    expect(container.querySelector<HTMLInputElement>("[aria-label='搜索 Agent']")?.disabled).toBe(true);
    expect(container.querySelector<HTMLButtonElement>("[role=search] button")?.disabled).toBe(true);
    expect(container.textContent).toContain("还没有 Agent");
  });

  it("publishes the viewed versions and stops the active entry", async () => {
    await open(); await publish();
    expect(requests.find((item) => item.path.endsWith("/deployments"))?.body).toEqual({ channel_type: "api", release_id: release.id, expected_deployment_version: 0 });
    expect(container.textContent).toContain("已发布");
    await click("停用 API");
    expect(requests.find((item) => item.method === "PATCH")?.body).toEqual({ enabled: false, expected_deployment_version: 1 });
    expect(container.textContent).toContain("已停用");
    await click("恢复 API");
    expect(container.textContent).toContain("已发布");
  });

  it("keeps failed publication visibly unpublished with its error", async () => {
    publishFailure = true; await open(); await publish();
    expect(container.querySelector("[role=alert]")?.textContent).toContain("配置已变化");
    expect(container.textContent).toContain("未发布");
    expect(container.textContent).not.toContain("停用 API");
  });

  it("shows a usable browser generated key only after registration and clears it on close", async () => {
    await open(); await click("客户端与密钥"); await click("新增 Key");
    expect(container.textContent).not.toContain("nl_app_");
    await submit();
    const written = requests.find((item) => item.method === "POST" && item.path.endsWith("/keys"))?.body.api_key;
    expect(written).toMatch(/^nl_app_[A-Za-z0-9_-]{16,64}\.[A-Za-z0-9_-]{43}$/);
    expect(container.querySelector("[aria-label='已注册的 API Key']")?.textContent).toBe(written);
    expect(localStorage.getItem("nianlun.apiKey")).toBeNull();
    await click("完成");
    expect(container.textContent).not.toContain(String(written));
    await click("新增 Key"); expect(container.textContent).not.toContain(String(written));
  });

  it("does not expose an unregistered key after registration fails", async () => {
    keyFailure = true; await open(); await click("客户端与密钥"); await click("新增 Key"); await submit();
    expect(container.querySelector("[role=dialog] [role=alert]")?.textContent).toContain("配置已变化");
    expect(container.querySelector("[aria-label='已注册的 API Key']")).toBeNull();
    expect(container.textContent).not.toContain("nl_app_");
  });

  it("does not reveal a completed registration after its dialog has been closed", async () => {
    let resolve!: () => void;
    keyGate = new Promise<void>((done) => { resolve = done; });
    await open(); await click("客户端与密钥"); await click("新增 Key"); await submit();
    expect(container.querySelector<HTMLButtonElement>("button[type=submit]:disabled")).toBeTruthy();
    await click("取消");
    await act(async () => { resolve(); await keyGate; });
    expect(container.querySelector("[role=dialog]")).toBeNull();
    expect(container.textContent).not.toContain("nl_app_");
  });

  it("refreshes a conflicting draft before submitting its next edit", async () => {
    await open(); await click("Agent 配置");
    await act(async () => { container.querySelector<HTMLInputElement>("[name=clarification_enabled]")!.click(); });
    draftVersion = 3;
    await act(async () => { container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    expect(container.querySelector("[role=alert]")?.textContent).toContain("配置已变化");
    expect(container.querySelector<HTMLButtonElement>(".application-form-actions button")?.disabled).toBe(false);
    await click("刷新草稿");
    await act(async () => { container.querySelector<HTMLInputElement>("[name=clarification_enabled]")!.click(); });
    await act(async () => { container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    const edits = requests.filter((item) => item.method === "PATCH" && item.path.endsWith("/app-1"));
    expect(edits[1].body.expected_config_version).toBe(3);
    expect(container.textContent).toContain("草稿已保存");
    expect(container.querySelector<HTMLButtonElement>(".application-form-actions button")?.disabled).toBe(true);
    expect(appUpdates.at(-1)?.search_mode).toBe("fts");
  });

  it("loads subsequent call pages and resets pagination when filters change", async () => {
    await open(); await click("运行日志");
    expect(container.textContent).toContain("run-first-page");
    await act(async () => { container.querySelector<HTMLButtonElement>("[aria-label='下一页']")!.click(); });
    expect(container.textContent).toContain("run-second-page");
    await act(async () => { const select = container.querySelector<HTMLSelectElement>("#run-status")!; select.value = "failed"; select.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(container.textContent).toContain("run-first-page");
    expect(container.querySelector<HTMLButtonElement>("[aria-label='上一页']")?.disabled).toBe(true);
  });

  it("keeps publication actions unavailable while loading or dependencies are unavailable", async () => {
    let resolve!: () => void;
    publicationGate = new Promise<void>((done) => { resolve = done; });
    currentPublication = { ...currentPublication, draft_ready: false };
    await open(false);
    expect(container.querySelector("[aria-busy=true]")).toBeTruthy();
    expect(container.textContent).toContain("正在加载");
    expect(container.querySelector<HTMLButtonElement>("[aria-label='发布']")?.disabled).toBe(true);
    await act(async () => { resolve(); await publicationGate; });
    const button = container.querySelector<HTMLButtonElement>("[aria-label='发布']");
    expect(button?.disabled).toBe(false);
    await click("发布");
    expect(container.querySelector<HTMLButtonElement>("[aria-label='确认发布']")?.disabled).toBe(true);
  });

  it("registers a replacement before allowing explicit revocation of the old key", async () => {
    withExistingKey = true; await open(); await click("客户端与密钥"); await click("轮换"); await submit();
    expect(container.querySelector("[aria-label='已注册的 API Key']")).toBeTruthy();
    expect(requests.some((item) => item.method === "DELETE")).toBe(false);
    await click("撤销旧 Key");
    expect(container.querySelector("[role=dialog]")?.textContent).toContain("旧 Key 已撤销");
    await click("完成");
    expect(container.textContent).toContain("已撤销");
  });

  it("allows publishing a valid draft while an incompatible active release cannot resume", async () => {
    currentPublication = { ...currentPublication, active_release: release, dependencies_ready: false, dependency_error: "旧模型配置已变化", draft_ready: true };
    await open();
    const button = container.querySelector<HTMLButtonElement>("[aria-label='发布']");
    expect(button?.disabled).toBe(false);
    const resume = Array.from(container.querySelectorAll("button")).find((item) => item.textContent === "恢复 API");
    expect(resume?.disabled).toBe(true);
    expect(container.textContent).toContain("旧模型配置已变化");
    await publish();
    expect(requests.some((item) => item.path.endsWith("/deployments"))).toBe(true);
  });

  it("allows restoring a valid release while a broken draft cannot publish", async () => {
    currentPublication = { ...currentPublication, active_release: release, dependencies_ready: true, draft_ready: false, draft_error: "草稿模型缺少凭据" };
    await open();
    const publishButton = container.querySelector<HTMLButtonElement>("[aria-label='发布']");
    expect(publishButton?.disabled).toBe(false);
    expect(container.textContent).toContain("草稿模型缺少凭据");
    await click("恢复 API");
    expect(container.textContent).toContain("已发布");
  });

  it("shows call ownership, content revision, duration, usage, and request correlation", async () => {
    await open(); await click("运行日志");
    const row = container.querySelector("tbody tr")!;
    expect(row.textContent).toContain("业务后台");
    expect(row.textContent).toContain("v7");
    expect(row.textContent).toContain("1.25 s");
    expect(row.textContent).toContain("130 tokens");
    expect(row.textContent).toContain("request-123");
    expect(row.textContent).toContain("100");
    expect(row.textContent).toContain("30");
  });

  it("keeps unknown metrics distinct from zero and falls back to the client id", async () => {
    unknownMetrics = true; await open(); await click("运行日志");
    const row = container.querySelector("tbody tr")!;
    expect(row.textContent).toContain("removed-client");
    expect(row.textContent).toContain("用量未知");
    expect(row.querySelector(".application-run-metrics")?.textContent).toContain("—");
    expect(row.textContent).not.toContain("0 tokens");
  });

  it("lists only created channels and separates API settings from release history", async () => {
    await open(false);
    expect(Array.from(container.querySelectorAll("[role=tab]")).map((item) => item.textContent)).toEqual(["Agent 配置", "版本管理", "发布渠道", "会话", "运行日志"]);
    expect(container.textContent).not.toContain("企业微信");
    expect(container.textContent).not.toContain("个人微信");
    expect(container.textContent).not.toContain("调用地址");
    expect(requests.some((item) => item.path.endsWith("/releases"))).toBe(false);
    await click("管理 API");
    expect(container.textContent).toContain("调用地址");
    await click("返回发布渠道");
    expect(container.textContent).toContain("API 服务");
    await click("版本管理");
    expect(requests.some((item) => item.path.endsWith("/releases"))).toBe(true);
    expect(container.textContent).toContain("尚未创建版本");
  });

  it("creates an API channel only after choosing a supported type", async () => {
    currentPublication = { ...currentPublication, api_channel_created: false };
    await open(false);
    expect(container.textContent).toContain("还没有发布渠道");
    expect(container.textContent).not.toContain("API 运行版本");
    expect(container.textContent).not.toContain("草稿与渠道版本不同");
    expect(container.textContent).not.toContain("管理 API");
    expect(container.querySelector<HTMLButtonElement>("[aria-label='发布']")?.disabled).toBe(true);
    await click("创建发布渠道");
    const create = Array.from(container.querySelectorAll<HTMLButtonElement>("[role=dialog] button")).find((item) => item.textContent === "创建");
    expect(create?.disabled).toBe(true);
    expect(container.querySelectorAll("[role=dialog] input[type=radio]:disabled")).toHaveLength(1);
    await act(async () => { container.querySelector<HTMLInputElement>("input[value=api]")!.click(); });
    await click("创建");
    expect(requests.find((item) => item.path.endsWith("/channels"))?.body).toEqual({ type: "api" });
    expect(container.textContent).toContain("管理 API");
    expect(container.textContent).toContain("API 运行版本");
    expect(container.querySelector(".application-release-strip")?.textContent).toContain("未发布");
    expect(container.textContent).not.toContain("草稿与渠道版本不同");
    expect(container.textContent).not.toContain("企业微信");
    expect(requests.some((item) => item.path.endsWith("/publish"))).toBe(false);
    expect(container.querySelector("[role=dialog]")).toBeNull();
  });

  it("allows adding the WeCom channel while the API channel exists", async () => {
    currentPublication = { ...currentPublication, api_channel_created: true };
    await open(false);
    const create = Array.from(container.querySelectorAll<HTMLButtonElement>("button")).find((item) => item.textContent?.trim() === "创建发布渠道");
    expect(create?.disabled).toBe(false);
    await click("创建发布渠道");
    expect(container.querySelector<HTMLInputElement>("input[value=api]")?.disabled).toBe(true);
    expect(container.querySelector<HTMLInputElement>("input[value=wecom]")?.disabled).toBe(false);
  });

  it("creates a WeCom channel with bot credentials and never echoes the secret", async () => {
    currentPublication = { ...publication, api_channel_created: false };
    await open(false);
    await click("创建发布渠道");
    await act(async () => { container.querySelector<HTMLInputElement>("input[value=wecom]")!.click(); });
    expect(container.querySelector<HTMLInputElement>("#wecom-secret")?.value).toBe("");
    fill(container.querySelector<HTMLInputElement>("#wecom-bot-id")!, "bot-corp-1");
    fill(container.querySelector<HTMLInputElement>("#wecom-secret")!, "super-secret-value");
    await click("创建");
    expect(requests.find((item) => item.path.endsWith("/channels"))?.body).toEqual({ type: "wecom", bot_id: "bot-corp-1", secret: "super-secret-value", default_response: "暂时无法回答，请稍后再试。", expires_at: null });
    expect(container.querySelector("[role=dialog]")).toBeNull();
    expect(container.textContent).toContain("企业微信");
    expect(container.textContent).not.toContain("super-secret-value");
  });

  it("shows a fresh WeCom connection as online and a stale one as unknown", async () => {
    currentPublication = { ...publication, api_channel_created: false, wecom_channel: { ...wecomChannel, enabled: true, active_release: release, status: "online", last_reported_at: new Date().toISOString() } };
    await open(false);
    expect(container.textContent).toContain("在线");
    act(() => root.unmount());
    root = createRoot(container);
    currentPublication = { ...publication, api_channel_created: false, wecom_channel: { ...wecomChannel, enabled: true, active_release: release, status: "online", last_reported_at: new Date(Date.now() - 120_000).toISOString() } };
    await act(async () => { root.render(<ApplicationView apps={[application]} knowledgeBases={[]} llmModels={[]} selectedAppId="app-1" onCreate={vi.fn()} onUse={vi.fn()} onDelete={vi.fn()} onKnowledge={vi.fn()} onModels={vi.fn()} />); });
    await click("产品助手");
    expect(container.textContent).toContain("状态未知");
  });

  it("refreshes a connecting WeCom channel without overlapping requests or resetting an open form", async () => {
    vi.useFakeTimers();
    currentPublication = { ...publication, api_channel_created: false, wecom_channel: { ...wecomChannel, enabled: true, active_release: release, last_reported_at: new Date().toISOString() } };
    await open(false);
    const publicationRequests = () => requests.filter((item) => item.path.endsWith("/publication") && item.method === "GET");
    let resolve!: () => void;
    publicationGate = new Promise<void>((done) => { resolve = done; });
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(publicationRequests()).toHaveLength(3);
    expect(container.querySelector("[aria-busy=true]")).toBeNull();
    expect(button("配置企业微信")?.disabled).toBe(false);
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(publicationRequests()).toHaveLength(3);
    currentPublication = { ...currentPublication, wecom_channel: { ...currentPublication.wecom_channel!, status: "online", last_reported_at: new Date().toISOString() } };
    await act(async () => { resolve(); });
    expect(container.textContent).toContain("在线");
    await click("配置企业微信");
    fill(container.querySelector<HTMLInputElement>("#wecom-bot-id")!, "unsaved-bot");
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(container.querySelector<HTMLInputElement>("#wecom-bot-id")?.value).toBe("unsaved-bot");
    expect(publicationRequests()).toHaveLength(3);
  });

  it("reports background refresh failures and recovers on the next successful refresh", async () => {
    vi.useFakeTimers();
    currentPublication = { ...publication, api_channel_created: false, wecom_channel: { ...wecomChannel, enabled: true, active_release: release, last_reported_at: new Date().toISOString() } };
    await open(false);
    publicationFailure = true;
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(container.querySelector("[role=alert]")?.textContent).toContain("发布状态暂时不可用");
    expect(container.textContent).toContain("离线");
    publicationFailure = false;
    currentPublication = { ...currentPublication, wecom_channel: { ...currentPublication.wecom_channel!, status: "online", last_reported_at: new Date().toISOString() } };
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(container.querySelector("[role=alert]")).toBeNull();
    expect(container.textContent).toContain("在线");
  });

  it("updates idle WeCom expiry controls while a refresh is pending and cancels it on navigation", async () => {
    vi.useFakeTimers();
    const now = Date.now();
    currentPublication = { ...publication, api_channel_created: false, wecom_channel: { ...wecomChannel, enabled: true, active_release: release, status: "online", expires_at: new Date(now + 10_000).toISOString(), last_reported_at: new Date(now).toISOString() } };
    await open(false);
    let resolve!: () => void;
    publicationGate = new Promise<void>((done) => { resolve = done; });
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(container.textContent).toContain("已过期");
    expect(button("连接企业微信")?.disabled).toBe(true);
    expect(button("切换到企业微信版本")?.disabled).toBe(true);
    const pending = requests.filter((item) => item.path.endsWith("/publication")).at(-1)!;
    await click("Agent 配置");
    expect(pending.signal?.aborted).toBe(true);
    await act(async () => { resolve(); });
    const count = requests.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
    expect(requests).toHaveLength(count);
    expect(container.querySelector("[role=alert]")).toBeNull();
  });

  it("blocks an expired WeCom channel from publishing, enabling and reconnecting", async () => {
    currentPublication = { ...publication, api_channel_created: false, wecom_channel: { ...wecomChannel, enabled: false, active_release: release, expires_at: "2020-01-01T00:00:00Z", last_reported_at: new Date().toISOString() } };
    await open(false);
    expect(container.textContent).toContain("已过期");
    expect(button("切换到企业微信版本")?.disabled).toBe(true);
    expect(button("连接企业微信")?.disabled).toBe(true);
    expect(button("启用企业微信")?.disabled).toBe(true);
    await click("发布");
    expect(container.querySelector<HTMLOptionElement>("#publish-channel option[value=wecom]")?.disabled).toBe(true);
    expect(button("确认发布")?.disabled).toBe(true);
    expect(container.querySelector("[role=dialog]")?.textContent).toContain("企业微信渠道已过期");
  });

  it("expires an idle publish dialog opened from Agent configuration", async () => {
    vi.useFakeTimers();
    currentPublication = { ...publication, api_channel_created: false, wecom_channel: { ...wecomChannel, expires_at: new Date(Date.now() + 1_000).toISOString() } };
    await open(false); await click("Agent 配置"); await click("发布");
    expect(button("确认发布")?.disabled).toBe(false);

    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });

    expect(button("确认发布")?.disabled).toBe(true);
    expect(container.querySelector("[role=dialog]")?.textContent).toContain("企业微信渠道已过期");
    await submit();
    expect(requests.filter((item) => item.method === "POST" && /\/(releases|deployments)$/.test(item.path))).toHaveLength(0);
  });

  it("rechecks expiry on submit even before the dialog clock fires", async () => {
    vi.useFakeTimers();
    currentPublication = { ...publication, api_channel_created: false, wecom_channel: { ...wecomChannel, expires_at: new Date(Date.now() + 1_000).toISOString() } };
    await open(false); await click("Agent 配置"); await click("发布");
    vi.setSystemTime(Date.now() + 1_000);

    await submit();

    expect(requests.filter((item) => item.method === "POST" && /\/(releases|deployments)$/.test(item.path))).toHaveLength(0);
    expect(container.querySelector("[role=dialog]")?.textContent).toContain("企业微信渠道已过期");
  });

  it.each([
    { enabled: true, dependencies_ready: true, expires_at: null, label: "企业微信已启用", tone: "is-live" },
    { enabled: false, dependencies_ready: true, expires_at: null, label: "企业微信已停用", tone: "" },
    { enabled: true, dependencies_ready: false, expires_at: null, label: "企业微信依赖异常", tone: "is-error" },
    { enabled: true, dependencies_ready: true, expires_at: "2020-01-01T00:00:00Z", label: "企业微信已过期", tone: "is-error" },
  ])("shows the WeCom-only list publication state: $label", async ({ enabled, dependencies_ready, expires_at, label, tone }) => {
    currentPublication = { ...publication, api_channel_created: false, wecom_channel: { ...wecomChannel, active_release: release, enabled, dependencies_ready, expires_at } };
    await open(false); await click("全部 Agent");
    const status = container.querySelector(".application-list-row:not(.application-list-labels) [role=cell]:nth-child(3)");
    expect(status?.textContent).toBe(label);
    expect(status?.querySelector(".application-state")?.classList.contains("is-live")).toBe(tone === "is-live");
    expect(status?.querySelector(".application-state")?.classList.contains("is-error")).toBe(tone === "is-error");
  });

  it("shows both channel publication states and updates list expiry while idle", async () => {
    vi.useFakeTimers();
    currentPublication = { ...publication, active_release: release, enabled: true, wecom_channel: { ...wecomChannel, active_release: release, enabled: true, expires_at: new Date(Date.now() + 1_000).toISOString() } };
    await open(false); await click("全部 Agent");
    const status = container.querySelector(".application-list-row:not(.application-list-labels) [role=cell]:nth-child(3)");
    expect(status?.textContent).toContain("API 服务中");
    expect(status?.textContent).toContain("企业微信已启用");
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    expect(status?.textContent).toContain("企业微信已过期");
    expect(status?.textContent).toContain("API 服务中");
  });

  it("deploys a release to the selected WeCom channel", async () => {
    currentPublication = { ...publication, api_channel_created: true, active_release: release, wecom_channel: { ...wecomChannel, enabled: true, active_release: previousRelease, deployment_version: 4, last_reported_at: new Date().toISOString() } };
    await open(false);
    await click("发布");
    await change(container.querySelector<HTMLSelectElement>("#publish-channel")!, "wecom");
    await change(container.querySelector<HTMLSelectElement>("#publish-source")!, release.id);
    await click("确认发布");
    expect(requests.find((item) => item.path.endsWith("/deployments"))?.body).toEqual({ channel_type: "wecom", release_id: release.id, expected_deployment_version: 4 });
    expect(container.textContent).toContain("企业微信已切换至 r1");
  });

  it("opens WeCom-only deployment history on the correct channel and returns to its controls", async () => {
    vi.useFakeTimers();
    currentPublication = { ...publication, api_channel_created: false, wecom_channel: { ...wecomChannel, active_release: release } };
    await open(false);
    await click("企业微信发布记录");
    expect(container.querySelector<HTMLSelectElement>("#deployment-channel")?.value).toBe("wecom");
    expect(requests.filter((item) => item.path.endsWith("/deployments")).at(-1)?.query).toContain("channel_type=wecom");
    expect(container.textContent).toContain("暂无发布记录");
    const before = requests.filter((item) => item.path.endsWith("/publication")).length;
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(requests.filter((item) => item.path.endsWith("/publication"))).toHaveLength(before);
    await click("返回发布渠道");
    expect(button("配置企业微信")).toBeTruthy();
    expect(container.querySelector("#deployment-channel")).toBeNull();
    const returned = requests.filter((item) => item.path.endsWith("/publication")).length;
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(requests.filter((item) => item.path.endsWith("/publication"))).toHaveLength(returned + 1);
  });

  it("opens API deployment history from its row and resets the API management history filter", async () => {
    currentPublication = { ...publication, wecom_channel: { ...wecomChannel } };
    await open(false);
    await click("API 发布记录");
    expect(container.querySelector<HTMLSelectElement>("#deployment-channel")?.value).toBe("api");
    await change(container.querySelector<HTMLSelectElement>("#deployment-channel")!, "wecom");
    await click("接入配置");
    await click("发布记录");
    expect(container.querySelector<HTMLSelectElement>("#deployment-channel")?.value).toBe("api");
    expect(requests.filter((item) => item.path.endsWith("/deployments")).at(-1)?.query).toContain("channel_type=api");
  });

  it.each([true, false])("identifies a WeCom-only version's channel and enabled state (%s) in the table and detail", async (enabled) => {
    currentPublication = { ...publication, api_channel_created: false, wecom_channel: { ...wecomChannel, active_release: release, enabled } };
    await open(false); await click("版本管理");
    const row = container.querySelector(".application-version-table tbody tr");
    expect(row?.querySelectorAll("td")[3].textContent).toBe(enabled ? "企业微信" : "企业微信 · 停用");
    expect(button("发布 r1")?.disabled).toBe(true);
    await click("查看 r1");
    expect(container.querySelector(".application-version-meta")?.textContent).toContain("企业微信");
    expect(container.querySelector(".application-version-meta")?.textContent).not.toContain("API");
  });

  it("publishes an API version to the other channel directly from its version row", async () => {
    currentPublication = { ...publication, active_release: release, enabled: true, wecom_channel: { ...wecomChannel, active_release: previousRelease } };
    await open(false); await click("版本管理");
    expect(button("发布 r1")?.disabled).toBe(false);
    await click("发布 r1");
    expect(container.querySelector<HTMLSelectElement>("#publish-channel")?.value).toBe("wecom");
    await click("确认发布");
    expect(requests.find((item) => item.method === "POST" && item.path.endsWith("/deployments"))?.body).toEqual({ channel_type: "wecom", release_id: release.id, expected_deployment_version: 0 });
  });

  it("shows both version usages and disables publishing when all created channels use it", async () => {
    currentPublication = { ...publication, active_release: release, enabled: false, wecom_channel: { ...wecomChannel, active_release: release, enabled: true } };
    await open(false); await click("版本管理");
    const usage = container.querySelector(".application-version-table tbody tr")?.querySelectorAll("td")[3];
    expect(usage?.textContent).toContain("API · 停用");
    expect(usage?.textContent).toContain("企业微信");
    expect(button("发布 r1")?.disabled).toBe(true);
    await click("查看 r1");
    expect(container.querySelector(".application-version-meta")?.textContent).toContain("API");
    expect(container.querySelector(".application-version-meta")?.textContent).toContain("企业微信");
  });

  it("does not offer a version row publication when only an expired WeCom channel needs it", async () => {
    currentPublication = { ...publication, active_release: release, wecom_channel: { ...wecomChannel, active_release: previousRelease, expires_at: "2020-01-01T00:00:00Z" } };
    await open(false); await click("版本管理");
    expect(button("发布 r1")?.disabled).toBe(true);
  });

  it("updates version row publication eligibility when the WeCom target expires while viewing versions", async () => {
    vi.useFakeTimers();
    currentPublication = { ...publication, active_release: release, wecom_channel: { ...wecomChannel, active_release: previousRelease, expires_at: new Date(Date.now() + 1_000).toISOString() } };
    await open(false); await click("版本管理");
    expect(button("发布 r1")?.disabled).toBe(false);
    await act(async () => { await vi.advanceTimersByTimeAsync(1_001); });
    expect(button("发布 r1")?.disabled).toBe(true);
  });

  it("labels the WeCom comparison version without attributing it to API", async () => {
    currentPublication = { ...publication, active_release: release, wecom_channel: { ...wecomChannel, active_release: previousRelease } };
    await open(false); await click("版本管理"); await click("查看 r1");
    const comparison = container.querySelector<HTMLOptionElement>("[aria-label='比较版本'] option[value=release-0]");
    expect(comparison?.textContent).toBe("r0 · 企业微信 当前版本");
    expect(container.querySelector<HTMLSelectElement>("[aria-label='比较版本']")?.value).toBe("release-0");
  });

  it("keeps a conflicted WeCom configuration and clears the secret after saving", async () => {
    currentPublication = { ...publication, api_channel_created: false, wecom_channel: { ...wecomChannel, active_release: release, last_reported_at: new Date().toISOString() } };
    await open(false);
    await click("配置企业微信");
    expect(container.querySelector<HTMLInputElement>("#wecom-secret")?.value).toBe("");
    fill(container.querySelector<HTMLInputElement>("#wecom-bot-id")!, "bot-2");
    channelFailure = true;
    await click("保存配置");
    expect(container.querySelector("[role=dialog] [role=alert]")?.textContent).toContain("配置已变化");
    expect(container.querySelector<HTMLInputElement>("#wecom-secret")?.value).toBe("");
    expect(requests.find((item) => item.method === "PUT" && item.path.endsWith("/channels/wecom"))?.body).toEqual({ expected_config_revision: 1, bot_id: "bot-2", default_response: "稍后再试", expires_at: null });
    channelFailure = false;
    fill(container.querySelector<HTMLInputElement>("#wecom-secret")!, "rotated-secret");
    await click("保存配置");
    expect(requests.filter((item) => item.method === "PUT" && item.path.endsWith("/channels/wecom")).at(-1)?.body.secret).toBe("rotated-secret");
    expect(container.querySelector("[role=dialog]")).toBeNull();
    await click("配置企业微信");
    expect(container.querySelector<HTMLInputElement>("#wecom-secret")?.value).toBe("");
  });

  it("shows an unpublished channel without a version difference", async () => {
    await open(false);
    expect(container.querySelector(".application-release-strip")?.textContent).toContain("未发布");
    expect(container.textContent).not.toContain("草稿与渠道版本不同");
  });

  it.each([true, false])("compares the draft only against an existing channel version (changed: %s)", async (changed) => {
    currentPublication = { ...currentPublication, active_release: release, has_unpublished_changes: changed };
    await open(false);
    expect(container.querySelector(".application-release-strip")?.textContent).toContain("r1");
    expect(container.textContent?.includes("草稿与渠道版本不同")).toBe(changed);
  });

  it("asks for confirmation before revoking a key", async () => {
    withExistingKey = true;
    const confirm = vi.fn(() => false);
    vi.stubGlobal("confirm", confirm);
    await open(); await click("客户端与密钥");
    await click("撤销");
    expect(confirm).toHaveBeenCalled();
    expect(requests.some((item) => item.method === "DELETE")).toBe(false);
    confirm.mockReturnValue(true);
    await click("撤销");
    expect(requests.some((item) => item.method === "DELETE")).toBe(true);
  });

  it("shows OpenAI connection settings and SDK examples for the Agent", async () => {
    await open(false); await click("管理 API");
    expect(container.textContent).toContain("OpenAI 兼容");
    expect(container.querySelector("[aria-label='Base URL']")?.textContent).toMatch(/\/v1$/);
    expect(container.textContent).toContain("请替换为公开网关地址");
    expect(container.querySelector("[aria-label='Model']")?.textContent).toBe("app-1");
    const examples = Array.from(container.querySelectorAll(".application-example pre")).map((item) => item.textContent).join("\n");
    expect(examples).toContain("/v1/chat/completions");
    expect(examples).toContain('"model":"app-1"');
    expect(examples).toContain("client.chat.completions.create");
    expect(examples).not.toContain("response_mode");
    expect(examples).not.toContain("Idempotency-Key");
  });

  it("uses the configured public gateway base URL for external callers", async () => {
    vi.stubGlobal("NIANLUN_PUBLIC_API_BASE", "https://gw.example.com/");
    await open(false); await click("管理 API");
    expect(container.querySelector("[aria-label='Base URL']")?.textContent).toBe("https://gw.example.com/v1");
    expect(container.querySelector("[aria-label='调用地址']")?.textContent).toBe("https://gw.example.com/v1/chat/completions");
    expect(container.textContent).not.toContain("请替换为公开网关地址");
  });

  it("keeps failed channel creation in the dialog and does not fabricate a channel", async () => {
    currentPublication = { ...currentPublication, api_channel_created: false }; channelFailure = true;
    await open(false); await click("创建发布渠道");
    await act(async () => { container.querySelector<HTMLInputElement>("input[value=api]")!.click(); });
    await click("创建");
    expect(container.querySelector("[role=dialog] [role=alert]")?.textContent).toContain("配置已变化");
    expect(container.textContent).not.toContain("管理 API");
    await click("取消");
    expect(container.textContent).toContain("还没有发布渠道");
  });

  it("requires release confirmation and cancelling makes no publication request", async () => {
    await open(false); await click("发布");
    expect(container.querySelector("[role=dialog]")?.textContent).toContain("API 服务");
    expect(requests.some((item) => item.path.endsWith("/publish"))).toBe(false);
    await click("取消");
    expect(container.querySelector("[role=dialog]")).toBeNull();
    expect(requests.some((item) => item.path.endsWith("/publish"))).toBe(false);
  });

  it("publishes the new configuration version after saving the draft", async () => {
    await open(); await click("Agent 配置");
    await act(async () => { container.querySelector<HTMLInputElement>("[name=clarification_enabled]")!.click(); });
    await act(async () => { container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    await publish();
    expect(requests.find((item) => item.path.endsWith("/releases") && item.method === "POST")?.body.expected_config_version).toBe(2);
  });

  it("does not resave an unchanged draft and enables saving only after an edit", async () => {
    await open(); await click("Agent 配置");
    let save = container.querySelector<HTMLButtonElement>(".application-form-actions button")!;
    expect(save.textContent).toBe("已保存");
    expect(save.disabled).toBe(true);
    await act(async () => { container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    expect(requests.some((item) => item.method === "PATCH")).toBe(false);
    await act(async () => { container.querySelector<HTMLInputElement>("[name=clarification_enabled]")!.click(); });
    save = container.querySelector<HTMLButtonElement>(".application-form-actions button")!;
    expect(save.textContent).toBe("保存草稿");
    expect(save.disabled).toBe(false);
    await click("保存草稿");
    expect(requests.filter((item) => item.method === "PATCH")).toHaveLength(1);
    expect(container.querySelector(".application-form-actions button")?.textContent).toBe("已保存");
    expect(container.querySelector<HTMLButtonElement>(".application-form-actions button")?.disabled).toBe(true);
  });

  it("keeps unsaved edits across tabs and prevents publishing until saved", async () => {
    await open(); await click("Agent 配置");
    await act(async () => { container.querySelector<HTMLInputElement>("[name=clarification_enabled]")!.click(); });
    await click("发布");
    expect(container.querySelector<HTMLButtonElement>("[aria-label='确认发布']")?.disabled).toBe(true);
    await click("取消");
    await click("发布渠道"); await click("Agent 配置");
    expect(container.querySelector<HTMLInputElement>("[name=clarification_enabled]")?.checked).toBe(false);
    await act(async () => { container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    expect(container.querySelector<HTMLButtonElement>("[aria-label='发布']")?.disabled).toBe(false);
  });

  it("keeps deletion as the only menu entry and supports dismissing it", async () => {
    await open(false); await click("全部 Agent");
    expect(container.textContent).not.toContain("删除 Agent");
    await click("产品助手更多操作");
    expect(container.querySelector("[role=menu]")?.textContent).toBe("删除 Agent");
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    expect(container.querySelector("[role=menu]")).toBeNull();
  });

  it("opens the Agent detail by clicking anywhere on the row", async () => {
    await open(false); await click("全部 Agent");
    const row = container.querySelector<HTMLDivElement>(".application-list-row.is-clickable");
    expect(row).toBeTruthy();
    await act(async () => { row!.click(); });
    expect(container.textContent).toContain("全部 Agent");
    expect(container.textContent).toContain("Agent 配置");
  });

  it("keeps row actions from opening the Agent detail", async () => {
    const onUse = vi.fn();
    await act(async () => { root.render(<ApplicationView apps={[application]} knowledgeBases={[]} llmModels={[]} selectedAppId="app-1" onCreate={vi.fn()} onUse={onUse} onDelete={vi.fn()} onKnowledge={vi.fn()} onModels={vi.fn()} />); });
    await act(async () => { container.querySelector<HTMLButtonElement>(".application-row-actions .quiet-button")!.click(); });
    expect(onUse).toHaveBeenCalledWith("app-1");
    expect(container.textContent).not.toContain("全部 Agent");
  });
});
