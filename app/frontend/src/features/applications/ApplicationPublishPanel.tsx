import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowRight, Braces, Building2, RefreshCw, Upload } from "lucide-react";
import { api } from "../../api/client";
import { Modal } from "../../components/Modal";
import type { Application, ApplicationPublication, ApplicationRelease, KnowledgeBase, ModelProfile, PublishedApplicationConfig } from "../../types";
import { useReleaseOptions, VersionSettings, versionError } from "./ApplicationVersions";
import { isWecomExpired, useWecomNow } from "./ApplicationWecomChannel";

type PublishChannel = "api" | "wecom";

interface Props {
  app: Application;
  publication: ApplicationPublication;
  initialRelease: ApplicationRelease | null;
  initialChannel?: PublishChannel;
  draftDirty: boolean;
  knowledgeBases: KnowledgeBase[];
  llmModels: ModelProfile[];
  onClose: () => void;
  onPublished: (publication: ApplicationPublication, channel: PublishChannel) => void;
  onVersionCreated: () => void;
  onRefreshed: (draft: Application, publication: ApplicationPublication, models: ModelProfile[]) => void;
}

export function ApplicationPublishPanel({ app, publication, initialRelease, initialChannel, draftDirty, knowledgeBases, llmModels, onClose, onPublished, onVersionCreated, onRefreshed }: Props) {
  const [state, setState] = useState(publication);
  const [draft, setDraft] = useState(app);
  const [models, setModels] = useState(llmModels);
  const refreshController = useRef<AbortController | null>(null);
  useEffect(() => () => refreshController.current?.abort(), []);
  const initial = initialChannel && (initialChannel === "api" ? publication.api_channel_created : Boolean(publication.wecom_channel))
    ? initialChannel
    : publication.api_channel_created ? "api" : "wecom";
  const [channel, setChannel] = useState<PublishChannel>(initial);
  const [source, setSource] = useState(initialRelease?.id || (publication.draft_ready ? "draft" : ""));
  const [description, setDescription] = useState("");
  const [created, setCreated] = useState<ApplicationRelease | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const options = useReleaseOptions(app.id, initialRelease);
  const target = options.items.find((item) => item.id === source) || (created?.id === source ? created : null);
  const model = draft.llm_model_id ? models.find((item) => item.id === draft.llm_model_id) : models.find((item) => item.kind === "llm" && item.is_default);
  const preview: Partial<PublishedApplicationConfig> = target?.config || {
    agent_version: draft.agent_version, knowledge_base_id: draft.knowledge_base_id,
    llm_model_id: model?.id || draft.llm_model_id || undefined,
    clarification_enabled: draft.clarification_enabled ?? true,
    model: model?.model || undefined, base_url: model?.base_url || undefined,
    context_window_tokens: model?.context_window_tokens || 128000,
  };
  const wecom = state.wecom_channel;
  const wecomNow = useWecomNow(wecom, channel === "wecom");
  const channelOptions = useMemo(() => [
    ...(state.api_channel_created ? [{ type: "api" as const, label: "API 服务" }] : []),
    ...(wecom ? [{ type: "wecom" as const, label: "企业微信" }] : []),
  ], [state.api_channel_created, wecom]);
  const channelReady = channel === "wecom" ? Boolean(wecom) : state.api_channel_created;
  const expired = channel === "wecom" && wecom ? isWecomExpired(wecom, wecomNow) : false;
  const activeRelease = channel === "wecom" ? wecom?.active_release ?? null : state.active_release;
  const deploymentVersion = channel === "wecom" ? wecom?.deployment_version ?? 0 : state.deployment_version;
  const channelEnabled = channel === "wecom" ? Boolean(wecom?.enabled) : state.enabled;
  const draftBlocked = source === "draft" && (draftDirty || !state.draft_ready);
  const noChange = Boolean(target && target.id === activeRelease?.id);
  const publish = async () => {
    if (busy || !source || draftBlocked || !channelReady || expired || noChange) return;
    if (channel === "wecom" && wecom && isWecomExpired(wecom)) {
      setError("企业微信渠道已过期，请更新有效期后再发布。");
      return;
    }
    setBusy(true); setError("");
    try {
      let release = target;
      if (source === "draft") {
        release = await api.createRelease(app.id, { expected_config_version: draft.config_version ?? state.config_version, description });
        setCreated(release); options.add(release); setSource(release.id); onVersionCreated();
      }
      if (!release) throw new Error("请选择可用版本");
      const updated = await api.deployRelease(app.id, { channel_type: channel, release_id: release.id, expected_deployment_version: deploymentVersion });
      onPublished(updated, channel);
    } catch (failure) { setError(versionError(failure)); }
    finally { setBusy(false); }
  };
  const refresh = async () => {
    setBusy(true); setError("");
    const controller = new AbortController();
    refreshController.current = controller;
    try {
      const [latestDraft, latestState, latestModels] = await Promise.all([
        api.getApp(app.id, controller.signal), api.getPublication(app.id, controller.signal), api.listModels("llm", controller.signal),
      ]);
      if (controller.signal.aborted) return;
      setState(latestState); setModels(latestModels);
      if (!draftDirty) setDraft(latestDraft);
      onRefreshed(latestDraft, latestState, latestModels);
    }
    catch (failure) { if (!controller.signal.aborted) setError(versionError(failure)); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  };

  return <Modal title="发布 Agent" description="" onClose={() => { if (!busy) onClose(); }}>
    <form className="application-publish-panel" onSubmit={(event) => { event.preventDefault(); void publish(); }}>
      <div className="application-publish-fields">
        <div className="field"><label htmlFor="publish-source">发布内容</label><select id="publish-source" value={source} disabled={busy} onChange={(event) => { setSource(event.target.value); setError(""); }}><option value="" disabled>选择发布版本</option><option value="draft">最新已保存草稿 · 创建新版本</option>{options.items.map((release) => <option value={release.id} key={release.id}>r{release.sequence}{release.description ? ` · ${release.description}` : ""}</option>)}</select></div>
        {options.loading && <p className="application-muted" role="status">正在加载版本…</p>}
        {options.hasMore && <button type="button" className="application-text-button" disabled={busy || options.loading} onClick={options.loadMore}>加载更多版本</button>}
        {options.error && <p className="application-error" role="alert">{options.error}<button className="quiet-button" type="button" disabled={busy} onClick={options.retry}>重试</button></p>}
        {source === "draft" && <div className="field"><label htmlFor="publish-description">版本说明</label><textarea id="publish-description" value={description} maxLength={500} disabled={busy} placeholder="填写本次变更" onChange={(event) => setDescription(event.target.value)} /></div>}
        {draftBlocked && <p className="application-error" role="alert">{draftDirty ? "请先保存草稿，再发布最新配置。" : state.draft_error || "草稿当前不可发布"}</p>}
        {created && <p className="application-version-created" role="status">版本 r{created.sequence} 已创建{error ? "，尚未完成渠道发布" : ""}</p>}
        <div className="field"><label htmlFor="publish-channel">目标渠道</label><select id="publish-channel" value={channel} disabled={busy || channelOptions.length < 2} onChange={(event) => setChannel(event.target.value === "wecom" ? "wecom" : "api")}>{channelOptions.map((option) => <option key={option.type} value={option.type} disabled={option.type === "wecom" && wecom ? isWecomExpired(wecom) : false}>{option.label}{option.type === "wecom" && wecom && isWecomExpired(wecom) ? " · 已过期" : ""}</option>)}</select></div>
        {!channelReady && <p className="application-error" role="alert">请先创建目标发布渠道</p>}
        {expired && <p className="application-error" role="alert">企业微信渠道已过期，请更新有效期后再发布。</p>}
        <section className="application-deploy-target"><span className={`application-channel-icon ${channel === "api" ? "api" : "wecom"}`}>{channel === "api" ? <Braces size={20} /> : <Building2 size={20} />}</span><div><strong>{channel === "api" ? "API 服务" : "企业微信"}</strong><span>{activeRelease ? `r${activeRelease.sequence}` : "未发布"}<ArrowRight size={14} />{target ? `r${target.sequence}` : source === "draft" ? "新版本" : "—"}</span></div><span className={`application-state ${channelEnabled ? "is-live" : ""}`}>{!activeRelease ? "首次发布后启用" : channelEnabled ? "保持启用" : "保持停用"}</span></section>
        {target?.description && <p className="application-version-description">{target.description}</p>}
        {source && <section className="application-deploy-summary"><h3>配置变更</h3><VersionSettings config={preview} previous={activeRelease?.config} knowledgeBases={knowledgeBases} /></section>}
        {noChange && <p className="application-muted" role="status">{channel === "api" ? "API 服务" : "企业微信"}已使用此版本</p>}
        <p className="application-deploy-note">新会话使用目标版本，已有会话保留原版本。知识库使用最新已提交内容。</p>
        {error && <div className="application-deploy-error"><p className="application-error" role="alert">{error}</p><button className="application-text-button" type="button" disabled={busy} onClick={() => void refresh()}><RefreshCw size={14} />刷新发布状态</button></div>}
      </div>
      <div className="modal-actions"><button className="outline-button" type="button" disabled={busy} onClick={onClose}>取消</button><button className="primary-button" type="submit" aria-label="确认发布" disabled={busy || options.loading || !source || draftBlocked || !channelReady || expired || noChange}><Upload size={15} />{busy ? "发布中…" : "确认发布"}</button></div>
    </form>
  </Modal>;
}
