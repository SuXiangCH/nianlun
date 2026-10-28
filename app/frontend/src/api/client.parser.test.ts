// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { api } from "./client";

afterEach(() => vi.unstubAllGlobals());

it("posts an encoded document reparse command with the nullable OCR override", async () => {
  const document = { id: "doc/1", status: "uploaded" };
  const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ code: 0, data: document }), { status: 200 }));
  vi.stubGlobal("fetch", fetch);
  for (const isOcr of [null, false, true]) {
    expect(await api.reparseKnowledgeBaseDocument("kb/1", "doc/1", isOcr)).toEqual(document);
    expect(fetch).toHaveBeenLastCalledWith(expect.stringContaining("/knowledge-bases/kb%2F1/documents/doc%2F1/reparse"), expect.objectContaining({ method: "POST", body: JSON.stringify({ is_ocr: isOcr }) }));
  }
});

it("preserves the reparse error and request id", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ code: 409, message: "文档正在处理", data: null }), { status: 409, headers: { "X-Request-Id": "request-1" } })));
  await expect(api.reparseKnowledgeBaseDocument("kb", "doc", null)).rejects.toMatchObject({ message: "文档正在处理", status: 409, requestId: "request-1" });
});
