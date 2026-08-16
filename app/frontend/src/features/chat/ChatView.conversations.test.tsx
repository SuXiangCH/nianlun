// @vitest-environment jsdom

import { act } from "react";
import { useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatView } from "./ChatView";
import type { Application } from "../../types";

// Marks this file as a React act environment so state updates flush synchronously in tests.
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

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

const chatStorageKey = (conversationId: string) => `nianlun.chat.${application.id}.${conversationId}`;
const indexKey = `nianlun.conversations.${application.id}`;
const stored = (text: string) => ({ messages: [{ id: "m1", role: "user" as const, text }], sources: [] });

// Stateful wrapper so clicking a conversation drives `conversationId` through
// `onConversationId`, the same way App.tsx does in production.
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

function renderChat(root: Root, container: HTMLElement, initialConversationId: string): void {
  act(() => {
    root.render(<ChatHarness initialConversationId={initialConversationId} />);
  });
}

function openConversationMenu(container: HTMLElement): void {
  act(() => {
    container.querySelector<HTMLButtonElement>('[aria-label="会话列表"]')?.click();
  });
}

describe("ChatView conversation list", () => {
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
    vi.restoreAllMocks();
  });

  it("backfills the list from existing stored chats and highlights the active one", () => {
    localStorage.setItem(chatStorageKey("conv-a"), JSON.stringify(stored("AAA内容")));
    localStorage.setItem(chatStorageKey("conv-b"), JSON.stringify(stored("BBB内容")));

    renderChat(root, container, "conv-a");
    openConversationMenu(container);

    const popover = container.querySelector(".conv-popover");
    expect(popover?.textContent).toContain("AAA内容");
    expect(popover?.textContent).toContain("BBB内容");
    expect(container.querySelector(".conv-item.is-active")?.textContent).toContain("AAA内容");
    // The backfill persisted the index so it need not be recomputed next mount.
    expect(JSON.parse(localStorage.getItem(indexKey) || "[]")).toHaveLength(2);
  });

  it("does not present a legacy cached pending answer as completed", () => {
    localStorage.setItem(chatStorageKey("conv-pending"), JSON.stringify({
      messages: [
        { id: "u1", role: "user", text: "问题" },
        { id: "a1", role: "assistant", text: "半截回答", pending: true },
      ],
      sources: [],
    }));

    renderChat(root, container, "conv-pending");

    expect(container.querySelector(".message.error")).not.toBeNull();
    expect(container.textContent).toContain("半截回答");
    expect(container.querySelector(".message-status")).toBeNull();
  });

  it("switches to the selected conversation and restores its messages", () => {
    localStorage.setItem(chatStorageKey("conv-a"), JSON.stringify(stored("AAA内容")));
    localStorage.setItem(chatStorageKey("conv-b"), JSON.stringify(stored("BBB内容")));
    localStorage.setItem(indexKey, JSON.stringify([
      { id: "conv-a", title: "AAA内容", updatedAt: 0 },
      { id: "conv-b", title: "BBB内容", updatedAt: 0 },
    ]));

    renderChat(root, container, "conv-a");
    expect(container.textContent).toContain("AAA内容");

    openConversationMenu(container);
    const itemB = [...container.querySelectorAll<HTMLElement>(".conv-item")].find((el) => el.textContent?.includes("BBB内容"));
    act(() => { itemB?.click(); });

    expect(container.textContent).toContain("BBB内容");
    expect(container.textContent).not.toContain("AAA内容");
  });

  it("deletes a conversation from the list and its stored messages", () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    localStorage.setItem(chatStorageKey("conv-a"), JSON.stringify(stored("AAA内容")));
    localStorage.setItem(chatStorageKey("conv-b"), JSON.stringify(stored("BBB内容")));
    localStorage.setItem(indexKey, JSON.stringify([
      { id: "conv-a", title: "AAA内容", updatedAt: 0 },
      { id: "conv-b", title: "BBB内容", updatedAt: 0 },
    ]));

    renderChat(root, container, "conv-a");
    openConversationMenu(container);
    const delB = [...container.querySelectorAll<HTMLElement>(".conv-item")].find((el) => el.textContent?.includes("BBB内容"))?.querySelector<HTMLButtonElement>(".conv-del");
    act(() => { delB?.click(); });

    const popover = container.querySelector(".conv-popover");
    expect(popover?.textContent).not.toContain("BBB内容");
    expect(JSON.parse(localStorage.getItem(indexKey) || "[]")).toHaveLength(1);
    expect(localStorage.getItem(chatStorageKey("conv-b"))).toBeNull();
  });

  it("does not delete when the confirm dialog is dismissed", () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    localStorage.setItem(chatStorageKey("conv-a"), JSON.stringify(stored("AAA内容")));
    localStorage.setItem(chatStorageKey("conv-b"), JSON.stringify(stored("BBB内容")));
    localStorage.setItem(indexKey, JSON.stringify([
      { id: "conv-a", title: "AAA内容", updatedAt: 0 },
      { id: "conv-b", title: "BBB内容", updatedAt: 0 },
    ]));

    renderChat(root, container, "conv-a");
    openConversationMenu(container);
    const delB = [...container.querySelectorAll<HTMLElement>(".conv-item")].find((el) => el.textContent?.includes("BBB内容"))?.querySelector<HTMLButtonElement>(".conv-del");
    act(() => { delB?.click(); });

    // 确认被取消后会话、索引、存储都应原样保留。
    expect(container.querySelector(".conv-popover")?.textContent).toContain("BBB内容");
    expect(JSON.parse(localStorage.getItem(indexKey) || "[]")).toHaveLength(2);
    expect(localStorage.getItem(chatStorageKey("conv-b"))).not.toBeNull();
  });
});
