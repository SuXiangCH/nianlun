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
const AGENT_PROGRESS = "我先检索知识库中的营收和利润。";
const TRACE = [
  { kind: "status" as const, event: "context_compaction_completed", message: "历史上下文整理完成。" },
  { kind: "agent_message" as const, message: AGENT_PROGRESS, round: 1 },
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
    Reflect.deleteProperty(document, "startViewTransition");
    container.remove();
  });

  it("renders the trace before the answer and preserves tool-call details after it", async () => {
    const { parseSse } = await import("../../api/sse");
    const { api } = await import("../../api/client");
    (api.chat as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true } as Response);
    (parseSse as unknown as ReturnType<typeof vi.fn>).mockImplementation(async function* () {
      yield { event: "ready", data: { conversation_id: "conv-1" } };
      yield { event: "trace", data: TRACE[0] };
      yield { event: "message", data: { delta: AGENT_PROGRESS, phase: "candidate", round: 1 } };
      yield { event: "trace", data: TRACE[1] };
      yield { event: "message", data: { delta: "答案", phase: "candidate", round: 2 } };
      yield { event: "message", data: { delta: "是", phase: "candidate", round: 2 } };
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
          trace: TRACE,
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

    const traceDetails = container.querySelector(".agent-trace");
    const assistantMessage = traceDetails?.closest(".message-body");
    const answer = assistantMessage?.querySelector(".message-text") || null;
    const toolDetails = assistantMessage?.querySelector("details.message-details:not(.agent-trace)") || null;
    expect(traceDetails).not.toBeNull();
    expect(answer).not.toBeNull();
    expect(toolDetails).not.toBeNull();
    expect(traceDetails!.compareDocumentPosition(answer!) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
    expect(answer!.compareDocumentPosition(toolDetails!) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);

    expect(traceDetails?.querySelector("summary")?.textContent).toBe("处理过程");
    expect(traceDetails?.querySelector(".agent-trace-mark")).not.toBeNull();
    expect(traceDetails?.querySelectorAll(".agent-trace-step")).toHaveLength(2);
    expect(traceDetails?.textContent).toContain("历史上下文整理完成。");
    expect(traceDetails?.textContent).toContain(AGENT_PROGRESS);
    expect(answer?.textContent).toBe("答案是");
    expect(answer?.textContent).not.toContain(AGENT_PROGRESS);

    expect(toolDetails?.querySelector("summary")?.textContent).toContain("详情 · 3 次工具调用 · 2 轮");
    const batchLabels = Array.from(toolDetails?.querySelectorAll(".tool-call-batch-label") || []).map((node) => node.textContent);
    expect(batchLabels).toEqual(["第 1 轮", "第 2 轮 · 并行 2 个"]);
    const items = toolDetails?.querySelectorAll(".tool-call-list li") || [];
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
    expect(stored.messages[1].trace).toEqual(TRACE);
    expect(stored.messages[1].keepTraceOpen).toBeUndefined();

    await act(async () => {
      traceDetails?.querySelector("summary")?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect((traceDetails as HTMLDetailsElement).open).toBe(false);
    act(() => container.querySelector<HTMLButtonElement>('button[aria-label="会话列表"]')!.click());
    expect((traceDetails as HTMLDetailsElement).open).toBe(false);
  });

  it("smoothly promotes a candidate round and keeps the final round streaming", async () => {
    const { parseSse } = await import("../../api/sse");
    const { api } = await import("../../api/client");
    let finishStream!: () => void;
    const waitForFinish = new Promise<void>((resolve) => { finishStream = resolve; });
    const transitionNames: Array<{ source: string; target: string }> = [];
    Object.defineProperty(document, "startViewTransition", {
      configurable: true,
      value: (update: () => void) => {
        const source = container.querySelector<HTMLElement>(".message-text.is-stream-candidate")?.style.viewTransitionName || "";
        const updateCallbackDone = Promise.resolve().then(() => {
          update();
          const target = container.querySelector<HTMLElement>(".agent-trace-step.is-promoted")?.style.viewTransitionName || "";
          transitionNames.push({ source, target });
        });
        return { finished: updateCallbackDone, updateCallbackDone };
      },
    });
    (api.chat as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true } as Response);
    (parseSse as unknown as ReturnType<typeof vi.fn>).mockImplementation(async function* () {
      yield { event: "ready", data: { conversation_id: "conv-live" } };
      yield { event: "trace", data: { kind: "status", event: "ignored", message: "  " } };
      yield { event: "trace", data: { kind: "agent_message", message: "\n", round: 0 } };
      yield { event: "message", data: { delta: AGENT_PROGRESS, phase: "candidate", round: 1 } };
      yield { event: "trace", data: TRACE[1] };
      yield { event: "message", data: { delta: "最终", phase: "candidate", round: 2 } };
      await waitForFinish;
      yield { event: "message", data: { delta: "答案", phase: "candidate", round: 2 } };
      yield {
        event: "done",
        data: {
          app_id: application.id,
          conversation_id: "conv-live",
          message_id: "m-live",
          answer: "最终答案",
          route: "retrieval",
          retrieved_snippets: [],
          status_events: [],
          trace: [TRACE[1]],
          tool_calls: TOOL_CALLS,
          usage: null,
          ttft_ms: 2500,
        },
      };
    });
    act(() => root.render(<ChatHarness initialConversationId="conv-live" />));

    const textarea = container.querySelector<HTMLTextAreaElement>('textarea[name="message"]')!;
    const form = container.querySelector<HTMLFormElement>("form.composer")!;
    await act(async () => {
      textarea.value = "查营收";
      form.requestSubmit();
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    const liveTrace = container.querySelector<HTMLDetailsElement>(".agent-trace");
    const liveBubble = liveTrace?.closest(".message-body")?.querySelector(".message-text");
    expect(liveTrace?.open).toBe(true);
    expect(liveTrace?.classList.contains("is-pending")).toBe(true);
    expect(liveTrace?.querySelector("summary")?.textContent).toBe("正在处理");
    expect(liveTrace?.textContent).toContain(AGENT_PROGRESS);
    expect(liveTrace?.querySelectorAll(".agent-trace-step")).toHaveLength(1);
    expect(liveTrace?.querySelector(".agent-trace-step.is-agent-message")).not.toBeNull();
    expect(liveBubble?.textContent).toBe("最终");
    expect(transitionNames).toHaveLength(1);
    expect(transitionNames[0]?.source).not.toBe("");
    expect(transitionNames[0]?.target).toBe(transitionNames[0]?.source);

    await act(async () => {
      finishStream();
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    const completedTrace = container.querySelector<HTMLDetailsElement>(".agent-trace");
    const completedAnswer = completedTrace?.closest(".message-body")?.querySelector(".message-text");
    expect(completedAnswer).toBe(liveBubble);
    expect(completedTrace?.open).toBe(true);
    expect(completedTrace?.classList.contains("is-pending")).toBe(false);
    expect(completedAnswer?.textContent).toBe("最终答案");
  });

  it("renders details from server-persisted history without localStorage", async () => {
    const { api } = await import("../../api/client");
    (api.getConversationMessages as unknown as ReturnType<typeof vi.fn>).mockResolvedValue([
      { id: "u1", conversation_id: "conv-2", seq_no: 1, role: "user", content: "查营收", status: "completed", route: null, error_message: null, created_at: "", updated_at: "", sources: [] },
      { id: "m2", conversation_id: "conv-2", seq_no: 2, role: "assistant", content: "答案是", status: "completed", route: "retrieval", error_message: null, tool_calls: TOOL_CALLS, trace: TRACE, usage: { input_tokens: 100, output_tokens: 50, total_tokens: 150, cached_tokens: 0 }, ttft_ms: 2500, created_at: "", updated_at: "", sources: [] },
    ]);
    (api.listConversations as unknown as ReturnType<typeof vi.fn>).mockResolvedValue([]);

    act(() => root.render(<ChatHarness initialConversationId="conv-2" />));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });

    const traceDetails = container.querySelector<HTMLDetailsElement>(".agent-trace");
    const toolDetails = container.querySelector("details.message-details:not(.agent-trace)");
    expect(traceDetails?.textContent).toContain(AGENT_PROGRESS);
    expect(traceDetails?.open).toBe(false);
    expect(toolDetails?.querySelector("summary")?.textContent).toContain("详情 · 3 次工具调用 · 2 轮");
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

    expect(container.querySelector(".agent-trace")).toBeNull();
    expect(container.querySelector("details.message-details:not(.agent-trace)")).toBeNull();
  });
});
