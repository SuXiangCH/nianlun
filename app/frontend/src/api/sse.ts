import type { ApiEvent } from "../types";

export async function* parseSse(response: Response): AsyncGenerator<ApiEvent> {
  if (!response.ok) throw new Error(`流式请求失败（${response.status}）`);
  if (!response.body) throw new Error("服务未返回流式响应");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let eventName = "message";
  let dataLines: string[] = [];
  const dispatch = (): ApiEvent | null => {
    if (!dataLines.length) { eventName = "message"; return null; }
    const currentEventName = eventName;
    const rawData = dataLines.join("\n");
    dataLines = []; eventName = "message";
    try {
      return { event: currentEventName, data: JSON.parse(rawData) as Record<string, unknown> };
    } catch {
      return null;
    }
  };
  const consume = (line: string): ApiEvent | null => {
    if (line === "") return dispatch();
    if (line.startsWith(":")) return null;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    const value = separator < 0 ? "" : line.slice(separator + 1).replace(/^ /, "");
    if (field === "event") eventName = value || "message";
    if (field === "data") dataLines.push(value);
    return null;
  };
  while (true) {
    const chunk = await reader.read();
    buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
    const lines = buffer.split(/\r\n|\r|\n/);
    buffer = lines.pop() || "";
    for (const line of lines) { const event = consume(line); if (event) yield event; }
    if (chunk.done) break;
  }
  if (buffer) { const event = consume(buffer); if (event) yield event; }
  const finalEvent = dispatch();
  if (finalEvent) yield finalEvent;
}
