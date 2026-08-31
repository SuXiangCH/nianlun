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

describe("ChatView stop button", () => {
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

  it("shows a stop button while busy and keeps the partial text on abort", async () => {
    const { parseSse } = await import("../../api/sse");
    const { api } = await import("../../api/client");
    let capturedSignal: AbortSignal | undefined;
    (api.chat as unknown as ReturnType<typeof vi.fn>).mockImplementation(
      (
        _app: string,
        _message: string,
        _conversation: string,
        _clarificationEnabled: boolean,
        signal?: AbortSignal,
      ) => {
        capturedSignal = signal;
        return Promise.resolve({ ok: true } as Response);
      },
    );
    // Stream stays open after the first delta so the request is still in flight
    // when the test clicks stop.
    (parseSse as unknown as ReturnType<typeof vi.fn>).mockImplementation(async function* () {
      yield { event: "ready", data: { conversation_id: "conv-stop" } };
      yield { event: "message", data: { delta: "部分内容" } };
      await new Promise(() => undefined);
    });
    act(() => root.render(<ChatHarness initialConversationId="conv-stop" />));

    const textarea = container.querySelector<HTMLTextAreaElement>('textarea[name="message"]')!;
    const form = container.querySelector<HTMLFormElement>("form.composer")!;
    await act(async () => {
      textarea.value = "问题";
      form.requestSubmit();
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    // While busy the composer swaps the send button for a stop button.
    const stopButton = container.querySelector<HTMLButtonElement>('button[aria-label="停止回答"]');
    expect(stopButton).not.toBeNull();
    expect(container.querySelector('button[aria-label="发送"]')).toBeNull();

    act(() => stopButton!.click());

    // The in-flight request is aborted and the partial answer stays, marked stopped.
    expect(capturedSignal?.aborted).toBe(true);
    expect(container.textContent).toContain("部分内容");
    expect(container.textContent).toContain("已停止");
    // Busy is cleared: the send button returns and the input is usable again.
    expect(container.querySelector('button[aria-label="发送"]')).not.toBeNull();
    expect(container.querySelector<HTMLTextAreaElement>('textarea[name="message"]')!.disabled).toBe(false);
  });

  it("removes the pending bubble when stopping before any text is streamed", async () => {
    const { parseSse } = await import("../../api/sse");
    const { api } = await import("../../api/client");
    (api.chat as unknown as ReturnType<typeof vi.fn>).mockImplementation(
      (
        _app: string,
        _message: string,
        _conversation: string,
        _clarificationEnabled: boolean,
        _signal?: AbortSignal,
      ) =>
        Promise.resolve({ ok: true } as Response),
    );
    (parseSse as unknown as ReturnType<typeof vi.fn>).mockImplementation(async function* () {
      yield { event: "ready", data: { conversation_id: "conv-empty" } };
      await new Promise(() => undefined);
    });
    act(() => root.render(<ChatHarness initialConversationId="conv-empty" />));

    const textarea = container.querySelector<HTMLTextAreaElement>('textarea[name="message"]')!;
    const form = container.querySelector<HTMLFormElement>("form.composer")!;
    await act(async () => {
      textarea.value = "问题";
      form.requestSubmit();
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    // No assistant text yet: only the user message should remain after stopping.
    const stopButton = container.querySelector<HTMLButtonElement>('button[aria-label="停止回答"]');
    act(() => stopButton!.click());

    expect(container.textContent).not.toContain("已停止");
    expect(container.querySelectorAll("article.message").length).toBe(1);
    expect(container.textContent).toContain("问题");
  });

  it("keeps an interrupted processing draft in the trace instead of the answer", async () => {
    const { parseSse } = await import("../../api/sse");
    const { api } = await import("../../api/client");
    (api.chat as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true } as Response);
    (parseSse as unknown as ReturnType<typeof vi.fn>).mockImplementation(async function* () {
      yield { event: "ready", data: { conversation_id: "conv-processing" } };
      yield {
        event: "trace",
        data: {
          kind: "agent_message_delta",
          delta: "我先确认需要检索哪些文档。",
          round: 1,
        },
      };
      await new Promise(() => undefined);
    });
    act(() => root.render(<ChatHarness initialConversationId="conv-processing" />));

    const textarea = container.querySelector<HTMLTextAreaElement>('textarea[name="message"]')!;
    const form = container.querySelector<HTMLFormElement>("form.composer")!;
    await act(async () => {
      textarea.value = "问题";
      form.requestSubmit();
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    const stopButton = container.querySelector<HTMLButtonElement>('button[aria-label="停止回答"]')!;
    act(() => stopButton.click());

    const assistant = container.querySelector("article.message:not(.user)");
    expect(assistant?.querySelector(".agent-trace")?.textContent).toContain("我先确认需要检索哪些文档。");
    expect(assistant?.querySelector(".message-text")?.textContent).toBe("");
    expect(assistant?.textContent).toContain("已停止");
  });
});
