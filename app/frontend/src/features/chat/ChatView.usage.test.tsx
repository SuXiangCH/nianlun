// @vitest-environment jsdom

import { act } from "react";
import { useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatView } from "./ChatView";
import type { Application } from "../../types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// parseSse is mocked to yield a fixed event sequence, and api.chat just needs to
// resolve to a truthy Response-like object (parseSse ignores it).
// getConversationMessages / listConversations default to throwing (caught in-app),
// and are overridden per-test when exercising the history-reload path.
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

const USAGE = { input_tokens: 1234, output_tokens: 567, total_tokens: 1801, cached_tokens: 890 };

async function streamOnce() {
  const { parseSse } = await import("../../api/sse");
  const { api } = await import("../../api/client");
  (api.chat as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true } as Response);
  (parseSse as unknown as ReturnType<typeof vi.fn>).mockImplementation(async function* () {
    yield { event: "ready", data: { conversation_id: "conv-1" } };
    yield { event: "message", data: { delta: "你好" } };
    yield {
      event: "done",
      data: {
        app_id: application.id,
        conversation_id: "conv-1",
        message_id: "m1",
        answer: "你好",
        route: "direct",
        retrieved_snippets: [],
        status_events: [],
        usage: USAGE,
        ttft_ms: 1234,
      },
    };
  });
}

describe("ChatView token usage display", () => {
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

  it("shows token usage under the assistant message after the stream completes", async () => {
    await streamOnce();
    act(() => root.render(<ChatHarness initialConversationId="conv-1" />));

    const textarea = container.querySelector<HTMLTextAreaElement>('textarea[name="message"]')!;
    const form = container.querySelector<HTMLFormElement>("form.composer")!;
    await act(async () => {
      textarea.value = "你好";
      form.requestSubmit();
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    const usage = container.querySelector(".message-usage");
    expect(usage).not.toBeNull();
    expect(usage?.textContent).toContain("1,234");
    expect(usage?.textContent).toContain("567");
    expect(usage?.textContent).toContain("1,801");
    expect(usage?.textContent).toContain("890");
    expect(usage?.textContent).toContain("缓存");
    expect(usage?.textContent).toContain("首字");
    expect(usage?.textContent).toContain("1.2s");
  });

  it("omits the cached segment when no cache hit is reported", async () => {
    const { parseSse } = await import("../../api/sse");
    (parseSse as unknown as ReturnType<typeof vi.fn>).mockImplementation(async function* () {
      yield { event: "ready", data: { conversation_id: "conv-2" } };
      yield { event: "message", data: { delta: "嗨" } };
      yield {
        event: "done",
        data: {
          app_id: application.id,
          conversation_id: "conv-2",
          message_id: "m2",
          answer: "嗨",
          route: "direct",
          retrieved_snippets: [],
          status_events: [],
          usage: { input_tokens: 10, output_tokens: 5, total_tokens: 15, cached_tokens: 0 },
        },
      };
    });
    act(() => root.render(<ChatHarness initialConversationId="conv-2" />));

    const textarea = container.querySelector<HTMLTextAreaElement>('textarea[name="message"]')!;
    const form = container.querySelector<HTMLFormElement>("form.composer")!;
    await act(async () => {
      textarea.value = "嗨";
      form.requestSubmit();
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    const usage = container.querySelector(".message-usage");
    expect(usage?.textContent).toContain("15");
    expect(usage?.textContent).not.toContain("缓存");
    expect(usage?.textContent).not.toContain("首字");
  });

  it("marks an SSE response without done as failed instead of persisting a pending answer", async () => {
    const { parseSse } = await import("../../api/sse");
    const { api } = await import("../../api/client");
    (api.chat as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true } as Response);
    (parseSse as unknown as ReturnType<typeof vi.fn>).mockImplementation(async function* () {
      yield { event: "ready", data: { conversation_id: "conv-incomplete" } };
      yield { event: "message", data: { delta: "半截" } };
    });
    act(() => root.render(<ChatHarness initialConversationId="conv-incomplete" />));

    const textarea = container.querySelector<HTMLTextAreaElement>('textarea[name="message"]')!;
    const form = container.querySelector<HTMLFormElement>("form.composer")!;
    await act(async () => {
      textarea.value = "问题";
      form.requestSubmit();
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    expect(container.querySelector(".message.error")).not.toBeNull();
    expect(container.textContent).toContain("流式响应未正常结束");
    const stored = JSON.parse(localStorage.getItem(`nianlun.chat.${application.id}.conv-incomplete`) || "{}");
    expect(stored.messages.some((message: { pending?: boolean }) => message.pending)).toBe(false);
  });

  it("preserves token usage when the history API reloads messages without it", async () => {
    const { api } = await import("../../api/client");
    // Backend returns the assistant message WITHOUT a usage field, but localStorage
    // already recorded one for message id "m3" (kept across a conversation switch).
    (api.getConversationMessages as unknown as ReturnType<typeof vi.fn>).mockResolvedValue([
      { id: "m3", conversation_id: "conv-3", seq_no: 2, role: "assistant", content: "你好", status: "completed", route: "direct", error_message: null, created_at: "", updated_at: "", sources: [] },
    ]);
    (api.listConversations as unknown as ReturnType<typeof vi.fn>).mockResolvedValue([]);
    localStorage.setItem(
      `nianlun.chat.${application.id}.conv-3`,
      JSON.stringify({
        messages: [{ id: "m3", role: "assistant", text: "你好", usage: USAGE, ttft_ms: 1234 }],
        sources: [],
      }),
    );

    act(() => root.render(<ChatHarness initialConversationId="conv-3" />));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });

    const usage = container.querySelector(".message-usage");
    expect(usage).not.toBeNull();
    expect(usage?.textContent).toContain("1,234");
    expect(usage?.textContent).toContain("首字");
    expect(usage?.textContent).toContain("1.2s");
    // The reload re-persisted the preserved usage and TTFT, so they survive a remount too.
    const stored = JSON.parse(localStorage.getItem(`nianlun.chat.${application.id}.conv-3`) || "{}");
    expect(stored.messages[0].usage).toEqual(USAGE);
    expect(stored.messages[0].ttft_ms).toBe(1234);
  });
});
