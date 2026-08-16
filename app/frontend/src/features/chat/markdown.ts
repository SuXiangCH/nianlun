const escapeCell = (value: string) => value.replace(/\|/g, "\\|").replace(/\s+/g, " ").trim();

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

export const normalizeHtmlTables = (text: string): string => {
  if (!/<table\b/i.test(text) || typeof DOMParser === "undefined") return text;
  return text.replace(/<table\b[^>]*>[\s\S]*?<\/table>/gi, (table) => {
    const markdown = tableToMarkdown(table);
    return markdown ? `\n\n${markdown}\n\n` : table;
  });
};
