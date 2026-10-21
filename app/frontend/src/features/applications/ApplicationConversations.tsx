import { useEffect, useState } from "react";
import { RefreshCw, Trash2 } from "lucide-react";
import { api } from "../../api/client";
import type { ConversationMessageRecord, ConversationRecord } from "../../types";

type ConversationChannel = "console" | "api" | "wecom";

const channelLabels: Record<ConversationChannel, string> = { console: "控制台", api: "API 服务", wecom: "企业微信" };
const timestamp = (value: string): string => new Date(value).toLocaleString();
const errorMessage = (error: unknown): string => error instanceof Error ? error.message : "请求失败，请重试";

export function ApplicationConversations({ appId }: { appId: string }) {
  const [channel, setChannel] = useState<ConversationChannel>("console");
  const [list, setList] = useState<{ appId: string; channel: ConversationChannel; items: ConversationRecord[] }>({ appId, channel, items: [] });
  const [selection, setSelection] = useState({ appId, channel, id: "" });
  const [messages, setMessages] = useState<ConversationMessageRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const items = list.appId === appId && list.channel === channel ? list.items : [];
  const selectedId = selection.appId === appId && selection.channel === channel ? selection.id : "";
  const selected = items.find((item) => item.id === selectedId);
  const readOnly = channel !== "console";

  useEffect(() => {
    const controller = new AbortController();
    setList({ appId, channel, items: [] }); setLoading(true); setError("");
    void api.listConversations(appId, channel, controller.signal).then((next) => {
      if (controller.signal.aborted) return;
      setList({ appId, channel, items: next });
      setSelection((current) => ({ appId, channel, id: current.appId === appId && current.channel === channel && next.some((item) => item.id === current.id) ? current.id : next[0]?.id || "" }));
    }).catch((failure: unknown) => { if (!controller.signal.aborted) setError(errorMessage(failure)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [appId, channel, refresh]);

  useEffect(() => {
    if (!selectedId) { setMessages([]); setMessagesLoading(false); return; }
    const controller = new AbortController();
    setMessages([]); setMessagesLoading(true); setError("");
    void api.getConversationMessages(appId, selectedId, controller.signal).then((next) => {
      if (!controller.signal.aborted) setMessages(next);
    }).catch((failure: unknown) => { if (!controller.signal.aborted) setError(errorMessage(failure)); })
      .finally(() => { if (!controller.signal.aborted) setMessagesLoading(false); });
    return () => controller.abort();
  }, [appId, selectedId, refresh]);

  const remove = async () => {
    if (!selectedId || readOnly || busy) return;
    if (!window.confirm("确认删除该会话？该操作不可恢复。")) return;
    setBusy(true); setError("");
    try { await api.deleteConversation(appId, selectedId); setSelection({ appId, channel, id: "" }); setMessages([]); setRefresh((value) => value + 1); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  };

  return <div role="tabpanel" id="application-panel-conversations" aria-labelledby="application-tab-conversations" className="application-conversations">
    <div className="application-section-heading"><h3>会话记录</h3><div className="application-run-filters"><div className="field"><label htmlFor="conversation-channel">渠道</label><select id="conversation-channel" value={channel} disabled={loading} onChange={(event) => setChannel(event.target.value as ConversationChannel)}>{(Object.entries(channelLabels) as Array<[ConversationChannel, string]>).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></div><button className="icon-button" type="button" title="刷新会话" aria-label="刷新会话" disabled={loading || busy} onClick={() => setRefresh((value) => value + 1)}><RefreshCw size={16} /></button></div></div>
    {channel === "wecom" && <p className="application-notice">企业微信会话只读：平台仅保存脱敏后的 user_key，不保存原始 userid / chatid。</p>}
    {error && <p className="application-error" role="alert">{error}</p>}
    {loading ? <p className="loading" role="status">正在加载…</p> : items.length ? <div className="application-conversation-layout">
      <ul className="application-conversation-list" aria-label="会话列表">{items.map((item) => <li key={item.id}><button type="button" className={item.id === selectedId ? "is-active" : ""} aria-pressed={item.id === selectedId} onClick={() => setSelection({ appId, channel, id: item.id })}><strong>{item.title || "未命名会话"}</strong><span>{item.message_count} 条消息 · {item.last_message_at ? timestamp(item.last_message_at) : timestamp(item.updated_at)}</span>{item.external_user_id && <code title="user_key">{item.external_user_id}</code>}</button></li>)}</ul>
      <section className="application-conversation-detail" aria-label="会话消息">
        <header><div><strong>{selected?.title || "会话"}</strong><span>{selected ? `${channelLabels[channel]} · ${selected.message_count} 条消息` : ""}</span>{selected?.external_user_id && <code title="user_key">{selected.external_user_id}</code>}</div>{!readOnly && <button className="quiet-button danger-button" type="button" disabled={busy || !selected} onClick={() => void remove()}><Trash2 size={14} />删除会话</button>}</header>
        {messagesLoading ? <p className="loading" role="status">正在加载消息…</p> : messages.length ? <div className="application-conversation-messages">{messages.map((message) => <article key={message.id} className={`application-conversation-message is-${message.role}`}><div><strong>{message.role === "user" ? "用户" : "Agent"}</strong><span>{timestamp(message.created_at)}</span></div><p>{message.content}</p>{message.status === "failed" && <span className="application-error">{message.error_message || "消息生成失败"}</span>}</article>)}</div> : <p className="application-empty">暂无消息</p>}
      </section>
    </div> : <p className="application-empty">{channelLabels[channel]}暂无会话</p>}
  </div>;
}
