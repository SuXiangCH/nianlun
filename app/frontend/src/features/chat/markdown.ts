const escapeCell = (value: string) => value.replace(/\|/g, "\\|").replace(/\s+/g, " ").trim();

export interface MarkdownAstNode {
  type: string;
  value?: string;
  url?: string;
  identifier?: string;
  data?: { hName?: string; hProperties?: Record<string, unknown> };
  children?: MarkdownAstNode[];
}

// Source previews render their authorized attachments separately from raw text.
export const remarkOmitImages = () => (tree: MarkdownAstNode): void => {
  const visit = (node: MarkdownAstNode): void => {
    if (!node.children) return;
    node.children = node.children.filter((child) => child.type !== "image" && child.type !== "imageReference");
    node.children.forEach(visit);
  };
  visit(tree);
};

export const remarkAnswerImages = () => (tree: MarkdownAstNode): void => {
  const seen = new Set<string>();
  const definitions = new Map<string, string>();
  const collectDefinitions = (node: MarkdownAstNode): void => {
    if (node.type === "definition" && node.identifier && node.url) {
      const id = node.identifier.toUpperCase();
      if (!definitions.has(id)) definitions.set(id, node.url);
    }
    node.children?.forEach(collectDefinitions);
  };
  collectDefinitions(tree);
  const visit = (node: MarkdownAstNode, parent?: MarkdownAstNode, grandparent?: MarkdownAstNode): void => {
    if (node.type === "image" || node.type === "imageReference") {
      const standalone = parent?.type === "paragraph" && parent.children?.length === 1 && grandparent?.type === "root";
      const url = node.type === "image" ? node.url : definitions.get(node.identifier?.toUpperCase() || "");
      const target = standalone && url?.startsWith("nianlun-image:") ? url.slice("nianlun-image:".length) : "";
      node.type = "answerImage";
      node.data = { hName: "span", hProperties: { "data-answer-image-id": target, "data-answer-image-repeat": seen.has(target) ? "true" : "false" } };
      if (target) seen.add(target);
      delete node.url;
      delete node.identifier;
      delete node.value;
      node.children = [];
      return;
    }
    node.children?.forEach((child) => visit(child, node, parent));
  };
  visit(tree);
};

export const imageSafeStreamingText = (text: string): string => {
  let fence = "";
  const lines = text.split("\n");
  let frozen = false;
  return lines.map((line, index) => {
    if (frozen) return "";
    const marker = line.match(/^ {0,3}(`{3,}|~{3,})/);
    if (marker) {
      if (!fence) fence = marker[1];
      else if (marker[1][0] === fence[0] && marker[1].length >= fence.length) fence = "";
      return line;
    }
    if (fence) return line;
    // Inline code is literal. Keep it outside provisional resource detection.
    const visible = line.replace(/(`+)([^`]|(?!\1)`)*?\1/g, "");
    // Bare prefixes are ambiguous only at the unfinished end of the stream;
    // punctuation and comparisons in completed lines are ordinary text.
    const partialPrefix = index === lines.length - 1 && /^\s*(?:!|<(?:i|im)?)$/i.test(visible);
    const candidate = partialPrefix || /!\[|<img(?:[\s/>]|$)|^ {0,3}\[[^\]]+\]:/i.test(visible);
    if (!candidate) return line;
    // A target may continue on later lines. Keep the complete suffix provisional
    // until done supplies the authorized, normalized answer and attachments.
    frozen = true;
    return new TextEncoder().encode(line).length > 4096 ? "图片暂不可用" : "图片处理中";
  }).join("\n");
};

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
