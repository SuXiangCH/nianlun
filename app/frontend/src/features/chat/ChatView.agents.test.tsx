// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../../api/client";
import { parseSse } from "../../api/sse";
import type { Application } from "../../types";
import { ChatView } from "./ChatView";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock("../../api/client", () => ({
  api: {
    chat: vi.fn(),
    getConversationMessages: vi.fn().mockResolvedValue([]),
    listConversations: vi.fn().mockResolvedValue([]),
  },
}));
vi.mock("../../api/sse", () => ({ parseSse: vi.fn() }));

const app = (id: string): Application => ({
  id, name: id, description: "", knowledge_base_id: "kb", llm_model_id: "llm",
  provider: "default", agent_version: "v0", search_mode: "fts", created_at: "", updated_at: "",
});

describe("ChatView Agent switching", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  const renderChat = async (selectedAppId = "a") => {
    const props = {
      apps: [app("a"), app("b")], selectedAppId, onSelectApp: vi.fn(), onNewConversation: vi.fn(),
      resetToken: 0, conversationId: `conversation-${selectedAppId}`, onConversationId: vi.fn(), toast: vi.fn(),
    };
    await act(async () => root.render(<ChatView key={selectedAppId} {...props} />));
    return props;
  };

  it("selects another Agent without replacing the old Agent's saved conversation", async () => {
    const props = await renderChat();
    const picker = container.querySelector<HTMLSelectElement>("[aria-label='选择应用']")!;
    act(() => {
      picker.value = "b";
      picker.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(props.onSelectApp).toHaveBeenCalledWith("b");
    expect(props.onNewConversation).not.toHaveBeenCalled();
  });

  it("aborts the old request and ignores a late ready event after switching Agent", async () => {
    let finishRequest!: (response: Response) => void;
    let signal: AbortSignal | undefined;
    vi.mocked(api.chat).mockImplementationOnce((_app, _message, _conversation, _clarification, requestSignal) => {
      signal = requestSignal;
      return new Promise<Response>((resolve) => { finishRequest = resolve; });
    });
    vi.mocked(parseSse).mockImplementation(async function* () {
      yield { event: "ready", data: { conversation_id: "late-server-conversation-a" } };
      yield { event: "done", data: { conversation_id: "late-server-conversation-a", answer: "旧 Agent 的回答" } };
    });
    const previous = await renderChat();
    await act(async () => {
      container.querySelector<HTMLTextAreaElement>("textarea[name='message']")!.value = "提问";
      container.querySelector<HTMLFormElement>("form.composer")!.requestSubmit();
    });
    await renderChat("b");
    expect(signal?.aborted).toBe(true);
    await act(async () => finishRequest(new Response("")));
    expect(previous.onConversationId).not.toHaveBeenCalled();
    expect(container.textContent).not.toContain("旧 Agent 的回答");
    expect(localStorage.getItem("nianlun.chat.a.late-server-conversation-a")).toBeNull();
  });

  it("recovers a foreign cached conversation and restores the question without resending it", async () => {
    vi.mocked(api.chat).mockResolvedValueOnce(new Response(""));
    vi.mocked(parseSse).mockImplementation(async function* () {
      yield { event: "error", data: { message: "会话不属于当前应用" } };
    });
    const props = await renderChat();
    const input = container.querySelector<HTMLTextAreaElement>("textarea[name='message']")!;
    await act(async () => {
      input.value = "保留这个问题";
      container.querySelector<HTMLFormElement>("form.composer")!.requestSubmit();
    });
    expect(props.onNewConversation).toHaveBeenCalledOnce();
    expect(input.value).toBe("保留这个问题");
    expect(props.toast).toHaveBeenCalledWith("会话与当前 Agent 不匹配，已切换到新会话，请重新发送。", true);
    expect(api.chat).toHaveBeenCalledOnce();
    expect(container.querySelector("[aria-label='发送']")).not.toBeNull();
  });

  it("keeps the existing conversation when the failure is unrelated to ownership", async () => {
    vi.mocked(api.chat).mockResolvedValueOnce(new Response(""));
    vi.mocked(parseSse).mockImplementation(async function* () {
      yield { event: "error", data: { message: "模型暂时不可用" } };
    });
    const props = await renderChat();
    await act(async () => {
      container.querySelector<HTMLTextAreaElement>("textarea[name='message']")!.value = "提问";
      container.querySelector<HTMLFormElement>("form.composer")!.requestSubmit();
    });
    expect(props.onNewConversation).not.toHaveBeenCalled();
    expect(container.querySelector(".message.error")?.textContent).toContain("模型暂时不可用");
  });
});
