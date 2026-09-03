import { useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import "katex/dist/katex.min.css";
import { api } from "../../api/client";
import { parseSse } from "../../api/sse";
import type { AgentTraceStep, Application, ChatDoneEvent, ChatMessage, ClarificationRequest, ConversationMessageRecord, ConversationSummary, SourceSnippet, TokenUsage, ToolCall } from "../../types";
import { normalizeHtmlTables } from "./markdown";

interface ChatViewProps { apps: Application[]; selectedAppId: string; onSelectApp: (id: string) => void; onNewConversation: () => void; resetToken: number; conversationId: string; onConversationId: (id: string) => void; toast: (message: string, error?: boolean) => void; }
interface MarkdownAstNode { type: string; value?: string; url?: string; data?: { hProperties?: Record<string, unknown> }; children?: MarkdownAstNode[]; }
const sourceName = (source: SourceSnippet) => String(source.title || source.doc_name || source.document_name || source.filename || source.source || "知识库片段");
const sourceText = (source: SourceSnippet) => String(source.text || source.retrieved_text || source.content || source.snippet || source.page_content || "暂无片段内容");
const sourceLocation = (source: SourceSnippet): string => String(source.doc_name || source.document_name || source.filename || source.path || source.heading || source.chunk_id || source.doc_id || source.id || "知识库");
const sourceCitation = (source: SourceSnippet, index: number): number => {
  if (Number.isInteger(source.citation_id) && Number(source.citation_id) > 0) return Number(source.citation_id);
  if (Number.isInteger(source.source_order) && Number(source.source_order) >= 0) return Number(source.source_order) + 1;
  return index + 1;
};
const remarkCitationLinks = () => (tree: MarkdownAstNode): void => {
  const visit = (node: MarkdownAstNode, protectedNode = false): void => {
    if (!node.children) return;
    const nextChildren: MarkdownAstNode[] = [];
    const protectsChildren = protectedNode || node.type === "link" || node.type === "code" || node.type === "inlineCode";
    for (const child of node.children) {
      if (child.type !== "text" || protectsChildren || !child.value) {
        visit(child, protectsChildren);
        nextChildren.push(child);
        continue;
      }
      const pattern = /\[(\d+)\]/g;
      let cursor = 0;
      let match: RegExpExecArray | null;
      while ((match = pattern.exec(child.value)) !== null) {
        if (match.index > cursor) nextChildren.push({ type: "text", value: child.value.slice(cursor, match.index) });
        const citationId = Number(match[1]);
        nextChildren.push({
          type: "link",
          url: `#citation-${citationId}`,
          data: { hProperties: { className: ["citation-link"], "data-citation-id": citationId, title: `查看来源 [${citationId}]` } },
          children: [{ type: "text", value: match[0] }],
        });
        cursor = pattern.lastIndex;
      }
      if (cursor === 0) nextChildren.push(child);
      else if (cursor < child.value.length) nextChildren.push({ type: "text", value: child.value.slice(cursor) });
    }
    node.children = nextChildren;
  };
  visit(tree);
};
const chatStorageKey = (applicationId: string, conversationId: string) => `nianlun.chat.${applicationId}.${conversationId}`;
const MarkdownContent = ({ text, className, onCitationClick }: { text: string; className: string; onCitationClick?: (citationId: number) => void }) => (
  <div className={className} onClick={(event) => {
    if (!onCitationClick || !(event.target instanceof Element)) return;
    const link = event.target.closest<HTMLAnchorElement>("a.citation-link");
    if (!link) return;
    const citationId = Number(link.dataset.citationId);
    if (!Number.isInteger(citationId) || citationId <= 0) return;
    event.preventDefault();
    onCitationClick(citationId);
  }}>
    <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath, ...(onCitationClick ? [remarkCitationLinks] : [])]} rehypePlugins={[rehypeKatex]}>{normalizeHtmlTables(text)}</ReactMarkdown>
  </div>
);
const readStoredChat = (applicationId: string, conversationId: string): { messages: ChatMessage[]; sources: SourceSnippet[] } | null => {
  if (!applicationId || !conversationId) return null;
  const storageKey = chatStorageKey(applicationId, conversationId);
  try {
    const raw = localStorage.getItem(storageKey);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { messages?: ChatMessage[]; sources?: SourceSnippet[] };
    return {
      messages: Array.isArray(parsed.messages) ? parsed.messages.map((message) => {
        if (message.pending && message.role === "assistant") {
          return {
            ...message,
            pending: false,
            error: true,
            text: message.text || "回答未完成，请重试",
          };
        }
        return { ...message, pending: false };
      }) : [],
      sources: Array.isArray(parsed.sources) ? parsed.sources : [],
    };
  } catch {
    return null;
  }
};
const sourceItemKey = (source: SourceSnippet, index: number) => `${source.id || sourceLocation(source)}-${index}`;
const mapConversation = (item: { id: string; title: string; updated_at: string }): ConversationSummary => ({
  id: item.id,
  title: item.title || "新对话",
  updatedAt: Date.parse(item.updated_at) || 0,
});
const mapMessage = (item: ConversationMessageRecord): ChatMessage => ({
  id: item.id,
  role: item.role,
  text: item.status === "failed" ? item.error_message || "对话请求失败" : item.content,
  pending: item.status === "pending",
  error: item.status === "failed",
  tool_calls: item.tool_calls ?? null,
  trace: item.trace ?? null,
  usage: item.usage ?? null,
  ttft_ms: item.ttft_ms ?? null,
  sources: item.sources || [],
});
const TOOL_LABELS: Record<string, string> = {
  search_across_docs: "搜索知识库",
  search_document_nodes: "搜索知识库",
  find_relevant_documents: "语义检索文档",
  get_document: "读取文档信息",
  get_structure_outline: "查看目录结构",
  get_line_content: "读取正文",
};
const formatElapsed = (ms?: number | null): string => {
  if (ms == null || ms < 0) return "";
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
};
const formatToolArgs = (args?: Record<string, unknown>): string =>
  Object.entries(args || {})
    .filter(([, value]) => value != null && value !== "")
    .map(([key, value]) => {
      const text = String(value);
      return `${key}=${text.length > 60 ? `${text.slice(0, 60)}…` : text}`;
    })
    .join("，");
const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
type ViewTransitionDocument = Document & {
  startViewTransition?: (update: () => void) => {
    finished: Promise<void>;
    updateCallbackDone?: Promise<void>;
  };
};
const commitWithViewTransition = async (update: () => void): Promise<void> => {
  const startViewTransition = (document as ViewTransitionDocument).startViewTransition;
  const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  if (!startViewTransition || reduceMotion) {
    update();
    return;
  }
  let updated = false;
  try {
    const transition = startViewTransition.call(document, () => {
      updated = true;
      flushSync(update);
    });
    void transition.finished.catch(() => undefined);
    await (transition.updateCallbackDone || Promise.resolve()).catch(() => undefined);
  } catch {
    if (!updated) flushSync(update);
  }
};
const parseTraceStep = (value: unknown): AgentTraceStep | null => {
  if (!isRecord(value)) return null;
  if (value.kind === "status" && typeof value.event === "string" && typeof value.message === "string") {
    const message = value.message.trim();
    if (!message) return null;
    return { kind: "status", event: value.event.trim() || "status", message };
  }
  if (value.kind === "agent_message" && typeof value.message === "string") {
    const message = value.message.trim();
    if (!message) return null;
    return { kind: "agent_message", message, round: typeof value.round === "number" ? value.round : undefined };
  }
  return null;
};
const formatUsage = (usage: TokenUsage, ttftMs?: number | null): string => {
  const n = (v: number): string => v.toLocaleString();
  const parts = [`输入 ${n(usage.input_tokens)}`, `输出 ${n(usage.output_tokens)}`, `共 ${n(usage.total_tokens)}`];
  if (usage.cached_tokens > 0) parts.push(`缓存 ${n(usage.cached_tokens)}`);
  if (ttftMs != null && ttftMs >= 0) parts.push(`首字 ${(ttftMs / 1000).toFixed(1)}s`);
  return parts.join(" · ");
};
const writeStoredChat = (storageKey: string, messages: ChatMessage[], sources: SourceSnippet[]): void => {
  if (!storageKey) return;
  const snapshot = {
    // Do not turn an in-flight assistant message into a completed-looking
    // history item. The user message remains available for a retry; the
    // unfinished assistant message is reconstructed only while the request is
    // active. A manually stopped answer (partial text) is also not persisted —
    // the backend marks the interrupted turn failed, and reloading it would
    // surface as an error bubble.
    messages: messages.filter((message) => !message.pending && !message.stopped).map(({ keepTraceOpen: _keepTraceOpen, streamRound: _streamRound, streamTransitionName: _streamTransitionName, ...message }) => ({ ...message, pending: false })),
    sources,
  };
  try {
    localStorage.setItem(storageKey, JSON.stringify(snapshot));
  } catch {
    // Local persistence is best effort; chat should continue if storage is unavailable.
  }
};
const conversationIndexKey = (applicationId: string) => `nianlun.conversations.${applicationId}`;
const readConversations = (applicationId: string): ConversationSummary[] => {
  if (!applicationId) return [];
  try {
    const raw = localStorage.getItem(conversationIndexKey(applicationId));
    if (!raw) return [];
    const parsed = JSON.parse(raw) as ConversationSummary[];
    return Array.isArray(parsed) ? parsed.filter((item) => item && item.id) : [];
  } catch {
    return [];
  }
};
const writeConversations = (applicationId: string, list: ConversationSummary[]): void => {
  if (!applicationId) return;
  try {
    localStorage.setItem(conversationIndexKey(applicationId), JSON.stringify(list));
  } catch {
    // Best effort; conversation switching still works in-memory if storage is unavailable.
  }
};
const titleFromStored = (applicationId: string, conversationId: string): string => {
  const stored = readStoredChat(applicationId, conversationId);
  const firstUser = stored?.messages.find((message) => message.role === "user");
  return (firstUser?.text || "新对话").slice(0, 40) || "新对话";
};
// One-time backfill: if an app has no index yet but does have persisted chats (e.g. from
// before the conversation list existed), seed the index from those storage keys so the user
// can finally reopen them.
const ensureIndexMigrated = (applicationId: string): ConversationSummary[] => {
  const existing = readConversations(applicationId);
  if (existing.length) return existing;
  const prefix = `nianlun.chat.${applicationId}.`;
  const backfilled: ConversationSummary[] = [];
  try {
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i);
      if (!key || !key.startsWith(prefix)) continue;
      const id = key.slice(prefix.length);
      if (!id) continue;
      backfilled.push({ id, title: titleFromStored(applicationId, id), updatedAt: 0 });
    }
  } catch {
    return [];
  }
  if (backfilled.length) {
    backfilled.sort((a, b) => a.title.localeCompare(b.title));
    writeConversations(applicationId, backfilled);
  }
  return backfilled;
};
const timeAgo = (ts: number): string => {
  if (!ts) return "";
  const diff = Date.now() - ts;
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`;
  try {
    return new Date(ts).toLocaleDateString();
  } catch {
    return "";
  }
};

function AgentTraceDetails({ trace, pending, keepOpen }: { trace?: AgentTraceStep[] | null; pending: boolean; keepOpen?: boolean }) {
  const steps = trace || [];
  const [open, setOpen] = useState(pending || Boolean(keepOpen));
  // Render as soon as the turn is pending, even before the first step arrives:
  // the panel itself (with a typing indicator) is the "processing" affordance.
  if (!steps.length && !pending) return null;
  return (
    <details className={`message-details agent-trace ${pending ? "is-pending" : ""}`} open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>
        <span className="agent-trace-mark" aria-hidden="true"><i /><i /><i /></span>
        <span>{pending ? "正在处理" : "处理过程"}</span>
      </summary>
      <div className="agent-trace-list" role="list">
        {pending && !steps.length ? <div className="agent-trace-step is-status" role="listitem"><span className="agent-trace-node" aria-hidden="true" /><span className="typing" aria-hidden="true"><i /><i /><i /></span></div> : null}
        {steps.map((step, index) => {
          if (step.kind === "status") {
            return <div className={`agent-trace-step is-status ${step.event.endsWith("_failed") ? "is-failed" : ""}`} role="listitem" key={`${step.event}-${index}`}><span className="agent-trace-node" aria-hidden="true" /><span className="agent-trace-copy">{step.message}</span></div>;
          }
          return <div className={`agent-trace-step is-agent-message ${step.transitionName ? "is-promoted" : ""}`} role="listitem" key={step.round != null ? `agent-message-${step.round}` : `agent-message-${index}`} style={step.transitionName ? { viewTransitionName: step.transitionName } : undefined}><span className="agent-trace-node" aria-hidden="true" /><span className="agent-trace-copy">{step.message}</span></div>;
        })}
      </div>
    </details>
  );
}

function ToolCallDetails({ toolCalls }: { toolCalls: ToolCall[] }) {
  // batch 相同的是模型同一轮响应里（可能并行）发出的调用；按出现顺序连续分组。
  const groups: { batch: number | null; calls: ToolCall[] }[] = [];
  for (const call of toolCalls) {
    const batch = call.batch ?? null;
    const last = groups[groups.length - 1];
    if (last && last.batch === batch) last.calls.push(call);
    else groups.push({ batch, calls: [call] });
  }
  const showBatchLabels = groups.length > 1 || groups.some((group) => group.calls.length > 1);
  return (
    <details className="message-details">
      <summary>详情 · {toolCalls.length} 次工具调用{groups.length > 1 ? ` · ${groups.length} 轮` : ""}</summary>
      <div className="tool-call-list">
        {groups.map((group, groupIndex) => (
          <div className="tool-call-batch" key={groupIndex}>
            {showBatchLabels && group.batch != null ? (
              <div className="tool-call-batch-label">
                第 {group.batch} 轮{group.calls.length > 1 ? ` · 并行 ${group.calls.length} 个` : ""}
              </div>
            ) : null}
            <ul>
              {group.calls.map((call, index) => {
                const argsText = formatToolArgs(call.args);
                const elapsedText = formatElapsed(call.elapsed_ms);
                return (
                  <li key={index}>
                    <span className="tool-call-name">{TOOL_LABELS[call.name] || call.name}</span>
                    {argsText ? <span className="tool-call-args" title={argsText}>{argsText}</span> : null}
                    {elapsedText ? <span className="tool-call-elapsed">{elapsedText}</span> : null}
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </div>
    </details>
  );
}

export function ChatView({ apps, selectedAppId, onSelectApp, onNewConversation, resetToken, conversationId, onConversationId, toast }: ChatViewProps) {
  const selected = apps.find((item) => item.id === selectedAppId) || apps[0];
  const applicationId = selected?.id || selectedAppId;
  const storageKey = applicationId && conversationId ? chatStorageKey(applicationId, conversationId) : "";
  const initialChat = readStoredChat(applicationId, conversationId);
  const [messages, setMessages] = useState<ChatMessage[]>(initialChat?.messages || []);
  const [sources, setSources] = useState<SourceSnippet[]>(initialChat?.sources || []);
  const [expandedSources, setExpandedSources] = useState<Record<string, boolean>>({});
  const [activeCitation, setActiveCitation] = useState<number | null>(null);
  const [pendingCitation, setPendingCitation] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [clarificationEnabled, setClarificationEnabled] = useState(true);
  const [conversations, setConversations] = useState<ConversationSummary[]>(() => ensureIndexMigrated(applicationId));
  const [showConversations, setShowConversations] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const requestVersion = useRef(0);
  const scrollRef = useRef<HTMLDivElement>(null);
  const loadedStorageKey = useRef(storageKey);
  const conversationIdRef = useRef(conversationId);
  const migratedStorageKey = useRef<string | null>(null);
  const persistenceReady = useRef(false);
  const citationHighlightTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Set to the conversation id once a stream's `done` event has been applied
  // locally; the history loader then skips the redundant refetch that would
  // otherwise remount every bubble right after the answer lands.
  const streamSyncedConversation = useRef<string | null>(null);
  // Whether the user is currently near the bottom of the transcript; streaming
  // updates only auto-scroll while this holds, so reading earlier messages is
  // not interrupted.
  const stickToBottom = useRef(true);

  useEffect(() => {
    const toggleSourceFromCard = (event: MouseEvent) => {
      const target = event.target;
      if (!(target instanceof Element) || target.closest("button, a, input, select, textarea")) return;
      const item = target.closest<HTMLElement>(".source-item");
      const list = item?.parentElement;
      if (!item || !list?.classList.contains("source-list")) return;
      const index = Array.from(list.children).indexOf(item);
      const source = sources[index];
      if (!source) return;
      const key = sourceItemKey(source, index);
      setExpandedSources((current) => ({ ...current, [key]: !current[key] }));
    };
    document.addEventListener("click", toggleSourceFromCard);
    return () => document.removeEventListener("click", toggleSourceFromCard);
  }, [sources]);
  const persistedStorageKey = useRef(storageKey);
  const prevResetToken = useRef(resetToken);
  const busyRef = useRef(false);
  conversationIdRef.current = conversationId;
  busyRef.current = busy;
  const latestChat = useRef({ storageKey, messages, sources });
  latestChat.current = { storageKey, messages, sources };
  useEffect(() => () => {
    controller.current?.abort();
    if (citationHighlightTimer.current) clearTimeout(citationHighlightTimer.current);
  }, []);
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    // Follow the stream only while the user is already near the bottom (or has
    // just sent a message); otherwise preserve their reading position.
    const lastIsUser = messages[messages.length - 1]?.role === "user";
    if (stickToBottom.current || lastIsUser) el.scrollTop = el.scrollHeight;
  }, [messages]);
  useEffect(() => {
    if (pendingCitation === null) return;
    document.getElementById(`citation-${pendingCitation}`)?.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
    setPendingCitation(null);
    if (citationHighlightTimer.current) clearTimeout(citationHighlightTimer.current);
    const highlightedCitation = pendingCitation;
    citationHighlightTimer.current = setTimeout(() => {
      setActiveCitation((current) => current === highlightedCitation ? null : current);
    }, 1600);
  }, [pendingCitation, sources]);
  useEffect(() => {
    // Only clear when resetToken actually changes (e.g. workspace refresh). Comparing
    // against the previous value also makes this mount-safe under React <StrictMode>,
    // which double-invokes effects on mount and would otherwise treat the second
    // invocation as a reset, wiping the conversation just restored from storage.
    if (prevResetToken.current === resetToken) return;
    prevResetToken.current = resetToken;
    requestVersion.current += 1;
    controller.current?.abort();
    streamSyncedConversation.current = null;
    setBusy(false);
    setMessages([]);
    setSources([]);
    setActiveCitation(null);
  }, [resetToken]);
  useEffect(() => {
    if (!storageKey || loadedStorageKey.current === storageKey) return;
    loadedStorageKey.current = storageKey;
    if (migratedStorageKey.current === storageKey) {
      migratedStorageKey.current = null;
      return;
    }
    const stored = readStoredChat(applicationId, conversationId);
    setMessages(stored?.messages || []);
    setSources(stored?.sources || []);
    setExpandedSources({});
    setActiveCitation(null);
  }, [applicationId, conversationId, selectedAppId, storageKey]);
  useEffect(() => {
    if (!storageKey) return;
    if (!persistenceReady.current) {
      persistenceReady.current = true;
      return;
    }
    if (persistedStorageKey.current !== storageKey) {
      persistedStorageKey.current = storageKey;
      return;
    }
    writeStoredChat(storageKey, messages, sources);
  }, [messages, sources, storageKey]);
  // Reload the conversation index when the active app changes. Idempotent under <StrictMode>
  // (a second invocation reads the now-populated index and returns it unchanged).
  useEffect(() => { setConversations(ensureIndexMigrated(applicationId)); }, [applicationId]);
  useEffect(() => {
    let cancelled = false;
    const listVersion = ++requestVersion.current;
    const loadFromBackend = async (): Promise<void> => {
      if (!applicationId) return;
      try {
        const records = await api.listConversations(applicationId);
        if (cancelled || requestVersion.current !== listVersion) return;
        const local = ensureIndexMigrated(applicationId);
        const backend = records.map(mapConversation);
        const backendIds = new Set(backend.map((item) => item.id));
        const next = [...backend, ...local.filter((item) => !backendIds.has(item.id))];
        setConversations(next);
        const hasActiveConversation = next.some((item) => item.id === conversationIdRef.current);
        const stored = readStoredChat(applicationId, conversationIdRef.current);
        if (!hasActiveConversation && !stored && next[0]) onConversationId(next[0].id);
      } catch {
        // localStorage remains a best-effort fallback for older/local-only data.
        if (!cancelled && requestVersion.current === listVersion) setConversations(ensureIndexMigrated(applicationId));
      }
    };
    void loadFromBackend();
    return () => { cancelled = true; };
  }, [applicationId, onConversationId]);
  useEffect(() => {
    let cancelled = false;
    const loadMessagesFromBackend = async (): Promise<void> => {
      if (!applicationId || !conversationId) return;
      // A just-completed stream already applied everything the history API
      // would return (answer, trace, usage, tool calls, sources). Refetching
      // here would replace every message object and remount the transcript
      // right after the answer landed — the visible "page refresh".
      if (streamSyncedConversation.current === conversationId) return;
      const historyVersion = requestVersion.current;
      try {
        const records = await api.getConversationMessages(applicationId, conversationId);
        if (cancelled || busyRef.current || requestVersion.current !== historyVersion) return;
        // The stream may have completed while the fetch was in flight.
        if (streamSyncedConversation.current === conversationId) return;
        if (!records.length) {
          setMessages([]);
          setSources([]);
          writeStoredChat(chatStorageKey(applicationId, conversationId), [], []);
          return;
        }
        // Messages completed before diagnostics were persisted server-side have no
        // usage/TTFT/tool calls in the history API. Fall back to the client-side
        // values recorded for these message ids (kept in localStorage); server
        // values win when present.
        const storedMeta = new Map<string, { trace: AgentTraceStep[] | null; usage: TokenUsage | null; ttftMs: number | null; toolCalls: ToolCall[] | null }>();
        for (const message of readStoredChat(applicationId, conversationId)?.messages ?? []) {
          const metaKey = message.server_id || message.id;
          if (metaKey) storedMeta.set(metaKey, { trace: message.trace ?? null, usage: message.usage ?? null, ttftMs: message.ttft_ms ?? null, toolCalls: message.tool_calls ?? null });
        }
        const nextMessages = records.map((item) => {
          const message = mapMessage(item);
          const meta = message.id ? storedMeta.get(message.id) : undefined;
          if (meta) {
            message.trace = message.trace?.length ? message.trace : meta.trace;
            message.usage = message.usage ?? meta.usage;
            message.ttft_ms = message.ttft_ms ?? meta.ttftMs;
            message.tool_calls = message.tool_calls?.length ? message.tool_calls : meta.toolCalls;
          }
          return message;
        });
        const lastAssistant = [...records].reverse().find((item) => item.role === "assistant");
        const nextSources = lastAssistant?.sources || [];
        setMessages(nextMessages);
        setSources(nextSources);
        writeStoredChat(chatStorageKey(applicationId, conversationId), nextMessages, nextSources);
      } catch {
        // Keep the already-loaded local fallback when the history API is unavailable.
      }
    };
    void loadMessagesFromBackend();
    return () => { cancelled = true; };
  }, [applicationId, conversationId]);
  const adoptConversationId = (nextConversationId: string): void => {
    if (!nextConversationId || nextConversationId === conversationIdRef.current) return;
    const currentStorageKey = latestChat.current.storageKey;
    const nextStorageKey = chatStorageKey(applicationId, nextConversationId);
    if (currentStorageKey && nextStorageKey !== currentStorageKey) {
      writeStoredChat(nextStorageKey, latestChat.current.messages, latestChat.current.sources);
      migratedStorageKey.current = nextStorageKey;
      // Keep the index entry in step when the server assigns a new conversation id,
      // otherwise the list would keep pointing at the now-orphaned client-side id.
      renameConversationEntry(conversationIdRef.current, nextConversationId);
    }
    onConversationId(nextConversationId);
  };
  const upsertConversation = (id: string, title: string): void => {
    if (!applicationId || !id) return;
    const list = readConversations(applicationId);
    const existing = list.find((item) => item.id === id);
    const entry: ConversationSummary = existing
      ? { ...existing, updatedAt: Date.now() }
      : { id, title: (title || "新对话").slice(0, 40), updatedAt: Date.now() };
    const next = [entry, ...list.filter((item) => item.id !== id)];
    writeConversations(applicationId, next);
    setConversations(next);
  };
  const renameConversationEntry = (oldId: string, newId: string): void => {
    if (!applicationId || oldId === newId) return;
    const list = readConversations(applicationId);
    if (!list.some((item) => item.id === oldId)) return;
    const next = list.map((item) => (item.id === oldId ? { ...item, id: newId, updatedAt: Date.now() } : item));
    writeConversations(applicationId, next);
    setConversations(next);
  };
  const switchConversation = (id: string): void => {
    setShowConversations(false);
    if (!id || id === conversationIdRef.current) return;
    // Abort any in-flight stream so it cannot keep writing into the newly selected
    // conversation; the existing load effect restores messages from storage.
    requestVersion.current += 1;
    controller.current?.abort();
    controller.current = null;
    streamSyncedConversation.current = null;
    setBusy(false);
    onConversationId(id);
  };
  const deleteConversation = async (id: string): Promise<void> => {
    if (!applicationId || !id) return;
    // 会话是硬删除且不可恢复，删前确认一次，避免误点。
    if (!window.confirm("删除这个会话？此操作不可撤销。")) return;
    const previousList = readConversations(applicationId);
    const nextList = previousList.filter((item) => item.id !== id);
    const wasCurrent = id === conversationIdRef.current;
    let previousChat: string | null = null;
    try {
      previousChat = localStorage.getItem(chatStorageKey(applicationId, id));
      writeConversations(applicationId, nextList);
      setConversations(nextList);
      localStorage.removeItem(chatStorageKey(applicationId, id));
    } catch {
      // localStorage is a best-effort cache; the backend remains authoritative.
    }

    try {
      await api.deleteConversation(applicationId, id);
    } catch (error) {
      // Legacy/local-only conversations are not present in SQLite. They are
      // still considered deleted when the backend correctly returns 404.
      const status =
        error && typeof error === "object" && "status" in error
          ? (error as { status?: unknown }).status
          : undefined;
      if (status !== 404) {
        writeConversations(applicationId, previousList);
        setConversations(previousList);
        if (previousChat !== null) {
          try {
            localStorage.setItem(chatStorageKey(applicationId, id), previousChat);
          } catch {
            // The UI still reports the failure even if the cache cannot be restored.
          }
        }
        toast(error instanceof Error ? error.message : "删除会话失败", true);
        return;
      }
    }

    if (wasCurrent) {
      if (nextList[0]) switchConversation(nextList[0].id);
      else {
        requestVersion.current += 1;
        controller.current?.abort();
        controller.current = null;
        setBusy(false);
        setMessages([]);
        setSources([]);
        onNewConversation();
      }
    }
  };
  const resetConversation = () => { requestVersion.current += 1; controller.current?.abort(); streamSyncedConversation.current = null; setBusy(false); setMessages([]); setSources([]); onNewConversation(); };
  const stop = () => {
    // 终止当前回答：中止请求、保留已流出的部分文本并标记"已停止"。从未产出正文的
    // 气泡直接移除，避免留下空白的"已停止"占位。与切换会话/新对话不同，这里不清理
    // 会话，用户可继续提问或重发。
    requestVersion.current += 1;
    controller.current?.abort();
    controller.current = null;
    setBusy(false);
    setMessages((current) =>
      current.flatMap((message) => {
        if (message.pending && message.role === "assistant") {
          const trace = message.trace || [];
          return message.text || trace.length ? [{ ...message, trace, pending: false, stopped: true }] : [];
        }
        return [message];
      }),
    );
  };
  const send = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = event.currentTarget;
    const input = form.elements.namedItem("message") as HTMLTextAreaElement;
    const message = input.value.trim();
    if (!message || !selected || busy) return;
    input.value = "";
    upsertConversation(conversationIdRef.current, message);
    streamSyncedConversation.current = null;
    const version = ++requestVersion.current;
    const assistantId = `${version}-assistant`;
    setMessages((current) => [...current, { id: `${version}-user`, role: "user", text: message }, { id: assistantId, role: "assistant", text: "", pending: true }]);
    // Keep the previous turn's snippets visible until the new `done` replaces
    // them — clearing here flashes the sources panel empty on every question.
    setBusy(true);
    const activeController = new AbortController(); controller.current = activeController;
    let receivedDone = false;
    // A model round starts as provisional answer text. If the same round later
    // produces a tool call, its confirmed trace event smoothly promotes that
    // text into the processing panel; the final round stays in the answer.
    let liveAnswer = "";
    let liveAnswerRound: number | null = null;
    const liveTrace: AgentTraceStep[] = [];
    const liveTraceRoundIndexes = new Map<number, number>();
    // Token deltas arrive far faster than frames; batch them so the markdown
    // re-render happens at most once per animation frame instead of once per
    // SSE chunk. Trace/done/clarification events still flush synchronously.
    let flushFrame: number | null = null;
    const cancelFlush = (): void => {
      if (flushFrame == null) return;
      cancelAnimationFrame(flushFrame);
      flushFrame = null;
    };
    const flushLive = (): void => {
      flushFrame = null;
      if (version !== requestVersion.current) return;
      const transitionName = liveAnswerRound == null ? null : `agent-round-${version}-${liveAnswerRound}`;
      setMessages((current) => current.map((entry) => entry.id === assistantId ? { ...entry, text: liveAnswer, trace: [...liveTrace], streamRound: liveAnswerRound, streamTransitionName: transitionName } : entry));
    };
    const scheduleFlush = (): void => {
      if (flushFrame != null) return;
      flushFrame = requestAnimationFrame(flushLive);
    };
    try {
      const response = await api.chat(selected.id, message, conversationId, clarificationEnabled, activeController.signal);
      for await (const item of parseSse(response)) {
        if (version !== requestVersion.current) return;
        if (item.event === "ready") adoptConversationId(String(item.data.conversation_id || conversationId));
        else if (item.event === "message") {
          const delta = String(item.data.delta || "");
          if (delta) {
            const round = typeof item.data.round === "number" ? item.data.round : null;
            if (item.data.phase === "answer") {
              liveAnswer = delta;
              liveAnswerRound = round;
            } else {
              if (round != null && liveAnswerRound !== round) liveAnswer = "";
              liveAnswerRound = round;
              liveAnswer += delta;
            }
            scheduleFlush();
          }
        }
        else if (item.event === "trace") {
          const step = parseTraceStep(item.data);
          if (step) {
            const shouldPromote = step.kind === "agent_message" && step.round != null && step.round === liveAnswerRound && Boolean(liveAnswer);
            if (shouldPromote) {
              // Ensure the source snapshot exists even when the tool-call event
              // follows the last text delta within the same animation frame.
              cancelFlush();
              flushSync(flushLive);
            }
            const transitionName = shouldPromote && step.kind === "agent_message" && step.round != null
              ? `agent-round-${version}-${step.round}`
              : undefined;
            const nextStep = transitionName && step.kind === "agent_message" ? { ...step, transitionName } : step;
            if (step.kind === "agent_message" && step.round != null) {
              const existingIndex = liveTraceRoundIndexes.get(step.round);
              if (existingIndex == null) {
                liveTraceRoundIndexes.set(step.round, liveTrace.length);
                liveTrace.push(nextStep);
              } else {
                liveTrace[existingIndex] = nextStep;
              }
            } else {
              liveTrace.push(nextStep);
            }
            if (shouldPromote) {
              liveAnswer = "";
              liveAnswerRound = null;
            }
            const committedAnswer = liveAnswer;
            const committedRound = liveAnswerRound;
            const committedTrace = [...liveTrace];
            const commitTrace = (): void => setMessages((current) => current.map((entry) => entry.id === assistantId ? { ...entry, text: committedAnswer, trace: committedTrace, streamRound: committedRound, streamTransitionName: null } : entry));
            if (shouldPromote) await commitWithViewTransition(commitTrace);
            else commitTrace();
          }
        }
        else if (item.event === "clarification") {
          const clarification = item.data as unknown as ClarificationRequest;
          const text = `${clarification.context ? `${clarification.context}\n\n` : ""}${clarification.question}${clarification.options?.length ? `\n\n${clarification.options.map((option, index) => `${index + 1}. ${option}`).join("\n")}` : ""}`;
          setMessages((current) => current.map((entry) => entry.id === assistantId ? { ...entry, text, pending: false, trace: [...liveTrace] } : entry));
        } else if (item.event === "done") {
          receivedDone = true;
          cancelFlush();
          const done = item.data as unknown as ChatDoneEvent;
          const completedMessageId = done.message_id || assistantId;
          const nextSources = (done.retrieved_snippets || []).map((source) => ({ ...source, message_id: source.message_id || completedMessageId }));
          const doneTrace = (done.trace || []).map(parseTraceStep).filter((step): step is AgentTraceStep => step !== null);
          const completedTrace = Array.isArray(done.trace) ? doneTrace : liveTrace;
          // Keep the streaming placeholder id as the React key and record the
          // server id separately — swapping `id` here would remount the whole
          // bubble (replaying trace animations and re-typesetting markdown)
          // the moment the answer completes.
          const completedAssistant: ChatMessage = { id: assistantId, server_id: completedMessageId, role: "assistant", text: done.answer || "", pending: false, tool_calls: done.tool_calls ?? null, trace: completedTrace, keepTraceOpen: true, streamRound: null, streamTransitionName: null, usage: done.usage ?? null, ttft_ms: done.ttft_ms ?? null, sources: nextSources };
          const currentMessages = latestChat.current.messages;
          const nextMessages = currentMessages.some((entry) => entry.id === assistantId)
            ? currentMessages.map((entry) => entry.id === assistantId ? { ...entry, ...completedAssistant } : entry)
            : [...currentMessages, completedAssistant];
          streamSyncedConversation.current = String(done.conversation_id || "");
          adoptConversationId(done.conversation_id);
          writeStoredChat(chatStorageKey(applicationId, done.conversation_id), nextMessages, nextSources);
          setSources(nextSources);
          setMessages(nextMessages);
        }
        else if (item.event === "error") throw new Error(String(item.data.message || "对话服务发生错误"));
      }
      if (!receivedDone) throw new Error("流式响应未正常结束");
    } catch (error) {
      if ((error as DOMException).name === "AbortError" || version !== requestVersion.current) return;
      const text = error instanceof Error ? error.message : "对话请求失败";
      setMessages((current) => current.map((entry) => entry.id === assistantId ? { ...entry, pending: false, error: true, text } : entry)); toast(text, true);
    } finally { cancelFlush(); if (version === requestVersion.current) { setBusy(false); controller.current = null; } }
  };
  const showCitation = (messageSources: SourceSnippet[], citationId: number): void => {
    const sourceIndex = messageSources.findIndex((source, index) => sourceCitation(source, index) === citationId);
    if (sourceIndex < 0) return;
    const source = messageSources[sourceIndex];
    setSources(messageSources);
    setExpandedSources((current) => ({ ...current, [sourceItemKey(source, sourceIndex)]: true }));
    setActiveCitation(citationId);
    setPendingCitation(citationId);
  };
  if (!selected) return null;
  return (
    <section className="chat-layout">
      <div className="chat-panel">
        <div className="chat-toolbar">
          <div className="app-picker"><div className="app-avatar" aria-hidden="true">⌘</div><select value={selected.id} onChange={(event) => { resetConversation(); onSelectApp(event.target.value); }} aria-label="选择应用">{apps.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div>
          <div className="toolbar-meta">
            <span className="toolbar-search-mode">{selected.search_mode || "scan"} 检索</span>
            <div className="conv-picker"><button className="quiet-button" type="button" onClick={() => setShowConversations((open) => !open)} aria-expanded={showConversations} aria-label="会话列表">会话{conversations.length ? ` (${conversations.length})` : ""} ▾</button>{showConversations && (<><div className="conv-backdrop" onClick={() => setShowConversations(false)} aria-hidden="true" /><div className="conv-popover" role="menu">{conversations.length ? conversations.map((conv) => (<div key={conv.id} className={`conv-item ${conv.id === conversationId ? "is-active" : ""}`} role="button" tabIndex={0} onClick={() => switchConversation(conv.id)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); switchConversation(conv.id); } }}><span className="conv-title" title={conv.title}>{conv.title}</span><span className="conv-time">{timeAgo(conv.updatedAt)}</span><button className="conv-del" type="button" aria-label="删除会话" onClick={(event) => { event.stopPropagation(); deleteConversation(conv.id); }}>×</button></div>)) : <div className="conv-empty">还没有会话</div>}</div></>)}</div>
            <button className="quiet-button" onClick={resetConversation} type="button">新对话</button>
          </div>
        </div>
        <div ref={scrollRef} className="chat-scroll" aria-live="polite" aria-busy={busy} onScroll={(event) => { const el = event.currentTarget; stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120; }}>
          {messages.length ? messages.map((message) => (
            <article key={message.id} className={`message ${message.role === "user" ? "user" : ""} ${message.error ? "error" : ""}`}>
              <div className="message-avatar" aria-hidden="true">{message.role === "user" ? "你" : "N"}</div>
              <div className="message-body">
                <div className="message-label">{message.role === "user" ? "你" : "Nianlun"}</div>
                {message.role === "assistant" ? <AgentTraceDetails trace={message.trace} pending={Boolean(message.pending)} keepOpen={message.keepTraceOpen} /> : null}
                {message.role === "user" || message.text || !message.pending ? <div className={`message-text ${message.streamTransitionName ? "is-stream-candidate" : ""}`} style={message.streamTransitionName ? { viewTransitionName: message.streamTransitionName } : undefined}>{message.role === "assistant" ? <MarkdownContent className="message-markdown" text={message.text} onCitationClick={message.sources?.length ? (citationId) => showCitation(message.sources || [], citationId) : undefined} /> : message.text}</div> : null}
                {message.role === "assistant" && !message.pending && message.usage ? <div className="message-usage">{formatUsage(message.usage, message.ttft_ms)}</div> : null}
                {message.role === "assistant" && !message.pending && message.tool_calls?.length ? <ToolCallDetails toolCalls={message.tool_calls} /> : null}
                {message.stopped && <div className="message-status">已停止 · 回答未完成</div>}
              </div>
            </article>
          )) : <div className="welcome-copy"><div className="eyebrow">{selected.search_mode || "scan"} 检索</div><h2>开始一段新的探索</h2><p>向 {selected.name} 提问，答案会优先基于绑定知识库中的内容生成。</p></div>}
        </div>
        <form className="composer" onSubmit={send}><div className="composer-box"><textarea name="message" rows={1} maxLength={32000} disabled={busy} aria-label="输入问题" placeholder="问问你的知识库..." onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} />{busy ? <button className="stop-button" type="button" title="停止回答" aria-label="停止回答" onClick={stop}>■</button> : <button className="send-button" type="submit" title="发送" aria-label="发送">↑</button>}</div><div className="composer-hint">回答会显示引用片段 · Enter 发送，Shift + Enter 换行</div></form>
      </div>
      <aside className="sources-panel"><div className="panel-heading"><h3>检索片段</h3><span>{sources.length ? `${sources.length} 条` : "等待检索"}</span></div><div className="source-list">{sources.length ? sources.map((source, index) => { const text = sourceText(source); const key = sourceItemKey(source, index); const citationId = sourceCitation(source, index); const expanded = Boolean(expandedSources[key]); const visibleText = expanded ? text : text.slice(0, 180); return <article className={`source-item ${activeCitation === citationId ? "is-citation-active" : ""}`} id={`citation-${citationId}`} key={key}><div className="source-item-heading"><span className="source-citation">[{citationId}]</span><strong title={sourceName(source)}>{sourceName(source)}</strong></div><MarkdownContent className="source-markdown" text={`${visibleText}${!expanded && text.length > 180 ? "…" : ""}`} /><button className="source-toggle" type="button" onClick={() => setExpandedSources((current) => ({ ...current, [key]: !expanded }))}>{expanded ? "收起片段" : "查看本次检索内容"}</button><small>{sourceLocation(source)}</small></article>; }) : <div className="source-empty">当对话产生检索结果时，相关片段会出现在这里。</div>}</div></aside>
    </section>
  );
}
