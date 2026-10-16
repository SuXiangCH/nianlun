import { useEffect, useRef, useState } from "react";
import { ArrowUpRight, Bot, MoreHorizontal, Plus, Search, Trash2 } from "lucide-react";
import { api } from "../../api/client";
import type { Application, ApplicationPublication, KnowledgeBase, ModelProfile } from "../../types";
import { ApplicationDetail } from "./ApplicationDetail";

interface Props { apps: Application[]; knowledgeBases: KnowledgeBase[]; llmModels: ModelProfile[]; selectedAppId: string; onCreate: () => void; onUse: (id: string) => void; onDelete: (id: string) => Promise<void>; onKnowledge: () => void; onModels: () => void; onUpdated?: (app: Application) => void; }
const modelLabel = (profile: ModelProfile): string => profile.model?.trim() || profile.name;
export function ApplicationView({ apps, knowledgeBases, llmModels, selectedAppId, onCreate, onUse, onDelete, onKnowledge, onModels, onUpdated }: Props) {
  const [detailId, setDetailId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [menuId, setMenuId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [publications, setPublications] = useState<Record<string, ApplicationPublication | null>>({});
  const menuRef = useRef<HTMLDivElement>(null);
  const detail = apps.find((item) => item.id === detailId);
  const hasReady = knowledgeBases.some((item) => item.status === "ready");
  const canCreate = hasReady && llmModels.length > 0;
  useEffect(() => {
    if (detailId) return;
    const controller = new AbortController();
    setPublications({});
    apps.forEach((item) => {
      void api.getPublication(item.id, controller.signal).then((value) => {
        if (!controller.signal.aborted) setPublications((current) => ({ ...current, [item.id]: value }));
      }).catch(() => {
        if (!controller.signal.aborted) setPublications((current) => ({ ...current, [item.id]: null }));
      });
    });
    return () => controller.abort();
  }, [apps, detailId]);
  useEffect(() => {
    if (!menuId) return;
    const close = (event: PointerEvent) => { if (!menuRef.current?.contains(event.target as Node)) setMenuId(null); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setMenuId(null); };
    document.addEventListener("pointerdown", close); document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", close); document.removeEventListener("keydown", escape); };
  }, [menuId]);
  const remove = async (id: string) => {
    if (deleting) return;
    setMenuId(null); setDeleting(id); setError(null);
    try { await onDelete(id); }
    catch (failure) { setError(failure instanceof Error ? failure.message : "删除失败，请重试"); }
    finally { setDeleting(null); }
  };
  if (detail) return <ApplicationDetail key={detail.id} app={detail} knowledgeBases={knowledgeBases} llmModels={llmModels} onBack={() => setDetailId(null)} onUse={onUse} onUpdated={onUpdated} />;
  const filtered = apps.filter((item) => `${item.name} ${item.description}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  return <section className="application-workspace application-list-workspace" aria-label="Agent 列表">
    <header className="application-list-toolbar">
      <nav className="application-list-navigation" aria-label="Agent 功能导航">
        <a href="#applications" aria-current="page"><Bot size={18} aria-hidden="true" />Agent</a>
        <span className="application-list-count" role="status">{filtered.length} 个 Agent</span>
      </nav>
      <div className="application-list-actions" role="search" aria-label="筛选 Agent">
        <label className="application-search"><Search size={16} aria-hidden="true" /><input aria-label="搜索 Agent" placeholder="搜索 Agent 名称或描述" value={query} disabled={!apps.length} onChange={(event) => setQuery(event.target.value)} /></label>
        <button className="primary-button" onClick={onCreate} type="button" disabled={!canCreate}><Plus size={15} />新建 Agent</button>
      </div>
    </header>
    {error && <p className="application-error" role="alert">{error}</p>}
    {apps.length ? <>
      <div className="application-list" role="table" aria-label="Agent">
        <div className="application-list-row application-list-labels" role="row"><span role="columnheader">Agent 名称</span><span role="columnheader">知识库 / 模型</span><span role="columnheader">发布状态</span><span role="columnheader">更新日期</span><span role="columnheader">操作</span></div>
        {filtered.map((item) => {
          const kb = knowledgeBases.find((entry) => entry.id === item.knowledge_base_id);
          const llm = llmModels.find((entry) => entry.id === item.llm_model_id) || (!item.llm_model_id ? llmModels.find((entry) => entry.is_default) : undefined);
          const state = publications[item.id];
          return <div className="application-list-row" role="row" key={item.id} aria-busy={deleting === item.id}>
            <div className="application-list-identity" role="cell"><span className="application-avatar"><Bot size={20} /></span><div><button className="application-name" type="button" onClick={() => setDetailId(item.id)}>{item.name}</button><p>{item.description || "暂无描述"}</p>{item.id === selectedAppId && <span className="application-current">当前使用</span>}</div></div>
            <div className="application-list-dependencies" role="cell"><span>{kb?.name || "知识库已移除"}</span><small>{llm ? modelLabel(llm) : "模型不可用"}</small></div>
            <div role="cell"><span className={`application-state ${state?.enabled && state.dependencies_ready ? "is-live" : state === null ? "is-error" : ""}`}>{state === undefined ? "读取中…" : state === null ? "状态读取失败" : !state.active_release ? "未发布" : !state.dependencies_ready ? "依赖异常" : state.enabled ? "API 服务中" : "API 已停用"}</span></div>
            <div className="application-list-date" role="cell">{new Date(item.updated_at).toLocaleDateString("zh-CN")}</div>
            <div className="application-row-actions" role="cell"><button className="quiet-button" onClick={() => onUse(item.id)} type="button">调试<ArrowUpRight size={14} /></button><div className="application-menu" ref={menuId === item.id ? menuRef : undefined}><button className="icon-button" type="button" aria-label={`${item.name}更多操作`} title="更多操作" aria-haspopup="menu" aria-expanded={menuId === item.id} disabled={deleting !== null} onClick={() => setMenuId(menuId === item.id ? null : item.id)}><MoreHorizontal size={18} /></button>{menuId === item.id && <div className="application-menu-popover" role="menu"><button role="menuitem" type="button" onClick={() => { setMenuId(null); setDetailId(item.id); }}>管理 Agent</button><button role="menuitem" className="danger-button" type="button" onClick={() => void remove(item.id)}><Trash2 size={14} />删除 Agent</button></div>}</div></div>
          </div>;
        })}
      </div>
      {!filtered.length && <div className="application-empty">没有匹配的 Agent</div>}
    </> : <div className="application-onboarding"><Bot size={30} /><h3>还没有 Agent</h3><p>{!hasReady ? "请先创建知识库并等待索引就绪" : !llmModels.length ? "请先配置一个 LLM 模型" : "创建第一个知识问答 Agent"}</p>{!hasReady ? <button className="outline-button" onClick={onKnowledge} type="button">去创建知识库</button> : !llmModels.length ? <button className="outline-button" onClick={onModels} type="button">去配置模型</button> : <button className="primary-button" onClick={onCreate} type="button"><Plus size={15} />创建 Agent</button>}</div>}
  </section>;
}
