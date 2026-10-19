import { useEffect, useState } from "react";
import { ArrowRight, ChevronLeft, ChevronRight, Copy, Eye, FilePenLine, GitCompareArrows, Pencil, Plus, Upload } from "lucide-react";
import { api } from "../../api/client";
import { Modal } from "../../components/Modal";
import type { Application, ApplicationPage, ApplicationRelease, KnowledgeBase, PublishedApplicationConfig } from "../../types";

export const versionError = (error: unknown): string => error instanceof Error ? error.message : "请求失败，请重试";
const date = (value: string): string => new Date(value).toLocaleString();
const mergeVersions = (items: ApplicationRelease[]): ApplicationRelease[] => Array.from(new Map(items.map((item) => [item.id, item])).values()).sort((a, b) => b.sequence - a.sequence);

export function useReleaseOptions(appId: string, seed: ApplicationRelease | null) {
  const [items, setItems] = useState<ApplicationRelease[]>(seed ? [seed] : []);
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const [loaded, setLoaded] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError("");
    void api.listReleases(appId, offset, controller.signal).then((page) => {
      if (controller.signal.aborted) return;
      setItems((current) => mergeVersions([...current, ...page.items]));
      setTotal(page.total); setLoaded(offset + page.items.length);
    }).catch((failure: unknown) => { if (!controller.signal.aborted) setError(versionError(failure)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [appId, offset, retry]);
  return { items, loading, error, hasMore: loaded < total, loadMore: () => setOffset(loaded), retry: () => setRetry((value) => value + 1), add: (release: ApplicationRelease) => setItems((current) => mergeVersions([...current, release])) };
}

function settingRows(config: Partial<PublishedApplicationConfig>, knowledgeBases: KnowledgeBase[]) {
  const bool = (value: boolean | undefined) => value === undefined ? "—" : value ? "开启" : "关闭";
  return [
    ["LLM 模型", config.model || "—", config.llm_model_id || ""],
    ["模型地址", config.base_url || "—"],
    ["知识库", knowledgeBases.find((item) => item.id === config.knowledge_base_id)?.name || config.knowledge_base_id || "—", config.knowledge_base_id || ""],
    ["Agent 架构", config.agent_version === "v1" ? "v1 · 检索与回答分离" : config.agent_version === "v0" ? "v0 · 一体化检索与回答" : "—"],
    ["上下文窗口", config.context_window_tokens ? `${config.context_window_tokens.toLocaleString()} tokens` : "—"],
    ["允许澄清问题", bool(config.clarification_enabled)],
    ["知识库更新", "最新已提交内容"],
  ];
}

export function VersionSettings({ config, previous, knowledgeBases }: { config: Partial<PublishedApplicationConfig>; previous?: Partial<PublishedApplicationConfig> | null; knowledgeBases: KnowledgeBase[] }) {
  const rows = settingRows(config, knowledgeBases);
  const before = previous ? settingRows(previous, knowledgeBases) : null;
  return <dl className={`application-version-settings ${before ? "is-comparison" : ""}`}>{rows.map(([label, value, identity], index) => {
    const bindingChanged = before && before[index][2] !== identity;
    const changed = before && (before[index][1] !== value || bindingChanged);
    return <div key={label} className={changed ? "is-changed" : ""}><dt>{label}{changed && <span>变更</span>}</dt><dd>{changed && <><span className="application-setting-before">{before[index][1]}{bindingChanged && before[index][2] ? ` · ${before[index][2]}` : ""}</span><ArrowRight size={13} aria-hidden="true" /></>}<span>{value}{bindingChanged && identity ? ` · ${identity}` : ""}</span></dd></div>;
  })}</dl>;
}

export function VersionDetail({ app, release, active, knowledgeBases, onClose, onRestored }: { app: Application; release: ApplicationRelease; active: ApplicationRelease | null; knowledgeBases: KnowledgeBase[]; onClose: () => void; onRestored: (app: Application) => void }) {
  const options = useReleaseOptions(app.id, release);
  const [compareId, setCompareId] = useState(active && active.id !== release.id ? active.id : "");
  const [confirmRestore, setConfirmRestore] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const compare = options.items.find((item) => item.id === compareId) || (active?.id === compareId ? active : null);
  const restore = async () => {
    setBusy(true); setError("");
    try { onRestored(await api.restoreRelease(app.id, release.id, app.config_version ?? 1)); }
    catch (failure) { setError(versionError(failure)); }
    finally { setBusy(false); }
  };
  return <Modal title={`版本 r${release.sequence}`} description="" onClose={() => { if (!busy) onClose(); }}>
    <div className="application-version-detail">
      <div className="application-version-meta"><span>{date(release.created_at)}</span>{release.id === active?.id && <span className="application-state is-live">API 使用中</span>}</div>
      {release.description && <p className="application-version-description">{release.description}</p>}
      <label className="application-version-compare"><GitCompareArrows size={16} /><span>比较版本</span><select aria-label="比较版本" disabled={busy} value={compareId} onChange={(event) => setCompareId(event.target.value)}><option value="">仅查看配置</option>{mergeVersions([...options.items, ...(active ? [active] : [])]).filter((item) => item.id !== release.id).map((item) => <option key={item.id} value={item.id}>r{item.sequence}{active?.id === item.id ? " · API 当前版本" : ""}</option>)}</select></label>
      {options.hasMore && <button className="application-text-button" type="button" disabled={options.loading} onClick={options.loadMore}>加载更多版本</button>}
      {options.error && <p className="application-error" role="alert">{options.error}<button type="button" className="quiet-button" onClick={options.retry}>重试</button></p>}
      {compare && <p className="application-comparison-label">r{compare.sequence}<ArrowRight size={13} />r{release.sequence}</p>}
      <VersionSettings config={release.config} previous={compare?.config} knowledgeBases={knowledgeBases} />
      {compare && <details className="application-example"><summary>完整配置差异</summary><div className="application-config-pair"><div><strong>r{compare.sequence}</strong><pre>{JSON.stringify(compare.config, null, 2)}</pre></div><div><strong>r{release.sequence}</strong><pre>{JSON.stringify(release.config, null, 2)}</pre></div></div></details>}
      {!compare && <details className="application-example"><summary>完整配置</summary><pre>{JSON.stringify(release.config, null, 2)}</pre></details>}
      {confirmRestore && <div className="application-restore-confirm" role="status">将覆盖当前草稿中的模型、知识库和运行配置，线上版本保持不变。{error && <p className="application-error" role="alert">{error}</p>}<div className="modal-actions"><button className="outline-button" type="button" disabled={busy} onClick={() => setConfirmRestore(false)}>取消</button><button className="primary-button" type="button" disabled={busy} onClick={() => void restore()}>{busy ? "复制中…" : "确认覆盖草稿"}</button></div></div>}
      {!confirmRestore && <div className="modal-actions"><button className="outline-button" type="button" onClick={onClose}>关闭</button><button className="primary-button" type="button" onClick={() => setConfirmRestore(true)}><Copy size={14} />复制到草稿</button></div>}
    </div>
  </Modal>;
}

function CreateVersion({ app, onClose, onCreated }: { app: Application; onClose: () => void; onCreated: (release: ApplicationRelease) => void }) {
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const create = async () => {
    setBusy(true); setError("");
    try { onCreated(await api.createRelease(app.id, { expected_config_version: app.config_version ?? 1, description })); }
    catch (failure) { setError(versionError(failure)); }
    finally { setBusy(false); }
  };
  return <Modal title="创建版本" description="" onClose={() => { if (!busy) onClose(); }}><form className="application-create-version form-grid" onSubmit={(event) => { event.preventDefault(); void create(); }}><div className="field"><label htmlFor="release-description">版本说明</label><textarea id="release-description" aria-label="版本说明" maxLength={500} value={description} onChange={(event) => setDescription(event.target.value)} placeholder="例如：调整回答策略，优化文档检索" disabled={busy} /></div>{error && <p className="application-error" role="alert">{error}</p>}<div className="modal-actions"><button className="outline-button" type="button" disabled={busy} onClick={onClose}>取消</button><button className="primary-button" type="submit" disabled={busy}>{busy ? "创建中…" : "确认创建"}</button></div></form></Modal>;
}

interface Props {
  app: Application;
  page: ApplicationPage<ApplicationRelease>;
  active: ApplicationRelease | null;
  knowledgeBases: KnowledgeBase[];
  loading: boolean;
  canCreate: boolean;
  canPublish: boolean;
  enabled: boolean;
  draftDirty: boolean;
  createDisabledReason: string;
  offset: number;
  onOffset: (offset: number) => void;
  onCreated: () => void;
  onEditDraft: () => void;
  onPublish: (release: ApplicationRelease) => void;
  onRestored: (app: Application) => void;
}

export function ApplicationVersions({ app, page, active, knowledgeBases, loading, canCreate, canPublish, enabled, draftDirty, createDisabledReason, offset, onOffset, onCreated, onEditDraft, onPublish, onRestored }: Props) {
  const [creating, setCreating] = useState(false);
  const [viewing, setViewing] = useState<ApplicationRelease | null>(null);
  return <div role="tabpanel" id="application-panel-history" aria-labelledby="application-tab-history" className="application-versions">
    <div className="application-version-draft" aria-label="当前草稿">
      <FilePenLine size={21} aria-hidden="true" />
      <div className="application-version-draft-copy"><div><strong>当前草稿</strong><span className={draftDirty ? "application-draft-indicator" : "application-muted"}>{draftDirty ? "有未保存修改" : "已保存"}</span></div><p>架构 {app.agent_version} · {knowledgeBases.find((item) => item.id === app.knowledge_base_id)?.name || "知识库"}{!draftDirty && <> · {date(app.updated_at)}</>}</p></div>
      <div className="application-version-draft-actions"><button className="icon-button" type="button" title="编辑草稿" aria-label="编辑草稿" disabled={loading} onClick={onEditDraft}><Pencil size={16} /></button><button className="outline-button" type="button" disabled={!canCreate || loading} title={!canCreate ? createDisabledReason : undefined} onClick={() => setCreating(true)}><Plus size={15} />创建版本</button></div>
    </div>
    <div className="application-section-heading"><h3>版本记录</h3><span className="application-muted">{page.total} 个版本</span></div>
    {page.items.length ? <div className="application-table-wrap"><table className="application-table application-version-table"><thead><tr><th>版本</th><th>版本说明</th><th>创建时间</th><th>使用渠道</th><th>操作</th></tr></thead><tbody>{page.items.map((release) => <tr key={release.id}><td><button className="application-version-link" type="button" onClick={() => setViewing(release)}>r{release.sequence}</button></td><td><span className="application-version-description">{release.description || "—"}</span><small className="application-secondary">{release.config.model} · {release.config.agent_version}</small></td><td className="application-muted">{date(release.created_at)}</td><td>{active?.id === release.id ? <span className={`application-state ${enabled ? "is-live" : ""}`}>API{!enabled ? " · 停用" : ""}</span> : <span className="application-muted">未使用</span>}</td><td><div className="application-version-actions"><button className="icon-button" type="button" title="查看与比较配置" aria-label={`查看 r${release.sequence}`} onClick={() => setViewing(release)}><Eye size={16} /></button><button className="application-text-button" type="button" aria-label={`发布 r${release.sequence}`} disabled={!canPublish || loading || active?.id === release.id} title={active?.id === release.id ? "渠道已使用此版本" : !canPublish ? "请先创建发布渠道" : undefined} onClick={() => onPublish(release)}><Upload size={14} />发布</button></div></td></tr>)}</tbody></table></div> : !loading && <p className="application-version-empty">尚未创建版本</p>}
    {page.total > 20 && <div className="application-pagination"><span>共 {page.total} 个 · 第 {Math.floor(offset / 20) + 1} 页</span><button className="icon-button" type="button" title="上一页" aria-label="上一页" disabled={loading || offset === 0} onClick={() => onOffset(offset - 20)}><ChevronLeft size={16} /></button><button className="icon-button" type="button" title="下一页" aria-label="下一页" disabled={loading || offset + 20 >= page.total} onClick={() => onOffset(offset + 20)}><ChevronRight size={16} /></button></div>}
    {creating && <CreateVersion app={app} onClose={() => setCreating(false)} onCreated={() => { setCreating(false); onCreated(); }} />}
    {viewing && <VersionDetail app={app} release={viewing} active={active} knowledgeBases={knowledgeBases} onClose={() => setViewing(null)} onRestored={(next) => { setViewing(null); onRestored(next); }} />}
  </div>;
}
