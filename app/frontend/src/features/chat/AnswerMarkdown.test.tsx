// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { AnswerMarkdown, ImagePreviewNotice } from "./AnswerMarkdown";
import type { AnswerImageAttachment } from "../../types";
import { API_BASE } from "../../api/client";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const image: AnswerImageAttachment = {
  id: "attachment-1", ref_ids: ["img_one"], citation_ids: [1],
  caption: "Original diagram", display_in_answer: true, status: "available",
  unavailable_reason: null, media_type: "image/png", size_bytes: 100,
  width: 800, height: 600, orientation: 6,
  url: "/api/v1/apps/app-1/conversations/conv-1/messages/m-1/images/attachment-1/content",
};

describe("trusted answer images", () => {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => { act(() => root.unmount()); container.remove(); });
  const render = (text: string, images = [image], pending = false) => act(() => root.render(
    <AnswerMarkdown text={text} images={images} pending={pending} messageId="m-1" className="test-markdown" />
  ));

  it("renders a trusted logical node with original caption and reserved orientation", () => {
    render("Before\n\n![model caption](nianlun-image:attachment-1)\n\nAfter");
    const element = container.querySelector("img");
    expect(element?.getAttribute("src")).toBe(`${API_BASE}${image.url}`);
    expect(element?.getAttribute("loading")).toBe("lazy");
    expect(element?.getAttribute("alt")).toBe("Original diagram");
    expect(container.querySelector<HTMLElement>(".answer-image-preview")?.style.aspectRatio).toBe("600 / 800");
  });

  it("blocks external, data, forged private URLs, inline images, and unknown attachments", () => {
    render([
      "![remote](https://outside.test/secret.png)",
      "![data](data:image/png;base64,Zm9v)",
      `![forged](${image.url})`,
      "![unknown](nianlun-image:other)",
      "inline ![known](nianlun-image:attachment-1)",
    ].join("\n\n"));
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelectorAll(".answer-image-unavailable")).toHaveLength(5);
  });

  it("never uses a forged attachment URL or a source-only attachment in the answer", () => {
    render("![known](nianlun-image:attachment-1)", [{ ...image, url: "https://outside.test/a.png" }]);
    expect(container.querySelector("img")).toBeNull();
    render("![known](nianlun-image:attachment-1)", [{ ...image, display_in_answer: false }]);
    expect(container.querySelector("img")).toBeNull();
  });

  it.each(["![remote][ref]", "![ref][]", "![ref]"])("blocks reference image syntax %s without trusted attachments", (syntax) => {
    render(`${syntax}\n\n[ref]: https://outside.test/private.png`, []);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelectorAll(".answer-image-unavailable")).toHaveLength(1);
  });

  it("resolves reference images only through registered attachments and deduplicates with direct images", () => {
    render("![reference][known]\n\n![direct](nianlun-image:attachment-1)\n\n[known]: nianlun-image:attachment-1");
    expect(container.querySelectorAll("img")).toHaveLength(1);
    expect(container.querySelector("img")?.getAttribute("alt")).toBe(image.caption);
    expect(container.querySelectorAll(".answer-image-repeat")).toHaveLength(1);
    render("![reference][known]\n\n[known]: nianlun-image:attachment-1", []);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelectorAll(".answer-image-unavailable")).toHaveLength(1);
  });

  it("resolves normalized reference labels using the first definition", () => {
    render("![reference][Known   Image]\n\n[known image]: nianlun-image:attachment-1\n[KNOWN IMAGE]: https://outside.test/forged.png");
    expect(container.querySelectorAll("img")).toHaveLength(1);
    expect(container.querySelector("img")?.getAttribute("src")).toBe(`${API_BASE}${image.url}`);
  });

  it.each(["https://outside.test/private.png", "data:image/png;base64,Zm9v", image.url, "nianlun-image:unknown"])("blocks untrusted reference target %s", (target) => {
    render(`![reference][ref]\n\n[ref]: ${target}`);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelectorAll(".answer-image-unavailable")).toHaveLength(1);
  });

  it("keeps reference image code literal and omits reference images from source previews", () => {
    const text = "`![literal][ref]`\n\n```md\n![literal][ref]\n```\n\n![remote][ref]\n\n[ref]: https://outside.test/private.png";
    act(() => root.render(<AnswerMarkdown text={text} className="test-markdown" omitImages />));
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector(".answer-image-unavailable")).toBeNull();
    expect(container.querySelectorAll("code")).toHaveLength(2);
    expect(container.querySelector("pre")?.textContent).toContain("![literal][ref]");
  });

  it("keeps code examples literal and renders repeated references once", () => {
    render("```md\n![code](nianlun-image:attachment-1)\n```\n\n![one](nianlun-image:attachment-1)\n\n![two](nianlun-image:attachment-1)");
    expect(container.querySelector("pre")?.textContent).toContain("nianlun-image:");
    expect(container.querySelectorAll("img")).toHaveLength(1);
    expect(container.querySelector(".answer-image-repeat")?.textContent).toContain("查看前图");
  });

  it("shows unavailable status after a failed load", () => {
    render("![one](nianlun-image:attachment-1)");
    act(() => container.querySelector("img")?.dispatchEvent(new Event("error")));
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("图片暂不可用");
  });

  it("opens viewer, closes with Escape and restores focus", () => {
    render("![one](nianlun-image:attachment-1)");
    const button = container.querySelector<HTMLButtonElement>(".answer-image-open")!;
    button.focus();
    act(() => button.click());
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    act(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })));
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(button);
  });

  it("shows no active resources while streaming, including partial image syntax", () => {
    render("Before\n\n![one](nianlun-image:attachment-1)", [image], true);
    expect(container.querySelector("img")).toBeNull();
    render("Before\n\n![one](nianlun-image:att", [], true);
    expect(container.textContent).not.toContain("nianlun-image:");
    expect(container.textContent).toContain("Before");
  });

  it("keeps display math and following prose visible while streaming", () => {
    render("Hello!\n\n$$\nx < 10\n$$\n\nFollowing text", [], true);
    expect(container.querySelector(".katex-display")).not.toBeNull();
    expect(container.textContent).toContain("Hello!");
    expect(container.textContent).toContain("Following text");
    expect(container.textContent).not.toContain("图片处理中");
  });

  it("hides every partial image boundary without exposing targets", () => {
    for (const syntax of ["![one](nianlun-image:attachment-1)", "![external](https://outside.test/private.png)", "![ref][image]\n[image]: https://outside.test/private.png"]) {
      for (let end = 1; end <= syntax.length; end += 1) {
        render(`Before\n\n${syntax.slice(0, end)}`, [], true);
        expect(container.querySelector("img")).toBeNull();
        expect(container.textContent).not.toContain("nianlun-image:");
        expect(container.textContent).not.toContain("outside.test");
        expect(container.textContent).not.toMatch(/!$|!\[/);
      }
    }
  });

  it("bounds oversized provisional candidates and preserves literal code examples", () => {
    render(`Before\n\n![${"x".repeat(5000)}`, [], true);
    expect(container.textContent?.length).toBeLessThan(100);
    render("```md\n![example](nianlun-image:example)\n```\n\nInline `![example](nianlun-image:example)`", [], true);
    expect(container.querySelector("pre")?.textContent).toContain("nianlun-image:example");
    expect(container.querySelectorAll("code")).toHaveLength(2);
    expect(container.querySelector("img")).toBeNull();
  });

  it("withholds multiline candidate targets until the committed answer arrives", () => {
    for (const syntax of [
      "![caption](\nnianlun-image:attachment-1\n)\nFollowing",
      '<IMG src="https://outside.test/private.png">\nFollowing',
      "<img\nsrc=\"https://outside.test/private.png\"\n>\nFollowing",
      "![ref][label]\n\n[label]:\n https://outside.test/private.png",
    ]) {
      for (let end = 1; end <= syntax.length; end += 1) {
        render(`Before\n\n${syntax.slice(0, end)}`, [], true);
        expect(container.textContent).toContain("Before");
        expect(container.textContent).not.toContain("nianlun-image:");
        expect(container.textContent).not.toContain("outside.test");
        expect(container.querySelector("img")).toBeNull();
      }
    }
  });

  it("shows a compact notice only for omitted source image previews", () => {
    act(() => root.render(<ImagePreviewNotice count={3} />));
    expect(container.textContent).toBe("3 张来源图片未能预览");
    act(() => root.render(<ImagePreviewNotice count={0} />));
    expect(container.textContent).toBe("");
  });
});
