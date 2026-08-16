// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { ChatView } from "./ChatView";
import type { Application } from "../../types";

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

describe("ChatView retrieval snippets", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    localStorage.clear();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    localStorage.setItem(`nianlun.chat.${application.id}.conversation-1`, JSON.stringify({
      messages: [],
      sources: [{ id: "source-1", citation_id: 4, title: "产品文档", text: "## 关键结论\n\n- 支持 **Markdown**\n- 支持表格" }],
    }));
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it("renders retrieval snippets as Markdown", () => {
    act(() => {
      root.render(
        <ChatView
          apps={[application]}
          selectedAppId={application.id}
          onSelectApp={() => undefined}
          onNewConversation={() => undefined}
          resetToken={0}
          conversationId="conversation-1"
          onConversationId={() => undefined}
          toast={() => undefined}
        />,
      );
    });

    const snippet = container.querySelector(".source-markdown");
    expect(container.querySelector(".source-citation")?.textContent).toBe("[4]");
    expect(container.querySelector("#citation-4")).not.toBeNull();
    expect(snippet?.querySelector("h2")?.textContent).toBe("关键结论");
    expect(snippet?.querySelectorAll("li")).toHaveLength(2);
    expect(snippet?.querySelector("strong")?.textContent).toBe("Markdown");
  });

  it("links answer citations to the matching expanded source", () => {
    const source = { id: "source-1", message_id: "assistant-1", citation_id: 4, title: "产品文档", text: "引用正文" };
    localStorage.setItem(`nianlun.chat.${application.id}.conversation-1`, JSON.stringify({
      messages: [{ id: "assistant-1", role: "assistant", text: "结论由该片段支持[4]", sources: [source] }],
      sources: [source],
    }));
    act(() => {
      root.render(
        <ChatView
          apps={[application]}
          selectedAppId={application.id}
          onSelectApp={() => undefined}
          onNewConversation={() => undefined}
          resetToken={0}
          conversationId="conversation-1"
          onConversationId={() => undefined}
          toast={() => undefined}
        />,
      );
    });

    const citation = container.querySelector<HTMLAnchorElement>("a.citation-link");
    expect(citation?.textContent).toBe("[4]");
    expect(citation?.getAttribute("href")).toBe("#citation-4");

    act(() => citation?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true })));

    expect(container.querySelector("#citation-4")?.classList.contains("is-citation-active")).toBe(true);
    expect(container.querySelector("#citation-4 .source-toggle")?.textContent).toBe("收起片段");
  });

  it("renders HTML tables as Markdown tables", () => {
    localStorage.setItem(`nianlun.chat.${application.id}.conversation-1`, JSON.stringify({
      messages: [],
      sources: [{ id: "source-table", title: "规格", text: "<table><tr><td><strong>术语</strong></td><td><strong>说明</strong></td></tr><tr><td>额定电压</td><td>最大直流工作电压</td></tr></table>" }],
    }));
    act(() => {
      root.render(
        <ChatView
          apps={[application]}
          selectedAppId={application.id}
          onSelectApp={() => undefined}
          onNewConversation={() => undefined}
          resetToken={0}
          conversationId="conversation-1"
          onConversationId={() => undefined}
          toast={() => undefined}
        />,
      );
    });

    const table = container.querySelector(".source-markdown table");
    expect(table?.querySelectorAll("tr")).toHaveLength(2);
    expect(table?.textContent).toContain("额定电压");
  });
});
