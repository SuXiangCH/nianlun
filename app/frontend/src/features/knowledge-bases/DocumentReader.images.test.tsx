// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DocumentReader } from "./DocumentReader";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const firstId = "img_" + "a".repeat(24);
const secondId = "img_" + "b".repeat(24);
const imagePath = (id: string) => `/api/v1/knowledge-bases/kb/documents/doc/images/${id}/content?content_version=3`;
const image = (id: string) => ({ id, caption: `Diagram ${id}`, status: "available" as const, url: imagePath(id), width: 80, height: 60, orientation: 1 });

describe("document preview images", () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    vi.stubGlobal("NIANLUN_API_BASE", "http://127.0.0.1:8000");
    Element.prototype.scrollIntoView = vi.fn();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  const render = (rendered = `![First](nianlun-document-image:${firstId})\n\n![Second](nianlun-document-image:${secondId})`) => {
    act(() => root.render(<DocumentReader
      title="Document" content="# Original\n\n![First](images/original.png)"
      renderedContent={rendered} images={[image(firstId), image(secondId)]}
      contentLoading={false} contentError={null} tree={null} treeLoading={false} treeError={null} onClose={vi.fn()}
    />));
  };

  it("renders version-bound images in place and opens a navigable viewer", () => {
    render();
    const images = container.querySelectorAll(".reader-markdown img");
    expect(images).toHaveLength(2);
    expect(images[0].getAttribute("src")).toBe(`http://127.0.0.1:8000${imagePath(firstId)}`);
    expect(images[0].getAttribute("loading")).toBe("lazy");
    act(() => container.querySelector<HTMLButtonElement>("[aria-label='放大图片：First']")!.click());
    expect(document.querySelector("[role='dialog'] img")?.getAttribute("src")).toContain(firstId);
    act(() => document.querySelector<HTMLButtonElement>("[aria-label='下一张']")!.click());
    expect(document.querySelector("[role='dialog'] img")?.getAttribute("src")).toContain(secondId);
    act(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(document.querySelector("[role='dialog']")).toBeNull();
  });

  it("keeps the original Markdown in source mode", () => {
    render();
    act(() => Array.from(container.querySelectorAll("button")).find((item) => item.textContent === "源码")!.click());
    expect(container.querySelector(".reader-source")?.textContent).toContain("images/original.png");
    expect(container.querySelector(".reader-source")?.textContent).not.toContain("nianlun-document-image");
  });

  it("shows unavailable placeholders for unmapped, external and failed images", () => {
    render(`![Unknown](nianlun-document-image:missing)\n\n![Remote](https://remote.test/image.png)\n\n![First](nianlun-document-image:${firstId})`);
    expect(container.querySelectorAll("img")).toHaveLength(1);
    expect(container.textContent).toContain("图片暂不可用");
    act(() => container.querySelector("img")!.dispatchEvent(new Event("error")));
    expect(container.querySelectorAll("img")).toHaveLength(0);
    expect(container.querySelectorAll(".document-image-unavailable")).toHaveLength(3);
  });

  it("preserves every repeated image occurrence without a chat preview limit", () => {
    render(Array.from({ length: 12 }, (_, i) => `![Diagram ${i}](nianlun-document-image:${firstId})`).join("\n\n"));
    expect(container.querySelectorAll(".reader-markdown img")).toHaveLength(12);
  });
});
