const escapeCell = (value: string) => value.replace(/\|/g, "\\|").replace(/\s+/g, " ").trim();

export interface NormalizedMarkdown {
  content: string;
  /** Maps each 1-based rendered line to its original Markdown source line. */
  sourceLineByRenderedLine: number[];
}

const tableToMarkdown = (html: string): string | null => {
  const document = new DOMParser().parseFromString(html, "text/html");
  const table = document.querySelector("table");
  if (!table) return null;
  const rows = Array.from(table.querySelectorAll("tr"))
    .map((row) => Array.from(row.children)
      .filter((cell) => cell.tagName === "TH" || cell.tagName === "TD")
      .map((cell) => escapeCell(cell.textContent || "")))
    .filter((cells) => cells.length > 0);
  if (rows.length === 0) return null;

  const width = Math.max(...rows.map((cells) => cells.length));
  const normalized = rows.map((cells) => [...cells, ...Array(width - cells.length).fill("")]);
  const [header, ...body] = normalized;
  const line = (cells: string[]) => `| ${cells.join(" | ")} |`;
  return [line(header), line(Array(width).fill("---")), ...body.map(line)].join("\n");
};

const sourceLineStarts = (text: string): number[] => {
  const starts = [0];
  for (let index = 0; index < text.length; index += 1) {
    if (text[index] === "\n") starts.push(index + 1);
  }
  return starts;
};

const lineForOffset = (lineStarts: number[], offset: number): number => {
  let low = 0;
  let high = lineStarts.length - 1;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    if (lineStarts[middle] <= offset) low = middle + 1;
    else high = middle - 1;
  }
  return high + 1;
};

export const normalizeHtmlTablesWithSourceLines = (text: string): NormalizedMarkdown => {
  if (!/<table\b/i.test(text) || typeof DOMParser === "undefined") {
    return {
      content: text,
      sourceLineByRenderedLine: text.split("\n").map((_line, index) => index + 1),
    };
  }

  const lineStarts = sourceLineStarts(text);
  const sourceLineByRenderedLine: number[] = [];
  let content = "";
  let lineHasSource = false;

  const append = (value: string, sourceOffset: number, isOriginalText: boolean) => {
    for (let index = 0; index < value.length; index += 1) {
      if (!lineHasSource) {
        sourceLineByRenderedLine.push(lineForOffset(
          lineStarts,
          isOriginalText ? sourceOffset + index : sourceOffset,
        ));
        lineHasSource = true;
      }
      if (value[index] === "\n") lineHasSource = false;
    }
    content += value;
  };

  const tablePattern = /<table\b[^>]*>[\s\S]*?<\/table>/gi;
  let cursor = 0;
  for (const match of text.matchAll(tablePattern)) {
    const offset = match.index ?? 0;
    append(text.slice(cursor, offset), cursor, true);
    const table = match[0];
    const markdown = tableToMarkdown(table);
    append(markdown ? `\n\n${markdown}\n\n` : table, offset, !markdown);
    cursor = offset + table.length;
  }
  append(text.slice(cursor), cursor, true);
  if (!lineHasSource) sourceLineByRenderedLine.push(lineForOffset(lineStarts, text.length));

  return { content, sourceLineByRenderedLine };
};

export const normalizeHtmlTables = (text: string): string =>
  normalizeHtmlTablesWithSourceLines(text).content;
