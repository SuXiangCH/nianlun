// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApplicationConversations } from "./ApplicationConversations";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const created = "2026-10-20T08:00:00Z";
const records = {
  console: [{ id: "conv-console", application_id: "app-1", title: "控制台会话", status: "active", created_at: created, updated_at: created, last_message_at: created, message_count: 2, channel: "console", external_user_id: null }],
  api: [{ id: "conv-api", application_id: "app-1", title: "API 会话", status: "active", created_at: created, updated_at: created, last_message_at: created, message_count: 1, channel: "api", external_user_id: null }],
  wecom: [{ id: "conv-wecom", application_id: "app-1", title: "企业微信群会话", status: "active", created_at: created, updated_at: created, last_message_at: created, message_count: 1, channel: "wecom", external_user_id: "wc_abc123" }],
};
const messages = [
  { id: "m1", conversation_id: "conv-console", seq_no: 1, role: "user", content: "你好", status: "completed", route: null, error_message: null, created_at: created, updated_at: created, sources: [] },
  { id: "m2", conversation_id: "conv-console", seq_no: 2, role: "assistant", content: "你好，有什么可以帮你？", status: "completed", route: null, error_message: null, created_at: created, updated_at: created, sources: [] },
];
const response = (data: unknown, status = 200) => new Response(JSON.stringify({ data, message: status === 503 ? "服务暂不可用" : "ok" }), { status });

describe("Application conversations", () => {
  let container: HTMLDivElement;
  let root: Root;
  let requestedChannels: string[];
  let deleted: string[];
  beforeEach(() => {
    container = document.createElement("div"); document.body.appendChild(container); root = createRoot(container);
    requestedChannels = []; deleted = [];
    vi.stubGlobal("fetch", vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input);
      const method = init?.method || "GET";
      let data: unknown = null;
      if (url.pathname.endsWith("/messages")) data = messages;
      else if (method === "DELETE") { deleted.push(url.pathname); data = null; }
      else if (url.pathname.endsWith("/conversations")) {
        const channel = (url.searchParams.get("channel") || "console") as keyof typeof records;
        requestedChannels.push(channel);
        data = records[channel];
      }
      return new Response(JSON.stringify({ data }), { status: 200 });
    }));
  });
  afterEach(() => { act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); });
  const change = async (select: HTMLSelectElement, value: string) => {
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(select, value);
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
  };
  const render = async () => { await act(async () => { root.render(<ApplicationConversations appId="app-1" />); }); };

  it("defaults to console conversations and renders their messages", async () => {
    await render();
    expect(requestedChannels).toEqual(["console"]);
    expect(container.textContent).toContain("控制台会话");
    expect(container.textContent).toContain("你好，有什么可以帮你？");
    expect(container.querySelector("button[aria-label='删除会话'], .application-conversation-detail header button")).toBeTruthy();
  });

  it("filters by WeCom and keeps the mirrored conversation read-only", async () => {
    await render();
    await change(container.querySelector<HTMLSelectElement>("#conversation-channel")!, "wecom");
    expect(requestedChannels).toContain("wecom");
    expect(container.textContent).toContain("企业微信群会话");
    expect(container.textContent).toContain("wc_abc123");
    expect(container.textContent).toContain("企业微信会话只读");
    expect(Array.from(container.querySelectorAll("button")).some((item) => item.textContent?.includes("删除会话"))).toBe(false);
  });

  it("keeps API conversations read-only", async () => {
    await render();
    await change(container.querySelector<HTMLSelectElement>("#conversation-channel")!, "api");
    expect(container.textContent).toContain("API 会话");
    expect(container.querySelector(".application-conversation-detail header button")).toBeNull();
  });

  it("clears the previous conversation messages on a failed selection and recovers on refresh", async () => {
    let failure = true;
    const next = { ...records.console[0], id: "conv-next", title: "第二个会话" };
    vi.stubGlobal("fetch", vi.fn(async (input: string) => {
      const path = new URL(input).pathname;
      if (path.endsWith("/conversations")) return response([...records.console, next]);
      if (path.endsWith("/conv-next/messages")) return failure ? response(null, 503) : response([{ ...messages[0], conversation_id: next.id, content: "第二个会话的消息" }]);
      return response(messages);
    }));
    await render();
    await act(async () => { container.querySelectorAll<HTMLButtonElement>(".application-conversation-list button")[1].click(); });
    expect(container.querySelector("[role=alert]")).not.toBeNull();
    expect(container.textContent).not.toContain("你好，有什么可以帮你？");
    expect(container.querySelector(".application-conversation-detail header")?.textContent).toContain("第二个会话");
    failure = false;
    await act(async () => { container.querySelector<HTMLButtonElement>("[aria-label='刷新会话']")!.click(); });
    expect(container.textContent).toContain("第二个会话的消息");
    expect(container.querySelector("[role=alert]")).toBeNull();
  });

  it("clears the previous channel list on failure and recovers without mislabeling its records", async () => {
    let failure = true;
    vi.stubGlobal("fetch", vi.fn(async (input: string) => {
      const url = new URL(input);
      if (url.pathname.endsWith("/messages")) return response(messages);
      const channel = (url.searchParams.get("channel") || "console") as keyof typeof records;
      return channel === "api" && failure ? response(null, 503) : response(records[channel]);
    }));
    await render();
    await change(container.querySelector<HTMLSelectElement>("#conversation-channel")!, "wecom");
    await change(container.querySelector<HTMLSelectElement>("#conversation-channel")!, "api");
    expect(container.querySelector("[role=alert]")).not.toBeNull();
    expect(container.textContent).not.toContain("企业微信群会话");
    expect(container.textContent).not.toContain("wc_abc123");
    expect(container.querySelector(".application-conversation-detail")).toBeNull();
    failure = false;
    await act(async () => { container.querySelector<HTMLButtonElement>("[aria-label='刷新会话']")!.click(); });
    expect(container.textContent).toContain("API 会话");
    expect(container.querySelector("[role=alert]")).toBeNull();
  });

  it("ignores an aborted message request that completes after a newer selection", async () => {
    let resolveOld!: (response: Response) => void;
    let oldSignal: AbortSignal | null | undefined;
    const pending = new Promise<Response>((resolve) => { resolveOld = resolve; });
    const next = { ...records.console[0], id: "conv-next", title: "第二个会话" };
    vi.stubGlobal("fetch", vi.fn(async (input: string, init?: RequestInit) => {
      const path = new URL(input).pathname;
      if (path.endsWith("/conversations")) return response([...records.console, next]);
      if (path.endsWith("/conv-console/messages")) { oldSignal = init?.signal; return pending; }
      return response([{ ...messages[0], conversation_id: next.id, content: "第二个会话的消息" }]);
    }));
    await render();
    await act(async () => { container.querySelectorAll<HTMLButtonElement>(".application-conversation-list button")[1].click(); });
    expect(oldSignal?.aborted).toBe(true);
    await act(async () => { resolveOld(response(messages)); });
    expect(container.textContent).toContain("第二个会话的消息");
    expect(container.textContent).not.toContain("你好，有什么可以帮你？");
  });

  it.each(["channel", "application"])("aborts the old messages immediately when the %s changes before its list resolves", async (scope) => {
    let resolveOld!: (response: Response) => void;
    let resolveList!: (response: Response) => void;
    let oldSignal: AbortSignal | null | undefined;
    const oldMessages = new Promise<Response>((resolve) => { resolveOld = resolve; });
    const nextList = new Promise<Response>((resolve) => { resolveList = resolve; });
    const nextApp = scope === "application" ? "app-2" : "app-1";
    const nextRecord = { ...records.wecom[0], application_id: nextApp, channel: scope === "application" ? "console" : "wecom" };
    vi.stubGlobal("fetch", vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input);
      if (url.pathname.endsWith("/conv-console/messages")) { oldSignal = init?.signal; return oldMessages; }
      if (url.pathname.endsWith("/messages")) return response([{ ...messages[0], conversation_id: nextRecord.id, content: "新范围的消息" }]);
      if (url.pathname.includes("app-2") || url.searchParams.get("channel") === "wecom") return nextList;
      return response(records.console);
    }));
    await render();
    if (scope === "channel") await change(container.querySelector<HTMLSelectElement>("#conversation-channel")!, "wecom");
    else await act(async () => { root.render(<ApplicationConversations appId="app-2" />); });
    expect(oldSignal?.aborted).toBe(true);
    await act(async () => { resolveOld(response(messages)); });
    expect(container.textContent).not.toContain("你好，有什么可以帮你？");
    await act(async () => { resolveList(response([nextRecord])); });
    expect(container.textContent).toContain("新范围的消息");
    expect(container.textContent).not.toContain("你好，有什么可以帮你？");
  });

  it("ignores a list response from an application that was left while loading", async () => {
    let resolveOld!: (response: Response) => void;
    let oldSignal: AbortSignal | null | undefined;
    const oldList = new Promise<Response>((resolve) => { resolveOld = resolve; });
    const nextRecord = { ...records.console[0], application_id: "app-2", id: "conv-next", title: "新应用会话" };
    vi.stubGlobal("fetch", vi.fn(async (input: string, init?: RequestInit) => {
      const path = new URL(input).pathname;
      if (path.includes("app-1")) { oldSignal = init?.signal; return oldList; }
      return response(path.endsWith("/messages") ? [{ ...messages[0], conversation_id: nextRecord.id, content: "新应用消息" }] : [nextRecord]);
    }));
    await render();
    await act(async () => { root.render(<ApplicationConversations appId="app-2" />); });
    expect(oldSignal?.aborted).toBe(true);
    await act(async () => { resolveOld(response(records.console)); });
    expect(container.textContent).toContain("新应用会话");
    expect(container.textContent).toContain("新应用消息");
    expect(container.textContent).not.toContain("控制台会话");
  });

  it("deletes console conversations after confirmation", async () => {
    const confirm = vi.fn(() => true);
    vi.stubGlobal("confirm", confirm);
    await render();
    const remove = Array.from(container.querySelectorAll<HTMLButtonElement>("button")).find((item) => item.textContent?.includes("删除会话"));
    expect(remove).toBeTruthy();
    await act(async () => { remove!.click(); });
    expect(confirm).toHaveBeenCalled();
    expect(deleted).toEqual(["/api/v1/apps/app-1/conversations/conv-console"]);
  });

  it.each([true, false])("does not reload deleted messages while refreshing the list (remaining: %s)", async (hasRemaining) => {
    vi.stubGlobal("confirm", () => true);
    let removed = false;
    let resolveList!: (response: Response) => void;
    const refreshedList = new Promise<Response>((resolve) => { resolveList = resolve; });
    const removedMessageRequests: string[] = [];
    const remaining = { ...records.console[0], id: "conv-next", title: "剩余会话" };
    const response = (data: unknown, status = 200) => new Response(JSON.stringify({ data, message: status === 404 ? "会话不存在" : "ok" }), { status });
    vi.stubGlobal("fetch", vi.fn(async (input: string, init?: RequestInit) => {
      const path = new URL(input).pathname;
      if (init?.method === "DELETE") { removed = true; return response(null); }
      if (path.endsWith("/conversations")) return removed ? refreshedList : response(records.console);
      if (path.endsWith("/conv-console/messages") && removed) { removedMessageRequests.push(path); return response(null, 404); }
      return response(path.endsWith("/conv-next/messages") ? [{ ...messages[0], id: "next-message", conversation_id: "conv-next", content: "剩余会话的消息" }] : messages);
    }));
    await render();
    await act(async () => { container.querySelector<HTMLButtonElement>(".application-conversation-detail header button")!.click(); });
    await act(async () => { resolveList(response(hasRemaining ? [remaining] : [])); });
    expect(container.textContent).toContain(hasRemaining ? "剩余会话的消息" : "控制台暂无会话");
    expect(container.textContent).not.toContain("你好，有什么可以帮你？");
    expect(container.querySelector("[role=alert]")).toBeNull();
    expect(removedMessageRequests).toEqual([]);
  });
});
