// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApplicationView } from "./ApplicationView";
import type { Application } from "../../types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const application: Application = { id: "app-1", name: "产品助手", description: "", knowledge_base_id: "kb-1", llm_model_id: "llm-1", provider: "default", agent_version: "v0", search_mode: "fts", created_at: "2026-10-15T00:00:00Z", updated_at: "2026-10-15T00:00:00Z" };
const publication = { application_id: "app-1", api_channel_created: true, enabled: false, active_release: null, deployment_version: 0, config_version: 1, has_unpublished_changes: true, dependencies_ready: true, dependency_error: null as string | null, draft_ready: true, draft_error: null as string | null };
const client = { id: "client-1", application_id: "app-1", name: "业务后台", enabled: true, created_at: application.created_at, updated_at: application.updated_at };
const existingKey = { key_id: "existing-key", client_id: client.id, name: "生产 Key", expires_at: null, revoked_at: null, created_at: application.created_at };
const release = { id: "release-1", application_id: "app-1", sequence: 1, config_schema_version: 1, config_hash: "hash", config: {}, created_at: application.created_at };

describe("Application publishing", () => {
  let container: HTMLDivElement;
  let root: Root;
  let requests: Array<{ path: string; method: string; body: Record<string, unknown> }>;
  let keyFailure: boolean;
  let publishFailure: boolean;
  let channelFailure: boolean;
  let keyGate: Promise<void> | null;
  let draftVersion: number;
  let publicationGate: Promise<void> | null;
  let withExistingKey: boolean;
  let unknownMetrics: boolean;
  let appUpdates: Application[];
  let currentPublication: Omit<typeof publication, "active_release"> & { active_release: typeof release | null };
  beforeEach(() => {
    container = document.createElement("div"); document.body.appendChild(container); root = createRoot(container);
    requests = []; appUpdates = []; keyFailure = false; publishFailure = false; channelFailure = false; keyGate = null; publicationGate = null; draftVersion = 1; withExistingKey = false; unknownMetrics = false; currentPublication = { ...publication };
    vi.stubGlobal("fetch", vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input); const path = url.pathname; const method = init?.method || "GET";
      const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {};
      requests.push({ path, method, body });
      let data: unknown = null; let status = 200;
      if (path.endsWith("/channels")) {
        if (channelFailure) status = 409;
        else currentPublication = { ...currentPublication, api_channel_created: true };
        data = currentPublication;
      } else if (path.endsWith("/publish")) {
        if (publishFailure) { status = 409; } else currentPublication = { ...currentPublication, enabled: true, deployment_version: 1, active_release: release, has_unpublished_changes: false };
        data = currentPublication;
      } else if (path.endsWith("/publication")) {
        if (method === "GET" && publicationGate) await publicationGate;
        if (method === "PATCH") currentPublication = { ...currentPublication, enabled: Boolean(body.enabled), deployment_version: currentPublication.deployment_version + 1 };
        data = currentPublication;
      } else if (path.endsWith("/releases")) data = { items: currentPublication.active_release ? [release] : [], total: currentPublication.active_release ? 1 : 0 };
      else if (path.endsWith("/api-clients")) data = [client];
      else if (path.endsWith("/keys")) {
        if (method === "POST") { if (keyGate) await keyGate; status = keyFailure ? 409 : 200; data = { key_id: "registered", client_id: client.id, name: body.name, expires_at: null, revoked_at: null, created_at: application.created_at }; }
        else data = withExistingKey ? [existingKey] : [];
      } else if (path.endsWith("/runs")) data = { items: [{ run_id: url.searchParams.get("offset") === "20" ? "run-second-page" : "run-first-page", app_id: application.id, release_id: release.id, client_id: unknownMetrics ? "removed-client" : client.id, knowledge_base_content_version: 7, request_id: unknownMetrics ? null : "request-123", duration_ms: unknownMetrics ? null : 1250, usage: unknownMetrics ? null : { input_tokens: 100, output_tokens: 30, total_tokens: 130 }, conversation_id: "conversation-1", status: "completed", error_type: null, error_status: null, error_message: null, result: null, created_at: application.created_at, updated_at: application.updated_at }], total: 21 };
      else if (method === "DELETE") data = { ...existingKey, revoked_at: application.updated_at };
      else if (method === "PATCH") { if (body.expected_config_version !== draftVersion) status = 409; else { const { search_mode: _searchMode, ...serverApp } = application; data = { ...serverApp, ...body, config_version: ++draftVersion }; currentPublication = { ...currentPublication, config_version: draftVersion, has_unpublished_changes: true }; } }
      else if (path.endsWith("/app-1")) data = { ...application, config_version: draftVersion };
      return new Response(JSON.stringify({ code: status, message: status === 409 ? "配置已变化，请刷新后重试" : "ok", data }), { status });
    }));
  });
  afterEach(() => { act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); });
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
  const publish = async () => { await click("发布新版本"); await click("确认发布"); };
  const submit = async () => { await act(async () => { container.querySelector("[role=dialog] form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); }); };

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
    expect(requests.find((item) => item.path.endsWith("/publish"))?.body).toEqual({ expected_config_version: 1, expected_deployment_version: 0 });
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
    draftVersion = 3;
    await act(async () => { container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    expect(container.querySelector("[role=alert]")?.textContent).toContain("配置已变化");
    await click("刷新草稿");
    await act(async () => { container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    const edits = requests.filter((item) => item.method === "PATCH" && item.path.endsWith("/app-1"));
    expect(edits[1].body.expected_config_version).toBe(3);
    expect(container.textContent).toContain("草稿已保存");
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
    expect(container.querySelector<HTMLButtonElement>("[aria-label='发布新版本']")?.disabled).toBe(true);
    await act(async () => { resolve(); await publicationGate; });
    const button = container.querySelector<HTMLButtonElement>("[aria-label='发布新版本']");
    expect(button?.disabled).toBe(true);
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
    const button = container.querySelector<HTMLButtonElement>("[aria-label='发布新版本']");
    expect(button?.disabled).toBe(false);
    const resume = Array.from(container.querySelectorAll("button")).find((item) => item.textContent === "恢复 API");
    expect(resume?.disabled).toBe(true);
    expect(container.textContent).toContain("旧模型配置已变化");
    await publish();
    expect(requests.some((item) => item.path.endsWith("/publish"))).toBe(true);
  });

  it("allows restoring a valid release while a broken draft cannot publish", async () => {
    currentPublication = { ...currentPublication, active_release: release, dependencies_ready: true, draft_ready: false, draft_error: "草稿模型缺少凭据" };
    await open();
    const publishButton = container.querySelector<HTMLButtonElement>("[aria-label='发布新版本']");
    expect(publishButton?.disabled).toBe(true);
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
    expect(Array.from(container.querySelectorAll("[role=tab]")).map((item) => item.textContent)).toEqual(["Agent 配置", "发布渠道", "发布历史", "运行日志"]);
    expect(container.textContent).not.toContain("企业微信");
    expect(container.textContent).not.toContain("个人微信");
    expect(container.textContent).not.toContain("调用地址");
    expect(requests.some((item) => item.path.endsWith("/releases"))).toBe(false);
    await click("管理 API");
    expect(container.textContent).toContain("调用地址");
    await click("返回发布渠道");
    expect(container.textContent).toContain("API 服务");
    await click("发布历史");
    expect(requests.some((item) => item.path.endsWith("/releases"))).toBe(true);
    expect(container.textContent).toContain("暂无发布版本");
  });

  it("creates an API channel only after choosing a supported type", async () => {
    currentPublication = { ...currentPublication, api_channel_created: false };
    await open(false);
    expect(container.textContent).toContain("还没有发布渠道");
    expect(container.textContent).not.toContain("管理 API");
    expect(container.querySelector<HTMLButtonElement>("[aria-label='发布新版本']")?.disabled).toBe(true);
    await click("创建发布渠道");
    const create = Array.from(container.querySelectorAll<HTMLButtonElement>("[role=dialog] button")).find((item) => item.textContent === "创建");
    expect(create?.disabled).toBe(true);
    expect(container.querySelectorAll("[role=dialog] input[type=radio]:disabled")).toHaveLength(2);
    await act(async () => { container.querySelector<HTMLInputElement>("input[value=api]")!.click(); });
    await click("创建");
    expect(requests.find((item) => item.path.endsWith("/channels"))?.body).toEqual({ type: "api" });
    expect(container.textContent).toContain("管理 API");
    expect(container.textContent).not.toContain("企业微信");
    expect(requests.some((item) => item.path.endsWith("/publish"))).toBe(false);
    expect(container.querySelector("[role=dialog]")).toBeNull();
  });

  it("disables creating a second API channel once one exists", async () => {
    currentPublication = { ...currentPublication, api_channel_created: true };
    await open(false);
    const create = Array.from(container.querySelectorAll<HTMLButtonElement>("button")).find((item) => item.textContent?.trim() === "创建发布渠道");
    expect(create?.disabled).toBe(true);
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
    await open(false); await click("发布新版本");
    expect(container.querySelector("[role=dialog]")?.textContent).toContain("API 服务");
    expect(requests.some((item) => item.path.endsWith("/publish"))).toBe(false);
    await click("取消");
    expect(container.querySelector("[role=dialog]")).toBeNull();
    expect(requests.some((item) => item.path.endsWith("/publish"))).toBe(false);
  });

  it("publishes the new configuration version after saving the draft", async () => {
    await open(); await click("Agent 配置");
    await act(async () => { container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    await publish();
    expect(requests.find((item) => item.path.endsWith("/publish"))?.body.expected_config_version).toBe(2);
  });

  it("keeps unsaved edits across tabs and prevents publishing until saved", async () => {
    await open(); await click("Agent 配置");
    await act(async () => { container.querySelector<HTMLInputElement>("[name=clarification_enabled]")!.click(); });
    expect(container.querySelector<HTMLButtonElement>("[aria-label='发布新版本']")?.disabled).toBe(true);
    await click("发布渠道"); await click("Agent 配置");
    expect(container.querySelector<HTMLInputElement>("[name=clarification_enabled]")?.checked).toBe(false);
    await act(async () => { container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    expect(container.querySelector<HTMLButtonElement>("[aria-label='发布新版本']")?.disabled).toBe(false);
  });

  it("keeps deletion in the application menu and supports dismissing it", async () => {
    await open(false); await click("全部 Agent");
    expect(container.textContent).not.toContain("删除 Agent");
    await click("产品助手更多操作");
    expect(container.querySelector("[role=menu]")?.textContent).toContain("删除 Agent");
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    expect(container.querySelector("[role=menu]")).toBeNull();
  });
});
