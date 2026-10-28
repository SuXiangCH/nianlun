import { describe, expect, it } from "vitest";
import { imageSafeStreamingText } from "./markdown";

describe("streaming image filtering", () => {
  it.each([
    "Hello!\nThis is a normal answer.",
    "Hello!",
    "条件 x < 10。\n后续正文",
    "A < B and C > D",
    "!\nThis is a normal answer.",
    "<\nThis is a normal answer.",
    "$$\nx < 10\n$$\nFollowing text",
  ])("preserves ordinary text and math: %s", (text) => {
    expect(imageSafeStreamingText(text)).toBe(text);
  });
});
