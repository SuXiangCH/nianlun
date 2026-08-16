import { describe, expect, it } from "vitest";
import { parseSse } from "./sse";

describe("parseSse", () => {
  it("parses CRLF, chunk boundaries, and multiline data", async () => {
    const chunks = [
      "event: message\r\ndata: {\"delta\":\"hel",
      "lo\"}\r\n\r\nevent: done\r\ndata: {\"answer\":",
      "\r\ndata: \"ab\"}\r\n\r\n",
    ];
    const response = new Response(new ReadableStream({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk));
        controller.close();
      },
    }));
    const events = [];
    for await (const event of parseSse(response)) events.push(event);
    expect(events).toEqual([
      { event: "message", data: { delta: "hello" } },
      { event: "done", data: { answer: "ab" } },
    ]);
  });

  it("skips malformed event data and continues with later events", async () => {
    const response = new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(
          "event: message\ndata: {not-json}\n\nevent: done\ndata: {\"answer\":\"ok\"}\n\n",
        ));
        controller.close();
      },
    }));

    const events = [];
    for await (const event of parseSse(response)) events.push(event);

    expect(events).toEqual([{ event: "done", data: { answer: "ok" } }]);
  });
});
