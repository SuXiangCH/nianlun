// @vitest-environment jsdom

import { act } from "react";
import { useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatView } from "./ChatView";
import type { Application } from "../../types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// Same mocking strategy as ChatView.usage.test.tsx: parseSse yields a fixed event
// sequence and api.chat resolves to a truthy Response-like object.
vi.mock("../../api/client", () => ({
  api: { chat: vi.fn(), getConversationMessages: vi.fn(), listConversations: vi.fn() },
}));
vi.mock("../../api/sse", () => ({ parseSse: vi.fn() }));

const application: Application = {
  id: "app-1",
  name: "测试应用",
  description: "",
  knowledge_base_id: "kb-1",
  llm_model_id: "llm-1",
  provider: "default",
  search_mode: "scan",
  created_at: "",
  updated_at: "",
};

function ChatHarness({ initialConversationId }: { initialConversationId: string }) {
  const [conversationId, setConversationId] = useState(initialConversationId);
  return (
    <ChatView
      apps={[application]}
      selectedAppId={application.id}
      onSelectApp={() => undefined}
      onNewConversation={() => undefined}
      resetToken={0}
      conversationId={conversationId}
      onConversationId={setConversationId}
      toast={() => undefined}
    />
  );
}

// call-2/call-3 同 batch（模型同一轮响应里并行发出），call-1 是独立的一轮。
const TOOL_CALLS = [
  { name: "search_across_docs", args: { query: "营收" }, elapsed_ms: 120, tool_call_id: "call-1", batch: 1 },
  { name: "search_across_docs", args: { query: "利润" }, elapsed_ms: 98, tool_call_id: "call-2", batch: 2 },
  { name: "get_line_content", args: { doc_id: "doc-1", line_spec: "5-7" }, elapsed_ms: 1500, tool_call_id: "call-3", batch: 2 },
];

describe("ChatView tool-call details", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    localStorage.clear();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it("renders collapsible tool-call details under the assistant message", async () => {
    const { parseSse } = await import("../../api/sse");
    const { api } = await import("../../api/client");
    (api.chat as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true } as Response);
    (parseSse as unknown as ReturnType<typeof vi.fn>).mockImplementation(async function* () {
      yield { event: "ready", data: { conversation_id: "conv-1" } };
      yield { event: "message", data: { delta: "答案是" } };
      yield {
        event: "done",
        data: {
          app_id: application.id,
          conversation_id: "conv-1",
          message_id: "m1",
          answer: "答案是",
          route: "retrieval",
          retrieved_snippets: [],
          status_events: [],
          tool_calls: TOOL_CALLS,
          usage: { input_tokens: 100, output_tokens: 50, total_tokens: 150, cached_tokens: 0 },
          ttft_ms: 2500,
        },
      };
    });
    act(() => root.render(<ChatHarness initialConversationId="conv-1" />));

    const textarea = container.querySelector<HTMLTextAreaElement>('textarea[name="message"]')!;
    const form = container.querySelector<HTMLFormElement>("form.composer")!;
    await act(async () => {
      textarea.value = "查营收";
      form.requestSubmit();
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    const details = container.querySelector(".message-details");
    expect(details).not.toBeNull();
    expect(details?.querySelector("summary")?.textContent).toContain("详情 · 3 次工具调用 · 2 轮");
    const labels = Array.from(details?.querySelectorAll(".tool-call-batch-label") || []).map((node) => node.textContent);
    expect(labels).toEqual(["第 1 轮", "第 2 轮 · 并行 2 个"]);
    const items = details?.querySelectorAll(".tool-call-list li") || [];
    expect(items).toHaveLength(3);
    expect(items[0]?.textContent).toContain("搜索知识库");
    expect(items[0]?.textContent).toContain("query=营收");
    expect(items[0]?.textContent).toContain("120ms");
    expect(items[2]?.textContent).toContain("读取正文");
    expect(items[2]?.textContent).toContain("line_spec=5-7");
    expect(items[2]?.textContent).toContain("1.5s");

    // Tool calls are persisted with the message snapshot for local reloads.
    const stored = JSON.parse(localStorage.getItem(`nianlun.chat.${application.id}.conv-1`) || "{}");
    expect(stored.messages[1].tool_calls).toEqual(TOOL_CALLS);
  });

  it("renders details from server-persisted history without localStorage", async () => {
    const { api } = await import("../../api/client");
    (api.getConversationMessages as unknown as ReturnType<typeof vi.fn>).mockResolvedValue([
      { id: "u1", conversation_id: "conv-2", seq_no: 1, role: "user", content: "查营收", status: "completed", route: null, error_message: null, created_at: "", updated_at: "", sources: [] },
      { id: "m2", conversation_id: "conv-2", seq_no: 2, role: "assistant", content: "答案是", status: "completed", route: "retrieval", error_message: null, tool_calls: TOOL_CALLS, usage: { input_tokens: 100, output_tokens: 50, total_tokens: 150, cached_tokens: 0 }, ttft_ms: 2500, created_at: "", updated_at: "", sources: [] },
    ]);
    (api.listConversations as unknown as ReturnType<typeof vi.fn>).mockResolvedValue([]);

    act(() => root.render(<ChatHarness initialConversationId="conv-2" />));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });

    const details = container.querySelector(".message-details");
    expect(details).not.toBeNull();
    expect(details?.textContent).toContain("搜索知识库");
    expect(container.querySelector(".message-usage")?.textContent).toContain("首字 2.5s");
  });

  it("omits the details block when the turn made no tool calls", async () => {
    const { parseSse } = await import("../../api/sse");
    const { api } = await import("../../api/client");
    (api.chat as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true } as Response);
    (parseSse as unknown as ReturnType<typeof vi.fn>).mockImplementation(async function* () {
      yield { event: "ready", data: { conversation_id: "conv-3" } };
      yield {
        event: "done",
        data: {
          app_id: application.id,
          conversation_id: "conv-3",
          message_id: "m3",
          answer: "你好",
          route: "direct",
          retrieved_snippets: [],
          status_events: [],
          tool_calls: [],
          usage: null,
          ttft_ms: 10,
        },
      };
    });
    act(() => root.render(<ChatHarness initialConversationId="conv-3" />));

    const textarea = container.querySelector<HTMLTextAreaElement>('textarea[name="message"]')!;
    const form = container.querySelector<HTMLFormElement>("form.composer")!;
    await act(async () => {
      textarea.value = "你好";
      form.requestSubmit();
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    expect(container.querySelector(".message-details")).toBeNull();
  });
});
