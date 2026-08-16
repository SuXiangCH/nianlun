// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatView } from "./ChatView";
import type { Application } from "../../types";

vi.mock("../../api/client", () => ({ api: { chat: vi.fn() } }));

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

const storageKey = (conversationId: string) => `nianlun.chat.${application.id}.${conversationId}`;
const storedMessages = (text: string) => ({ messages: [{ id: "message-1", role: "user", text }], sources: [] });

function renderChat(root: Root, container: HTMLElement, conversationId: string): void {
  act(() => {
    root.render(
      <ChatView
        apps={[application]}
        selectedAppId={application.id}
        onSelectApp={() => undefined}
        onNewConversation={() => undefined}
        resetToken={0}
        conversationId={conversationId}
        onConversationId={() => undefined}
        toast={() => undefined}
      />,
    );
  });
}

describe("ChatView conversation persistence", () => {
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

  it("loads the target conversation without overwriting it with the previous state", async () => {
    localStorage.setItem(storageKey("conversation-a"), JSON.stringify(storedMessages("旧会话")));
    localStorage.setItem(storageKey("conversation-b"), JSON.stringify(storedMessages("目标会话")));

    renderChat(root, container, "conversation-a");
    await act(async () => {
      root.render(
        <ChatView
          apps={[application]}
          selectedAppId={application.id}
          onSelectApp={() => undefined}
          onNewConversation={() => undefined}
          resetToken={0}
          conversationId="conversation-b"
          onConversationId={() => undefined}
          toast={() => undefined}
        />,
      );
    });

    expect(container.textContent).toContain("目标会话");
    expect(JSON.parse(localStorage.getItem(storageKey("conversation-b")) || "{}").messages[0].text).toBe("目标会话");
  });

  it("restores the saved conversation after the chat view is remounted", () => {
    localStorage.setItem(storageKey("conversation-a"), JSON.stringify(storedMessages("已保存消息")));
    renderChat(root, container, "conversation-a");

    act(() => root.unmount());
    root = createRoot(container);
    renderChat(root, container, "conversation-a");

    expect(container.textContent).toContain("已保存消息");
    expect(JSON.parse(localStorage.getItem(storageKey("conversation-a")) || "{}").messages[0].text).toBe("已保存消息");
  });
});
