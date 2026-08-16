import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import "katex/dist/katex.min.css";
import { normalizeHtmlTables } from "../chat/markdown";
import type { DocumentIndexNode, DocumentIndexTree } from "../../types";

interface Props {
  title: string;
  content: string | null;
  contentLoading: boolean;
  contentError: string | null;
  tree: DocumentIndexTree | null;
  treeLoading: boolean;
  treeError: string | null;
  onClose: () => void;
}

const EXPANDED_DEPTH = 2;

interface HastNode {
  type: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
  position?: { start?: { line?: number } };
}

// Stamp each rendered heading with its source line so tree clicks can anchor
// into the rendered document; positions are exact unless table normalization
// rewrote lines above the heading, in which case jumps land on a nearby one.
const rehypeHeadingLines = () => (tree: HastNode) => {
  const walk = (node: HastNode) => {
    if (
      node.type === "element" &&
      typeof node.tagName === "string" &&
      /^h[1-6]$/.test(node.tagName) &&
      typeof node.position?.start?.line === "number"
    ) {
      node.properties = { ...(node.properties || {}), dataLine: node.position.start.line };
    }
    (node.children || []).forEach(walk);
  };
  walk(tree);
};

const collectDefaultCollapsed = (nodes: DocumentIndexNode[], depth: number, collapsed: Set<string>) => {
  for (const node of nodes) {
    if (node.nodes?.length && depth >= EXPANDED_DEPTH) collapsed.add(node.node_id);
    collectDefaultCollapsed(node.nodes || [], depth + 1, collapsed);
  }
};

const countNodes = (nodes: DocumentIndexNode[]): number =>
  nodes.reduce((total, node) => total + 1 + countNodes(node.nodes || []), 0);

interface TreeNodeProps {
  node: DocumentIndexNode;
  depth: number;
  collapsed: Set<string>;
  activeNodeId: string | null;
  onToggle: (nodeId: string) => void;
  onJump: (node: DocumentIndexNode) => void;
}

const TreeNode = ({ node, depth, collapsed, activeNodeId, onToggle, onJump }: TreeNodeProps) => {
  const hasChildren = Boolean(node.nodes?.length);
  const expanded = hasChildren && !collapsed.has(node.node_id);
  const summary = node.summary || node.prefix_summary;
  return (
    <li className="reader-tree-item">
      <div className={`reader-tree-row ${activeNodeId === node.node_id ? "is-active" : ""}`} style={{ paddingLeft: depth * 12 }}>
        {hasChildren ? (
          <button className="reader-tree-toggle" type="button" aria-label={expanded ? `收起 ${node.title}` : `展开 ${node.title}`} aria-expanded={expanded} onClick={() => onToggle(node.node_id)}>{expanded ? "▾" : "▸"}</button>
        ) : (
          <span className="reader-tree-toggle is-leaf" aria-hidden="true">·</span>
        )}
        <button className="reader-tree-link" type="button" title={summary || node.title} onClick={() => onJump(node)}>{node.title}</button>
      </div>
      {expanded && (
        <ul className="reader-tree-children">
          {(node.nodes || []).map((child) => (
            <TreeNode key={child.node_id} node={child} depth={depth + 1} collapsed={collapsed} activeNodeId={activeNodeId} onToggle={onToggle} onJump={onJump} />
          ))}
        </ul>
      )}
    </li>
  );
};

export function DocumentReader({
  title,
  content,
  contentLoading,
  contentError,
  tree,
  treeLoading,
  treeError,
  onClose,
}: Props) {
  const [mode, setMode] = useState<"rendered" | "source">("rendered");
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [activeNodeId, setActiveNodeId] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const flashTimer = useRef<number | null>(null);

  useEffect(() => {
    // The reader opens inline under its document row; nudge it into view once
    // so clicking a row near the viewport bottom still reveals the content.
    rootRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, []);

  useEffect(() => {
    const next = new Set<string>();
    collectDefaultCollapsed(tree?.structure || [], 0, next);
    setCollapsed(next);
    setActiveNodeId(null);
  }, [tree]);

  useEffect(() => () => {
    if (flashTimer.current !== null) window.clearTimeout(flashTimer.current);
  }, []);

  const flash = (target: Element) => {
    target.classList.add("is-flash");
    if (flashTimer.current !== null) window.clearTimeout(flashTimer.current);
    flashTimer.current = window.setTimeout(() => target.classList.remove("is-flash"), 1600);
  };

  const jumpTo = (node: DocumentIndexNode) => {
    setActiveNodeId(node.node_id);
    const body = bodyRef.current;
    if (!body) return;
    const anchors = Array.from(body.querySelectorAll<HTMLElement>("[data-line]"));
    // Untitled documents chunk without headings, so fall back to the nearest
    // heading at or above the node's line instead of requiring an exact match.
    let target: HTMLElement | null = null;
    for (const anchor of anchors) {
      const line = Number(anchor.dataset.line);
      if (line <= node.line_num) target = anchor;
      else break;
    }
    if (!target && anchors.length) target = anchors[0];
    if (target) {
      target.scrollIntoView({ behavior: "smooth", block: "start" });
      flash(target);
    }
  };

  const onToggle = (nodeId: string) => {
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(nodeId)) next.delete(nodeId);
      else next.add(nodeId);
      return next;
    });
  };

  const sourceLines = useMemo(() => (content === null ? [] : content.split("\n")), [content]);
  const nodeCount = tree ? countNodes(tree.structure) : 0;

  return (
    <div className="document-content-panel document-reader" ref={rootRef}>
      <div className="document-content-heading">
        <div>
          <div className="detail-kicker">解析内容与索引树</div>
          <h3>{title}</h3>
        </div>
        <div className="reader-heading-actions">
          <div className="reader-mode" role="group" aria-label="内容显示方式">
            <button className={`reader-mode-button ${mode === "rendered" ? "is-active" : ""}`} type="button" aria-pressed={mode === "rendered"} onClick={() => setMode("rendered")}>渲染</button>
            <button className={`reader-mode-button ${mode === "source" ? "is-active" : ""}`} type="button" aria-pressed={mode === "source"} onClick={() => setMode("source")}>源码</button>
          </div>
          <button className="quiet-button" onClick={onClose} type="button">关闭</button>
        </div>
      </div>
      <div className="reader-body">
        <aside className="reader-tree" aria-label="文档索引树">
          <div className="reader-tree-heading">
            <span>索引树</span>
            {tree && <span className="reader-tree-count">{nodeCount} 个节点</span>}
          </div>
          {treeLoading && <div className="reader-tree-state">正在加载索引树...</div>}
          {treeError && <div className="reader-tree-state is-error" role="alert">{treeError}</div>}
          {!treeLoading && !treeError && (tree && tree.structure.length ? (
            <ul className="reader-tree-children reader-tree-root">
              {tree.structure.map((node) => (
                <TreeNode key={node.node_id} node={node} depth={0} collapsed={collapsed} activeNodeId={activeNodeId} onToggle={onToggle} onJump={jumpTo} />
              ))}
            </ul>
          ) : <div className="reader-tree-state">这篇文档没有生成索引树。</div>)}
        </aside>
        <div className="reader-content" ref={bodyRef}>
          {contentLoading && <div className="reader-content-state">正在读取解析内容...</div>}
          {contentError && <div className="reader-content-state is-error" role="alert">{contentError}</div>}
          {content !== null && (mode === "rendered" ? (
            <div className="message-markdown reader-markdown">
              <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex, rehypeHeadingLines]}>{normalizeHtmlTables(content)}</ReactMarkdown>
            </div>
          ) : (
            <pre className="reader-source">{sourceLines.map((line, index) => (
              <div className="reader-source-line" data-line={index + 1} key={index}>{line || " "}</div>
            ))}</pre>
          ))}
        </div>
      </div>
    </div>
  );
}
