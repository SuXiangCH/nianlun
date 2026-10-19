// @vitest-environment jsdom

import { act, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { api } from "./api/client";
import type { Application } from "./types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const chat = vi.hoisted(() => ({
  renders: [] as Array<{
    selectedAppId: string;
    conversationId: string;
    onConversationId: (id: string) => void;
    onNewConversation: () => void;
    onSelectApp: (id: string) => void;
  }>,
}));

vi.mock("./api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("./api/client")>();
  return {
    ...original,
    api: {
      ...original.api,
      listApps: vi.fn(),
      listKnowledgeBases: vi.fn().mockResolvedValue([]),
      listModels: vi.fn().mockResolvedValue([]),
      deleteApp: vi.fn().mockResolvedValue(null),
    },
  };
});

vi.mock("./features/chat/ChatView", () => ({
  ChatView: (props: (typeof chat.renders)[number]) => {
    chat.renders.push(props);
    return <div data-testid="chat">{props.selectedAppId}:{props.conversationId}</div>;
  },
}));

vi.mock("./features/applications/ApplicationView", () => ({
  ApplicationView: ({ apps, onDelete }: { apps: Application[]; onDelete: (id: string) => void }) => (
    <div>{apps.map((app) => <button key={app.id} onClick={() => onDelete(app.id)}>删除 {app.id}</button>)}</div>
  ),
}));

const app = (id: string): Application => ({
  id, name: id, description: "", knowledge_base_id: "kb", llm_model_id: "llm",
  provider: "default", agent_version: "v0", search_mode: "fts", created_at: "", updated_at: "",
});

describe("Agent and conversation selection", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    localStorage.clear();
    window.location.hash = "chat";
    chat.renders.length = 0;
    vi.mocked(api.listApps).mockResolvedValue([app("a"), app("b")]);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.restoreAllMocks();
    vi.clearAllMocks();
  });

  const mount = async () => {
    await act(async () => root.render(<StrictMode><App /></StrictMode>));
  };
  const currentChat = () => chat.renders[chat.renders.length - 1];

  it("never renders a fallback Agent with the removed Agent's conversation", async () => {
    localStorage.setItem("nianlun.selectedAppId", "removed");
    localStorage.setItem("nianlun.activeConversation.removed", "conversation-removed");
    localStorage.setItem("nianlun.activeConversation.a", "conversation-a");
    await mount();

    expect(chat.renders.length).toBeGreaterThan(0);
    expect(chat.renders.every((props) => props.selectedAppId === "a" && props.conversationId === "conversation-a")).toBe(true);
    expect(localStorage.getItem("nianlun.activeConversation.removed")).toBe("conversation-removed");
  });

  it("restores each Agent's conversation without resetting the previous Agent", async () => {
    localStorage.setItem("nianlun.selectedAppId", "a");
    localStorage.setItem("nianlun.activeConversation.a", "conversation-a");
    localStorage.setItem("nianlun.activeConversation.b", "conversation-b");
    await mount();
    act(() => currentChat().onSelectApp("b"));
    expect(currentChat().conversationId).toBe("conversation-b");
    act(() => currentChat().onSelectApp("a"));
    expect(currentChat().conversationId).toBe("conversation-a");
    expect(localStorage.getItem("nianlun.activeConversation.b")).toBe("conversation-b");
  });

  it("ignores a previous Agent's late conversation callback", async () => {
    localStorage.setItem("nianlun.selectedAppId", "a");
    localStorage.setItem("nianlun.activeConversation.a", "conversation-a");
    localStorage.setItem("nianlun.activeConversation.b", "conversation-b");
    await mount();
    const previous = currentChat();
    act(() => previous.onSelectApp("b"));
    act(() => previous.onConversationId("late-conversation-a"));
    expect(currentChat().selectedAppId).toBe("b");
    expect(currentChat().conversationId).toBe("conversation-b");
    expect(localStorage.getItem("nianlun.activeConversation.b")).toBe("conversation-b");
    act(() => previous.onNewConversation());
    expect(currentChat().conversationId).toBe("conversation-b");
  });

  it("uses a fresh conversation when the fallback Agent has no saved conversation", async () => {
    localStorage.setItem("nianlun.selectedAppId", "removed");
    localStorage.setItem("nianlun.activeConversation.removed", "conversation-removed");
    await mount();
    expect(chat.renders.every((props) => props.conversationId !== "conversation-removed")).toBe(true);
    expect(currentChat().conversationId).toBeTruthy();
    expect(localStorage.getItem("nianlun.activeConversation.a")).toBe(currentChat().conversationId);
  });

  it("preserves the remaining Agent's saved conversation after deleting the selected Agent", async () => {
    localStorage.setItem("nianlun.selectedAppId", "a");
    localStorage.setItem("nianlun.activeConversation.a", "conversation-a");
    localStorage.setItem("nianlun.activeConversation.b", "conversation-b");
    await mount();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const agentNav = Array.from(container.querySelectorAll<HTMLButtonElement>(".nav-item")).find((button) => button.textContent?.includes("Agent"))!;
    act(() => agentNav.click());
    vi.mocked(api.listApps).mockResolvedValue([app("b")]);
    const remove = Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "删除 a")!;
    await act(async () => remove.click());
    const chatNav = Array.from(container.querySelectorAll<HTMLButtonElement>(".nav-item")).find((button) => button.textContent?.includes("对话"))!;
    act(() => chatNav.click());
    expect(currentChat().selectedAppId).toBe("b");
    expect(currentChat().conversationId).toBe("conversation-b");
    expect(localStorage.getItem("nianlun.activeConversation.a")).toBeNull();
  });
});
