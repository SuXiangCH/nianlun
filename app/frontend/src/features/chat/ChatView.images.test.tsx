// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../../api/client";
import { parseSse } from "../../api/sse";
import { ChatView } from "./ChatView";
import type { Application, AnswerImageAttachment } from "../../types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("../../api/client", async (original) => ({
  ...await original<typeof import("../../api/client")>(),
  api: { chat: vi.fn(), getConversationMessages: vi.fn(), listConversations: vi.fn() },
}));
vi.mock("../../api/sse", () => ({ parseSse: vi.fn() }));

const application: Application = { id: "app-1", name: "Agent", description: "", knowledge_base_id: "kb-1", llm_model_id: "llm-1", provider: "default", agent_version: "v0", search_mode: "scan", created_at: "", updated_at: "" };
const image = (index: number): AnswerImageAttachment => ({ id: `image-${index}`, ref_ids: [`img_${index}`], citation_ids: [1], caption: `来源图片 ${index}`, width: 100, height: 200, orientation: 1, status: "available", display_in_answer: true, unavailable_reason: null, media_type: "image/png", size_bytes: 100, url: `/api/v1/apps/app-1/conversations/conversation-1/messages/message-1/images/image-${index}/content` });
let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  vi.mocked(api.listConversations).mockResolvedValue([]);
  vi.mocked(api.getConversationMessages).mockResolvedValue([]);
});
afterEach(() => { act(() => root.unmount()); container.remove(); });
const render = async () => {
  await act(async () => root.render(<ChatView apps={[application]} selectedAppId={application.id} onSelectApp={() => undefined} onNewConversation={() => undefined} resetToken={0} conversationId="conversation-1" onConversationId={() => undefined} toast={() => undefined} />));
};
const legacyReferenceImage = "正文\n\n![legacy][ref]\n\n[ref]: https://outside.test/private.png";

it("blocks legacy reference images from server history without attachment metadata", async () => {
  vi.mocked(api.getConversationMessages).mockResolvedValue([{ id: "message-1", conversation_id: "conversation-1", role: "assistant", content: legacyReferenceImage, created_at: "", updated_at: "", seq_no: 1, status: "completed", route: null, error_message: null, sources: [] }]);
  await render();
  expect(container.querySelector(".message-markdown")?.textContent).toContain("正文");
  expect(container.querySelector(".message-markdown img")).toBeNull();
  expect(container.querySelector(".answer-image-unavailable")).not.toBeNull();
});

it.each([false, true])("blocks legacy reference images in local cached answers with stopped=%s", async (stopped) => {
  localStorage.setItem("nianlun.chat.app-1.conversation-1", JSON.stringify({ messages: [{ id: "message-1", role: "assistant", text: legacyReferenceImage, pending: false, stopped }], sources: [] }));
  vi.mocked(api.getConversationMessages).mockRejectedValue(new Error("History unavailable"));
  await render();
  expect(container.querySelector(".message-markdown")?.textContent).toContain("正文");
  expect(container.querySelector(".message-markdown img")).toBeNull();
  expect(container.querySelector(".answer-image-unavailable")).not.toBeNull();
  expect(Boolean(container.querySelector(".message-status"))).toBe(stopped);
});

it("blocks reference images in a completed answer when backend images are disabled", async () => {
  vi.mocked(api.chat).mockResolvedValue(new Response());
  vi.mocked(parseSse).mockImplementation(async function* () {
    yield { event: "done", data: { conversation_id: "conversation-1", message_id: "message-1", answer: legacyReferenceImage } };
  });
  await render();
  await act(async () => {
    container.querySelector<HTMLTextAreaElement>("textarea")!.value = "请解释";
    container.querySelector<HTMLFormElement>("form.composer")!.requestSubmit();
  });
  expect(container.querySelector(".message-markdown")?.textContent).toContain("正文");
  expect(container.querySelector(".message-markdown img")).toBeNull();
  expect(container.querySelector(".answer-image-unavailable")).not.toBeNull();
});

it("keeps all twenty answer images in order when history is loaded", async () => {
  const images = Array.from({ length: 20 }, (_, index) => image(index + 1));
  vi.mocked(api.getConversationMessages).mockResolvedValue([{ id: "message-1", conversation_id: "conversation-1", role: "assistant", content: images.map((item) => `![图](nianlun-image:${item.id})`).join("\n\n"), created_at: "", updated_at: "", seq_no: 1, status: "completed", route: null, error_message: null, sources: [], images, image_previews_unavailable: 3 }]);
  await render();
  expect([...container.querySelectorAll(".message-markdown img")].map((item) => item.getAttribute("alt"))).toEqual(images.map((item) => item.caption));
  expect(container.querySelector(".image-preview-notice")?.textContent).toBe("3 张来源图片未能预览");
  expect(container.querySelectorAll("img[loading=lazy]")).toHaveLength(20);
});

it.each(["available", "unavailable"] as const)("shows source images once with their actual %s status", async (status) => {
  const attachment = { ...image(1), display_in_answer: false, status, url: status === "available" ? image(1).url : null };
  const text = "Before\n\n![](chunks/0000/assets/images/original.jpg)\n\nMiddle\n\n![remote](https://outside.test/image.jpg)\n\nAfter\n\n`![example](literal.jpg)`";
  vi.mocked(api.getConversationMessages).mockResolvedValue([{
    id: "message-1", conversation_id: "conversation-1", role: "assistant", content: "回答 [1]",
    created_at: "", updated_at: "", seq_no: 1, status: "completed", route: null, error_message: null,
    sources: [{ message_id: "message-1", citation_id: 1, text, image_attachment_ids: [attachment.id] }], images: [attachment],
  }]);
  await render();
  act(() => container.querySelector<HTMLButtonElement>(".source-toggle")!.click());
  const source = container.querySelector(".source-item")!;
  expect(source.querySelectorAll(".source-markdown .answer-image-unavailable")).toHaveLength(0);
  expect(source.querySelector(".source-markdown img")).toBeNull();
  expect(source.querySelector(".source-markdown")?.textContent).toContain("Middle");
  expect(source.querySelector(".source-markdown code")?.textContent).toBe("![example](literal.jpg)");
  expect(source.querySelectorAll(".source-images img")).toHaveLength(status === "available" ? 1 : 0);
  expect(source.querySelectorAll(".source-images .answer-image-unavailable")).toHaveLength(status === "available" ? 0 : 1);
  expect(source.querySelector("img")?.getAttribute("src") || "").not.toContain("outside.test");
  expect(container.querySelector(".message-markdown img")).toBeNull();
});

it("shows preview omissions from done and persists them for reload", async () => {
  vi.mocked(api.chat).mockResolvedValue(new Response());
  vi.mocked(parseSse).mockImplementation(async function* () {
    yield { event: "ready", data: { conversation_id: "conversation-1" } };
    yield { event: "message", data: { delta: "正文" } };
    yield { event: "done", data: { conversation_id: "conversation-1", message_id: "message-1", answer: "正文", image_previews_unavailable: 2 } };
  });
  await render();
  await act(async () => {
    container.querySelector<HTMLTextAreaElement>("textarea")!.value = "请解释";
    container.querySelector<HTMLFormElement>("form.composer")!.requestSubmit();
  });
  expect(container.querySelector(".image-preview-notice")?.textContent).toBe("2 张来源图片未能预览");
  const stored = JSON.parse(localStorage.getItem("nianlun.chat.app-1.conversation-1") || "{}");
  expect(stored.messages.at(-1).image_previews_unavailable).toBe(2);
});

it("streams v0 text before done and activates images only after commit", async () => {
  let finish = () => {};
  const committed = new Promise<void>((resolve) => { finish = resolve; });
  vi.mocked(api.chat).mockResolvedValue(new Response());
  vi.mocked(parseSse).mockImplementation(async function* () {
    yield { event: "ready", data: { conversation_id: "conversation-1", answer_format_version: 2 } };
    yield { event: "message", data: { delta: "已经生成的", phase: "candidate", round: 1 } };
    yield { event: "message", data: { delta: "正文\n\n![原图](nianlun-image:img_01)", phase: "candidate", round: 1 } };
    await committed;
    yield { event: "done", data: { conversation_id: "conversation-1", message_id: "message-1", answer: "已经生成的正文\n\n![原图](nianlun-image:image-1)", images: [image(1)], answer_format_version: 2, answer_output_mode: "text_and_images" } };
  });
  await render();
  try {
    await act(async () => {
      container.querySelector<HTMLTextAreaElement>("textarea")!.value = "介绍一下并配图";
      container.querySelector<HTMLFormElement>("form.composer")!.requestSubmit();
      await new Promise((resolve) => setTimeout(resolve, 40));
    });
    expect(container.querySelector(".message-markdown")?.textContent).toContain("已经生成的正文");
    expect(container.querySelector('[aria-label="停止回答"]')).not.toBeNull();
    expect(container.textContent).not.toContain("nianlun-image:");
    expect(container.querySelector(".message-markdown img")).toBeNull();
  } finally {
    await act(async () => { finish(); });
  }
  expect(container.querySelector(".message-markdown img")?.getAttribute("alt")).toBe("来源图片 1");
  expect(container.querySelector(".message-markdown")?.textContent).toContain("已经生成的正文");
});

it.each([
  "正文\n\n![图](\nnianlun-image:img_01",
  "正文\n\n![图](https://outside.test/private.png)",
  "正文\n\n![图][ref]\n\n[ref]: https://outside.test/private.png",
  '正文\n\n<img\nsrc="https://outside.test/private.png"\n>',
])("retains safe placeholders after cancelling a provisional image answer: %s", async (text) => {
  vi.mocked(api.chat).mockResolvedValue(new Response());
  vi.mocked(parseSse).mockImplementation(async function* () {
    yield { event: "message", data: { delta: text } };
    await new Promise(() => undefined);
  });
  await render();
  await act(async () => {
    container.querySelector<HTMLTextAreaElement>("textarea")!.value = "请发图";
    container.querySelector<HTMLFormElement>("form.composer")!.requestSubmit();
    await new Promise((resolve) => setTimeout(resolve, 40));
  });
  expect(container.textContent).not.toContain("nianlun-image:");
  expect(container.textContent).not.toContain("outside.test");
  act(() => container.querySelector<HTMLButtonElement>('[aria-label="停止回答"]')!.click());
  expect(container.textContent).not.toContain("nianlun-image:");
  expect(container.textContent).not.toContain("outside.test");
  expect(container.textContent).toContain("正文");
  expect(container.textContent).toContain("已停止");
  expect(container.querySelector("img")).toBeNull();
  const stored = JSON.parse(localStorage.getItem("nianlun.chat.app-1.conversation-1") || "{}");
  expect(stored.messages.at(-1).text).not.toContain("nianlun-image:");
});

it.each([
  "Hello!\nThis is a normal answer.",
  "条件 x < 10。\n后续正文",
])("preserves ordinary streaming text after stop: %s", async (text) => {
  vi.mocked(api.chat).mockResolvedValue(new Response());
  vi.mocked(parseSse).mockImplementation(async function* () {
    yield { event: "message", data: { delta: text } };
    await new Promise(() => undefined);
  });
  await render();
  await act(async () => {
    container.querySelector<HTMLTextAreaElement>("textarea")!.value = "请回答";
    container.querySelector<HTMLFormElement>("form.composer")!.requestSubmit();
    await new Promise((resolve) => setTimeout(resolve, 40));
  });
  expect(container.querySelector(".message-markdown")?.textContent).toBe(text);
  act(() => container.querySelector<HTMLButtonElement>('[aria-label="停止回答"]')!.click());
  expect(container.querySelector(".message-markdown")?.textContent).toBe(text);
  expect(container.textContent).toContain("已停止");
});

it.each([
  ["Hello!\nThis is a normal answer.", "This is a normal answer."],
  ["条件 x < 10。\n后续正文", "后续正文"],
  ["正文\n\n![图](\nnianlun-image:img_01", "正文"],
])("recovers pending cached text safely as an interrupted answer: %s", async (text, visible) => {
  localStorage.setItem("nianlun.chat.app-1.conversation-1", JSON.stringify({ messages: [{ id: "message-1", role: "assistant", text, pending: true }], sources: [] }));
  vi.mocked(api.getConversationMessages).mockRejectedValue(new Error("History unavailable"));
  await render();
  expect(container.querySelector(".message-markdown")?.textContent).toContain(visible);
  expect(container.textContent).not.toContain("nianlun-image:");
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("article.message")?.classList.contains("error")).toBe(true);
});

it("discards provisional image targets when a stream fails", async () => {
  vi.mocked(api.chat).mockResolvedValue(new Response());
  vi.mocked(parseSse).mockImplementation(async function* () {
    yield { event: "message", data: { delta: '正文\n\n<img src="https://outside.test/private.png"' } };
    yield { event: "error", data: { message: "Stream failed" } };
  });
  await render();
  await act(async () => {
    container.querySelector<HTMLTextAreaElement>("textarea")!.value = "请发图";
    container.querySelector<HTMLFormElement>("form.composer")!.requestSubmit();
  });
  expect(container.querySelector(".message-markdown")?.textContent).toBe("Stream failed");
  expect(container.textContent).not.toContain("outside.test");
  expect(container.querySelector("img")).toBeNull();
});
