import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { ArrowLeft, ArrowUpRight, BookOpen, Bot, Braces, Building2, Check, ChevronLeft, ChevronRight, Copy, Globe, MessageCircle, Plus, RefreshCw, Upload } from "lucide-react";
import { api, API_BASE, publicApiBase } from "../../api/client";
import { Modal } from "../../components/Modal";
import type { Application, ApplicationApiClient, ApplicationApiKey, ApplicationPage, ApplicationPublication, ApplicationRelease, ApplicationRun, KnowledgeBase, ModelProfile } from "../../types";

interface Props {
  app: Application;
  knowledgeBases: KnowledgeBase[];
  llmModels: ModelProfile[];
  onBack: () => void;
  onUse: (id: string) => void;
  onUpdated?: (app: Application) => void;
}
type Tab = "publication" | "draft" | "clients" | "history" | "runs";
const timestamp = (value: string): string => new Date(value).toLocaleString();
const errorMessage = (error: unknown): string => error instanceof Error ? error.message : "请求失败，请重试";
const statusLabel = (publication: ApplicationPublication): string => !publication.active_release ? "未发布" : publication.enabled ? "已发布" : "已停用";
const runStatuses: Record<string, string> = { running: "执行中", cancelling: "取消中", completed: "已完成", failed: "失败", cancelled: "已取消", timed_out: "超时", interrupted: "已中断" };
const usageLabels: Record<string, string> = { input_tokens: "输入 tokens", output_tokens: "输出 tokens", total_tokens: "总 tokens" };
const durationLabel = (duration: number | null): string => duration === null ? "—" : duration < 1000 ? `${duration} ms` : `${(duration / 1000).toFixed(2)} s`;

function Pagination({ offset, total, onChange, disabled }: { offset: number; total: number; onChange: (offset: number) => void; disabled: boolean }) {
  return <div className="application-pagination"><span>共 {total} 条 · 第 {Math.floor(offset / 20) + 1} 页</span><button className="icon-button" title="上一页" aria-label="上一页" disabled={disabled || offset === 0} onClick={() => onChange(offset - 20)}><ChevronLeft size={16} /></button><button className="icon-button" title="下一页" aria-label="下一页" disabled={disabled || offset + 20 >= total} onClick={() => onChange(offset + 20)}><ChevronRight size={16} /></button></div>;
}

function ChannelDialog({ appId, apiCreated, onClose, onCreated }: { appId: string; apiCreated: boolean; onClose: () => void; onCreated: (publication: ApplicationPublication) => void }) {
  const [selected, setSelected] = useState<"api" | "">("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selected || busy || apiCreated) return;
    setBusy(true); setError(null);
    try { onCreated(await api.createPublicationChannel(appId, selected)); }
    catch (failure) { setError(errorMessage(failure)); setBusy(false); }
  };
  return <Modal title="创建发布渠道" description="" onClose={() => { if (!busy) onClose(); }}>
    <form onSubmit={(event) => void submit(event)}>
      <fieldset className="application-channel-options" disabled={busy}>
        <legend>渠道类型</legend>
        <label className={`application-channel-option ${selected === "api" ? "is-selected" : ""} ${apiCreated ? "is-unavailable" : ""}`}><input type="radio" name="channel_type" value="api" checked={selected === "api"} disabled={apiCreated} onChange={() => setSelected("api")} /><span className="application-channel-icon api"><Braces size={20} /></span><span className="application-channel-option-copy"><strong>API 服务</strong><small>系统与业务集成</small></span><span className={apiCreated ? "application-planned-label" : "application-state is-live"}>{apiCreated ? "已创建" : "可用"}</span></label>
        {([{ name: "企业微信", type: "wecom", label: "企业应用 / 微信客服", icon: Building2 }, { name: "个人微信", type: "wechat", label: "微信账号接入", icon: MessageCircle }]).map(({ name, type, label, icon: Icon }) => <label className="application-channel-option is-unavailable" key={type}><input type="radio" name="channel_type" value={type} disabled /><span className="application-channel-icon"><Icon size={20} /></span><span className="application-channel-option-copy"><strong>{name}</strong><small>{label}</small></span><span className="application-planned-label">即将支持</span></label>)}
      </fieldset>
      {error && <p className="application-error" role="alert">{error}</p>}
      <div className="modal-actions"><button type="button" className="outline-button" disabled={busy} onClick={onClose}>取消</button><button type="submit" className="primary-button" disabled={!selected || busy || apiCreated}>{busy ? "创建中…" : "创建"}</button></div>
    </form>
  </Modal>;
}

function KeyDialog({ appId, client, rotationKey, onClose, onRegistered, onRevoked }: { appId: string; client: ApplicationApiClient; rotationKey: ApplicationApiKey | null; onClose: () => void; onRegistered: (key: ApplicationApiKey) => void; onRevoked: (key: ApplicationApiKey) => void }) {
  const [secret, setSecret] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [oldRevoked, setOldRevoked] = useState(false);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const register = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;
    const form = new FormData(event.currentTarget);
    setBusy(true); setError(null);
    try {
      const keyId = Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, "0")).join("");
      const bytes = crypto.getRandomValues(new Uint8Array(32));
      const encoded = btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
      const key = `nl_app_${keyId}.${encoded}`;
      const expiration = String(form.get("expires_at") || "");
      const registered = await api.registerApiKey(appId, client.id, { name: String(form.get("name") || "默认 Key").trim() || "默认 Key", api_key: key, expires_at: expiration ? new Date(expiration).toISOString() : null });
      if (alive.current) { onRegistered(registered); setSecret(key); }
    } catch (failure) { if (alive.current) setError(errorMessage(failure)); }
    finally { if (alive.current) setBusy(false); }
  };
  const close = () => { alive.current = false; setSecret(null); onClose(); };
  const revokeOld = async () => {
    if (!rotationKey || busy) return;
    setBusy(true); setError(null);
    try { const revoked = await api.revokeApiKey(appId, client.id, rotationKey.key_id); if (alive.current) { onRevoked(revoked); setOldRevoked(true); } }
    catch (failure) { if (alive.current) setError(errorMessage(failure)); }
    finally { if (alive.current) setBusy(false); }
  };
  const copy = async () => {
    try { await navigator.clipboard.writeText(secret || ""); if (alive.current) setCopied(true); }
    catch { if (alive.current) setError("无法复制，请选中凭据复制"); }
  };
  return <Modal title={`${rotationKey ? "轮换 Key" : "新增 Key"} · ${client.name}`} description="凭据仅供可信服务端使用。关闭后无法再次查看。" onClose={close}>
    {error && <p role="alert" className="application-error">{error}</p>}
    {secret ? <><span className="tag ready">已注册</span><pre className="application-secret" aria-label="已注册的 API Key">{secret}</pre><div className="modal-actions"><button className="outline-button" type="button" onClick={() => void copy()}>{copied ? "已复制" : "复制 Key"}</button>{rotationKey && <button className="outline-button" type="button" disabled={busy || oldRevoked} onClick={() => void revokeOld()}>{oldRevoked ? "旧 Key 已撤销" : "撤销旧 Key"}</button>}<button className="primary-button" type="button" onClick={close}>完成</button></div></> : <form className="form-grid" onSubmit={(event) => void register(event)}><div className="field"><label htmlFor="key-name">Key 名称</label><input id="key-name" name="name" required maxLength={128} defaultValue={rotationKey ? `${rotationKey.name} · 轮换` : "默认 Key"} disabled={busy} /></div><div className="field"><label htmlFor="key-expiration">到期时间</label><input id="key-expiration" name="expires_at" type="datetime-local" disabled={busy} /></div><div className="modal-actions"><button type="button" className="outline-button" onClick={close}>取消</button><button type="submit" className="primary-button" disabled={busy}>{busy ? "注册中…" : "生成并注册"}</button></div></form>}
  </Modal>;
}

export function ApplicationDetail({ app, knowledgeBases, llmModels, onBack, onUse, onUpdated }: Props) {
  const [draft, setDraft] = useState(app);
  const [draftDirty, setDraftDirty] = useState(false);
  const [tab, setTab] = useState<Tab>("publication");
  const [apiDetail, setApiDetail] = useState(false);
  const [confirmPublish, setConfirmPublish] = useState(false);
  const [createChannel, setCreateChannel] = useState(false);
  const [copiedField, setCopiedField] = useState<string | null>(null);
  const [publication, setPublication] = useState<ApplicationPublication | null>(null);
  const [releases, setReleases] = useState<ApplicationPage<ApplicationRelease>>({ items: [], total: 0 });
  const [releaseOffset, setReleaseOffset] = useState(0);
  const [clients, setClients] = useState<ApplicationApiClient[]>([]);
  const [selectedClientId, setSelectedClientId] = useState("");
  const [keys, setKeys] = useState<ApplicationApiKey[]>([]);
  const [keyClient, setKeyClient] = useState<ApplicationApiClient | null>(null);
  const [rotationKey, setRotationKey] = useState<ApplicationApiKey | null>(null);
  const [runs, setRuns] = useState<ApplicationPage<ApplicationRun>>({ items: [], total: 0 });
  const [runOffset, setRunOffset] = useState(0);
  const [runStatus, setRunStatus] = useState("");
  const [runClient, setRunClient] = useState("");
  const [loading, setLoading] = useState(true);
  const [keysLoading, setKeysLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const selectedClient = clients.find((item) => item.id === selectedClientId);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(null);
    const load = async () => {
      try {
        const state = await api.getPublication(app.id, controller.signal);
        if (!controller.signal.aborted) setPublication(state);
        if (tab === "history") {
          const page = await api.listReleases(app.id, releaseOffset, controller.signal);
          if (!controller.signal.aborted) setReleases(page);
        } else if (tab === "clients" || tab === "runs") {
          const items = await api.listApiClients(app.id, controller.signal);
          if (!controller.signal.aborted) { setClients(items); setSelectedClientId((current) => items.some((item) => item.id === current) ? current : items[0]?.id || ""); }
          if (tab === "runs") {
            const page = await api.listApplicationRuns(app.id, { offset: runOffset, status: runStatus, client_id: runClient }, controller.signal);
            if (!controller.signal.aborted) setRuns(page);
          }
        }
      } catch (failure) { if (!controller.signal.aborted) setError(errorMessage(failure)); }
      finally { if (!controller.signal.aborted) setLoading(false); }
    };
    void load();
    return () => controller.abort();
  }, [app.id, tab, releaseOffset, runOffset, runStatus, runClient, refresh]);

  useEffect(() => {
    if (tab !== "clients" || !selectedClientId) { setKeys([]); return; }
    const controller = new AbortController();
    setKeys([]); setKeysLoading(true);
    void api.listApiKeys(app.id, selectedClientId, controller.signal).then((items) => { if (!controller.signal.aborted) setKeys(items); }).catch((failure: unknown) => { if (!controller.signal.aborted) setError(errorMessage(failure)); }).finally(() => { if (!controller.signal.aborted) setKeysLoading(false); });
    return () => controller.abort();
  }, [app.id, selectedClientId, tab, refresh]);

  const mutate = async (operation: () => Promise<void>) => {
    if (busy) return;
    setBusy(true); setError(null); setNotice(null);
    try { await operation(); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  };
  const publish = () => mutate(async () => {
    if (!publication) return;
    const state = await api.publishApp(app.id, { expected_config_version: publication.config_version, expected_deployment_version: publication.deployment_version });
    setPublication(state); setNotice("发布配置已更新"); setReleaseOffset(0); setRefresh((value) => value + 1);
  });
  const togglePublication = () => mutate(async () => {
    if (!publication) return;
    setPublication(await api.updatePublication(app.id, { enabled: !publication.enabled, expected_deployment_version: publication.deployment_version }));
  });
  const saveDraft = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    void mutate(async () => {
      const response = await api.updateApp(app.id, { expected_config_version: draft.config_version ?? 1, name: String(form.get("name") || "").trim(), description: String(form.get("description") || "").trim(), knowledge_base_id: String(form.get("knowledge_base_id") || ""), llm_model_id: String(form.get("llm_model_id") || "") || null, agent_version: form.get("agent_version") === "v1" ? "v1" : "v0", clarification_enabled: form.get("clarification_enabled") === "on" });
      const updated = { ...draft, ...response };
      setDraft(updated); setDraftDirty(false); onUpdated?.(updated); setNotice("草稿已保存"); setRefresh((value) => value + 1);
    });
  };
  const createClient = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = event.currentTarget;
    const name = String(new FormData(form).get("name") || "").trim();
    void mutate(async () => { const created = await api.createApiClient(app.id, name); setClients((items) => [...items, created]); setSelectedClientId(created.id); form.reset(); });
  };
  const baseUrl = `${publicApiBase()}/v1`;
  const endpoint = `${baseUrl}/chat/completions`;
  const publicBaseMissing = publicApiBase() === API_BASE;
  const reloadDraft = () => mutate(async () => { const latest = { ...draft, ...await api.getApp(app.id) }; setDraft(latest); setDraftDirty(false); onUpdated?.(latest); setRefresh((value) => value + 1); });
  const activeTab = tab === "clients" ? "publication" : tab;
  const switchTab = (next: Tab) => { setTab(next); setApiDetail(false); setError(null); setNotice(null); };
  const copyConnection = async (field: string, value: string) => {
    try { await navigator.clipboard.writeText(value); setCopiedField(field); }
    catch { setError("无法复制，请手动选择复制"); }
  };
  const live = Boolean(publication?.enabled && publication.dependencies_ready);

  return <section className="application-detail application-workspace" aria-busy={loading || busy}>
    <div className="application-breadcrumb"><button onClick={onBack} type="button"><ArrowLeft size={14} />全部 Agent</button><ChevronRight size={13} /><span>{draft.name}</span></div>
    <div className="application-detail-heading"><span className="application-avatar"><Bot size={24} /></span><div className="application-detail-copy"><h2>{draft.name}</h2><p>{draft.description || "Agent"}</p></div><div className="section-actions"><button className="outline-button" onClick={() => onUse(app.id)} type="button"><MessageCircle size={15} />调试 Agent</button><button className="primary-button" type="button" aria-label="发布新版本" title={draftDirty ? "请先保存草稿" : !publication?.api_channel_created ? "请先创建发布渠道" : "发布新版本"} disabled={busy || loading || draftDirty || !publication?.draft_ready || !publication.api_channel_created} onClick={() => setConfirmPublish(true)}><Upload size={15} />发布新版本</button></div></div>
    <div className="application-tabs" role="tablist" aria-label="Agent 管理">{([["draft", "Agent 配置"], ["publication", "发布渠道"], ["history", "发布历史"], ["runs", "运行日志"]] as const).map(([value, label]) => <button key={value} id={`application-tab-${value}`} type="button" role="tab" aria-controls={`application-panel-${value}`} aria-selected={activeTab === value} disabled={busy} onClick={() => switchTab(value)}>{label}</button>)}</div>
    {error && <div role="alert" className="application-error">{error}<button className="quiet-button" type="button" disabled={busy} onClick={() => { if (tab === "draft") void reloadDraft(); else setRefresh((value) => value + 1); }}>{tab === "draft" ? "刷新草稿" : "刷新"}</button></div>}
    {notice && <p role="status" className="application-notice">{notice}</p>}
    {draftDirty && <div className="application-unsaved" role="status"><span className="application-draft-indicator">有未保存的修改</span>{tab !== "draft" && <button className="application-text-button" type="button" onClick={() => switchTab("draft")}>返回 Agent 配置<ChevronRight size={14} /></button>}</div>}
    {loading && <p className="loading" role="status">正在加载…</p>}

    {(tab === "publication" || tab === "clients") && publication && <div className="application-release-strip"><div><span className="application-strip-label">发布版本</span><strong>{publication.active_release ? `r${publication.active_release.sequence}` : "未发布"}</strong>{publication.active_release && <span className="application-muted">{timestamp(publication.active_release.created_at)}</span>}</div>{publication.has_unpublished_changes && <span className="application-draft-indicator">草稿有变更</span>}</div>}
    {(tab === "publication" || tab === "clients") && publication && <>
      {publication.draft_error && <p className="application-error">草稿发布：{publication.draft_error}</p>}
      {publication.active_release && publication.dependency_error && <p className="application-error">当前发布：{publication.dependency_error}</p>}
    </>}
    {tab === "publication" && publication && !apiDetail && <div role="tabpanel" id="application-panel-publication" aria-labelledby="application-tab-publication">
      <div className="application-section-heading"><h3>发布渠道 <span className="application-heading-count">{publication.api_channel_created ? 1 : 0}</span></h3><button className="outline-button" type="button" disabled={busy || loading || publication.api_channel_created} onClick={() => setCreateChannel(true)}><Plus size={15} />创建发布渠道</button></div>
      {publication.api_channel_created ? <div className="application-channel-table" role="table" aria-label="发布渠道">
        <div className="application-channel-row application-channel-labels" role="row"><span role="columnheader">渠道</span><span role="columnheader">接入状态</span><span role="columnheader">服务状态</span><span role="columnheader">运行版本</span><span role="columnheader">操作</span></div>
        <div className="application-channel-row" role="row"><div className="application-channel-name" role="cell"><span className="application-channel-icon api"><Braces size={20} /></span><div><strong>API 服务</strong><small>系统与业务集成</small></div></div><div role="cell"><span className={`application-state ${publication.dependencies_ready ? "is-live" : "is-error"}`}>{publication.dependencies_ready ? "已就绪" : "依赖异常"}</span></div><div role="cell"><span className={`application-state ${live ? "is-live" : ""}`}>{!publication.active_release ? "未启用" : statusLabel(publication)}</span></div><div role="cell" className="application-channel-version">{publication.active_release ? `r${publication.active_release.sequence}` : "—"}</div><div role="cell"><button className="application-text-button" type="button" disabled={busy} onClick={() => setApiDetail(true)}>管理 API<ChevronRight size={14} /></button></div></div>
      </div> : <div className="application-channel-empty"><span className="application-channel-empty-icon"><Globe size={27} /></span><h3>还没有发布渠道</h3></div>}
      <div className="application-knowledge-note"><BookOpen size={15} /><span>{knowledgeBases.find((item) => item.id === draft.knowledge_base_id)?.name || "知识库"}</span><span className="application-note-divider" /><span>自动使用最新已提交内容</span></div>
    </div>}
    {(tab === "publication" || tab === "clients") && apiDetail && <div className="application-api-heading"><button className="icon-button" type="button" title="返回发布渠道" aria-label="返回发布渠道" disabled={busy} onClick={() => { setApiDetail(false); setTab("publication"); }}><ArrowLeft size={17} /></button><span className="application-channel-icon api"><Braces size={18} /></span><h3>API 服务</h3><span className={`application-state ${live ? "is-live" : ""}`}>{publication ? statusLabel(publication) : "读取中…"}</span>{publication?.active_release && <button className="outline-button" type="button" disabled={busy || loading || (!publication.enabled && !publication.dependencies_ready)} onClick={() => void togglePublication()}>{publication.enabled ? "停用 API" : "恢复 API"}</button>}</div>}
    {(tab === "publication" || tab === "clients") && apiDetail && <nav className="application-api-tabs" aria-label="API 服务管理"><button className={tab === "publication" ? "is-active" : ""} type="button" disabled={busy} onClick={() => { setTab("publication"); setError(null); setNotice(null); }}><Globe size={14} />接入配置</button><button className={tab === "clients" ? "is-active" : ""} type="button" disabled={busy} onClick={() => { setTab("clients"); setError(null); setNotice(null); }}><Braces size={14} />客户端与密钥</button><a href={api.docsUrl()} target="_blank" rel="noreferrer">接口文档<ArrowUpRight size={14} /></a></nav>}
    {tab === "publication" && publication && apiDetail && <div role="tabpanel" id="application-panel-publication" aria-labelledby="application-tab-publication">
      <div className="application-connection-fields">{([{ label: "Base URL", value: baseUrl }, { label: "Model", value: app.id }, { label: "调用地址", value: endpoint }]).map(({ label, value }) => <div className="application-endpoint" key={label}><label>{label}</label><div><code aria-label={label}>{value}</code><button className="icon-button" type="button" title={copiedField === label ? "已复制" : `复制${label}`} aria-label={`复制${label}`} onClick={() => void copyConnection(label, value)}>{copiedField === label ? <Check size={16} /> : <Copy size={16} />}</button></div></div>)}</div>
      {publicBaseMissing && <p className="application-endpoint-hint" role="note">当前显示控制台地址，对外部署请替换为公开网关地址</p>}
      <dl className="application-api-facts"><div><dt>接口协议</dt><dd>OpenAI 兼容 · Chat Completions</dd></div><div><dt>运行版本</dt><dd>{publication.active_release ? `r${publication.active_release.sequence}` : "未发布"}</dd></div><div><dt>响应方式</dt><dd>JSON / SSE</dd></div><div><dt>身份验证</dt><dd>Bearer API Key</dd></div></dl>
      <details className="application-example"><summary>cURL 调用示例</summary><pre>{`curl '${endpoint}' \\\n  -H "Authorization: Bearer $NIANLUN_APP_KEY" \\\n  -H 'Content-Type: application/json' \\\n  -d '${JSON.stringify({ model: app.id, messages: [{ role: "user", content: "请概括文档" }], stream: false })}'`}</pre></details>
      <details className="application-example"><summary>Python SDK 调用示例</summary><pre>{`import os\nfrom openai import OpenAI\n\nclient = OpenAI(\n    base_url=${JSON.stringify(baseUrl)},\n    api_key=os.environ["NIANLUN_APP_KEY"],\n)\n\nstream = client.chat.completions.create(\n    model=${JSON.stringify(app.id)},\n    messages=[{"role": "user", "content": "请概括文档"}],\n    stream=True,\n)\nfor chunk in stream:\n    if chunk.choices:\n        print(chunk.choices[0].delta.content or "", end="", flush=True)`}</pre></details>
    </div>}
    {tab === "history" && <div role="tabpanel" id="application-panel-history" aria-labelledby="application-tab-history">
      <div className="application-section-heading"><h3>发布历史</h3><span>{releases.total} 个版本</span></div>
      {releases.items.length ? <div className="application-table-wrap"><table className="application-table"><thead><tr><th>版本</th><th>发布时间</th><th>配置摘要</th></tr></thead><tbody>{releases.items.map((item) => <tr key={item.id}><td>r{item.sequence}{item.id === publication?.active_release?.id && <span className="tag ready">当前</span>}</td><td>{timestamp(item.created_at)}</td><td><code>{item.config_hash.slice(0, 16)}</code><details><summary>配置</summary><pre>{JSON.stringify(item.config, null, 2)}</pre></details></td></tr>)}</tbody></table></div> : !loading && <p className="application-empty">暂无发布版本</p>}
      <Pagination offset={releaseOffset} total={releases.total} onChange={setReleaseOffset} disabled={loading || busy} />
    </div>}

    <form className="application-draft" hidden={tab !== "draft"} id="application-panel-draft" aria-labelledby="application-tab-draft" onChange={() => setDraftDirty(true)} onSubmit={saveDraft} role="tabpanel" key={`${draft.id}-${draft.updated_at}-${draft.config_version}`}>
      <div className="application-section-heading"><h3>Agent 配置</h3><span>草稿 v{draft.config_version ?? 1}</span></div>
      <fieldset disabled={busy}>
        <div className="application-form-section"><div className="application-form-section-title"><h3>基本信息</h3></div><div className="application-form-fields"><div className="field"><label htmlFor="draft-name">Agent 名称</label><input id="draft-name" name="name" defaultValue={draft.name} required maxLength={128} /></div><div className="field"><label htmlFor="draft-description">描述</label><textarea id="draft-description" name="description" defaultValue={draft.description} maxLength={2000} /></div></div></div>
        <div className="application-form-section"><div className="application-form-section-title"><h3>模型与知识库</h3></div><div className="application-form-fields"><div className="field"><label htmlFor="draft-kb">绑定知识库</label><select id="draft-kb" name="knowledge_base_id" defaultValue={draft.knowledge_base_id} required>{!knowledgeBases.some((item) => item.id === draft.knowledge_base_id) && <option value={draft.knowledge_base_id}>当前知识库（不可用）</option>}{knowledgeBases.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div><div className="field"><label htmlFor="draft-model">LLM 模型</label><select id="draft-model" name="llm_model_id" defaultValue={draft.llm_model_id || ""}><option value="">系统默认模型</option>{draft.llm_model_id && !llmModels.some((item) => item.id === draft.llm_model_id) && <option value={draft.llm_model_id}>当前模型（不可用）</option>}{llmModels.map((item) => <option key={item.id} value={item.id}>{item.model || item.name}</option>)}</select></div></div></div>
        <div className="application-form-section"><div className="application-form-section-title"><h3>运行配置</h3></div><div className="application-form-fields"><div className="field"><label htmlFor="draft-version">Agent 架构</label><select id="draft-version" name="agent_version" defaultValue={draft.agent_version}><option value="v0">v0 · 一体化检索与回答</option><option value="v1">v1 · 检索与回答分离</option></select></div><label className="application-checkbox"><input type="checkbox" name="clarification_enabled" defaultChecked={draft.clarification_enabled ?? true} /><span>允许澄清问题</span></label></div></div>
        <div className="application-form-actions"><button className="primary-button" type="submit" disabled={busy}>{busy ? "保存中…" : "保存草稿"}</button></div>
      </fieldset>
    </form>

    {tab === "clients" && <div role="tabpanel" id="application-panel-publication" aria-labelledby="application-tab-publication"><form className="application-client-form" onSubmit={createClient}><div className="field"><label htmlFor="client-name">新客户端名称</label><input id="client-name" name="name" placeholder="例如：内部业务系统" required maxLength={128} disabled={busy} /></div><button className="outline-button" type="submit" disabled={busy}><Plus size={15} />创建客户端</button></form>
      {clients.length ? <><div className="application-client-toolbar"><div className="field"><label htmlFor="selected-client">调用客户端</label><select id="selected-client" value={selectedClientId} disabled={busy} onChange={(event) => setSelectedClientId(event.target.value)}>{clients.map((item) => <option key={item.id} value={item.id}>{item.name}{item.enabled ? "" : " · 已停用"}</option>)}</select></div>{selectedClient && <div className="section-actions"><button className="outline-button" type="button" disabled={busy} onClick={() => void mutate(async () => { const updated = await api.updateApiClient(app.id, selectedClient.id, { enabled: !selectedClient.enabled }); setClients((items) => items.map((item) => item.id === updated.id ? updated : item)); })}>{selectedClient.enabled ? "停用客户端" : "恢复客户端"}</button><button className="primary-button" type="button" disabled={busy || !selectedClient.enabled} onClick={() => { setRotationKey(null); setKeyClient(selectedClient); }}>新增 Key</button></div>}</div>
        {keysLoading ? <p role="status">正在加载 Key…</p> : keys.length ? <div className="application-table-wrap"><table className="application-table"><thead><tr><th>名称 / Key ID</th><th>状态</th><th>创建 / 到期</th><th>操作</th></tr></thead><tbody>{keys.map((key) => { const expired = key.expires_at !== null && new Date(key.expires_at).getTime() <= Date.now(); return <tr key={key.key_id}><td>{key.name}<code className="application-secondary">{key.key_id}</code></td><td>{key.revoked_at ? "已撤销" : expired ? "已到期" : "有效"}</td><td>{timestamp(key.created_at)}<span className="application-secondary">{key.expires_at ? timestamp(key.expires_at) : "长期有效"}</span></td><td><div className="section-actions"><button className="quiet-button" type="button" disabled={busy || !!key.revoked_at || !selectedClient?.enabled} onClick={() => { if (selectedClient) { setRotationKey(key); setKeyClient(selectedClient); } }}>轮换</button><button className="quiet-button danger-button" type="button" disabled={busy || !!key.revoked_at} onClick={() => { if (!window.confirm(`确认撤销「${key.name}」？该操作不可恢复。`)) return; void mutate(async () => { const updated = await api.revokeApiKey(app.id, key.client_id, key.key_id); setKeys((items) => items.map((item) => item.key_id === updated.key_id ? updated : item)); }); }}>撤销</button></div></td></tr>; })}</tbody></table></div> : <p className="application-empty">该客户端暂无 Key</p>}</> : !loading && <p className="application-empty">暂无调用客户端</p>}
    </div>}

    {tab === "runs" && <div role="tabpanel" id="application-panel-runs" aria-labelledby="application-tab-runs"><div className="application-section-heading"><h3>运行日志</h3><span>API 服务</span></div><div className="application-run-filters"><div className="field"><label htmlFor="run-client">客户端</label><select id="run-client" value={runClient} disabled={loading} onChange={(event) => { setRunClient(event.target.value); setRunOffset(0); }}><option value="">全部客户端</option>{clients.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div><div className="field"><label htmlFor="run-status">状态</label><select id="run-status" value={runStatus} disabled={loading} onChange={(event) => { setRunStatus(event.target.value); setRunOffset(0); }}><option value="">全部状态</option>{Object.entries(runStatuses).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></div><button className="icon-button" aria-label="刷新调用记录" title="刷新调用记录" disabled={loading} onClick={() => setRefresh((value) => value + 1)}><RefreshCw size={16} /></button></div>
      {runs.items.length ? <div className="application-table-wrap"><table className="application-table"><thead><tr><th>运行 / 客户端</th><th>状态</th><th>发布 / 知识库</th><th>耗时 / 用量</th><th>时间</th><th>详情</th></tr></thead><tbody>{runs.items.map((run) => <tr key={run.run_id}>
        <td><code>{run.run_id}</code><span className="application-secondary">{clients.find((client) => client.id === run.client_id)?.name || run.client_id}</span></td>
        <td>{runStatuses[run.status] || run.status}</td>
        <td><code>{run.release_id}</code><span className="application-secondary">知识库 v{run.knowledge_base_content_version}</span></td>
        <td className="application-run-metrics">{durationLabel(run.duration_ms)}<span className="application-secondary">{run.usage?.total_tokens === undefined ? "用量未知" : `${run.usage.total_tokens} tokens`}</span></td>
        <td>{timestamp(run.created_at)}</td>
        <td><details><summary>运行详情</summary><dl className="application-run-detail"><dt>请求 ID</dt><dd><code>{run.request_id || "—"}</code></dd><dt>客户端 ID</dt><dd><code>{run.client_id}</code></dd><dt>会话 ID</dt><dd><code>{run.conversation_id}</code></dd>{run.usage && Object.entries(run.usage).map(([name, count]) => <div key={name}><dt>{usageLabels[name] || name}</dt><dd>{count}</dd></div>)}{run.error_message && <><dt>错误</dt><dd>{run.error_message}</dd></>}</dl></details>{run.error_message && <span className="application-secondary">{run.error_type || "调用失败"}</span>}</td>
      </tr>)}</tbody></table></div> : !loading && <p className="application-empty">暂无调用记录</p>}
      <Pagination offset={runOffset} total={runs.total} onChange={setRunOffset} disabled={loading} />
    </div>}
    {keyClient && <KeyDialog appId={app.id} client={keyClient} rotationKey={rotationKey} onClose={() => setKeyClient(null)} onRegistered={(key) => setKeys((items) => [...items, key])} onRevoked={(key) => setKeys((items) => items.map((item) => item.key_id === key.key_id ? key : item))} />}
    {createChannel && publication && <ChannelDialog appId={app.id} apiCreated={publication.api_channel_created} onClose={() => setCreateChannel(false)} onCreated={(state) => { setPublication(state); setCreateChannel(false); setNotice("API 渠道已创建"); }} />}
    {confirmPublish && publication && <Modal title="发布新版本" description={publication.active_release && !publication.enabled ? "将更新 API 服务版本，渠道保持停用。" : "将更新并启用 API 服务，已有会话保持原版本。"} onClose={() => setConfirmPublish(false)}><dl className="application-publish-summary"><div><dt>Agent</dt><dd>{draft.name}</dd></div><div><dt>草稿版本</dt><dd>v{publication.config_version}</dd></div><div><dt>更新渠道</dt><dd><Braces size={15} />API 服务</dd></div><div><dt>知识库</dt><dd>{knowledgeBases.find((item) => item.id === draft.knowledge_base_id)?.name || "当前绑定知识库"}</dd></div></dl><div className="modal-actions"><button className="outline-button" type="button" onClick={() => setConfirmPublish(false)}>取消</button><button className="primary-button" type="button" disabled={busy || loading || !publication.draft_ready} onClick={() => { setConfirmPublish(false); void publish(); }}><Upload size={15} />确认发布</button></div></Modal>}
  </section>;
}
