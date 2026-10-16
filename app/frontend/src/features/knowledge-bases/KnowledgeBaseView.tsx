import { Fragment, useEffect, useRef, useState } from "react";
import type { ChangeEvent, DragEvent, FormEvent, KeyboardEvent } from "react";
import { ArrowLeft, BookOpen, Eye, MoreHorizontal, Plus, RefreshCw, Search, Settings2, Upload, X } from "lucide-react";
import { Modal } from "../../components/Modal";
import type { DocumentIndexTree, KnowledgeBase, KnowledgeBaseDocument, ModelProfile } from "../../types";
import { DocumentReader } from "./DocumentReader";

interface Props {
  items: KnowledgeBase[];
  selectedId: string | null;
  documents: KnowledgeBaseDocument[];
  documentsLoading: boolean;
  documentsError: string | null;
  embeddingModels: ModelProfile[];
  onCreate: () => void;
  onOpen: (knowledgeBaseId: string) => void;
  onBack: () => void;
  onLoadDocuments: (knowledgeBaseId: string) => void;
  onUpload: (knowledgeBaseId: string, files: File[], onProgress?: (loaded: number, total: number) => void, signal?: AbortSignal) => Promise<void>;
  onRebuildFts: (knowledgeBaseId: string) => Promise<void>;
  onRebuildVector: (knowledgeBaseId: string) => Promise<void>;
  onUpdateSummary: (knowledgeBaseId: string, enabled: boolean) => Promise<void>;
  onUpdateHeadingRecovery: (knowledgeBaseId: string, enabled: boolean) => Promise<void>;
  onUpdateName: (knowledgeBaseId: string, name: string) => Promise<void>;
  onUpdateEmbeddingModel: (knowledgeBaseId: string, modelId: string) => Promise<void>;
  onUpdateVectorEnabled: (knowledgeBaseId: string, enabled: boolean) => Promise<void>;
  onDeleteDocument: (knowledgeBaseId: string, documentId: string) => Promise<void>;
  onRetryDocument: (knowledgeBaseId: string, documentId: string) => Promise<void>;
  onDeleteKnowledgeBase: (knowledgeBaseId: string) => Promise<void>;
  onReadContent: (knowledgeBaseId: string, documentId: string) => Promise<string>;
  onReadTree: (knowledgeBaseId: string, documentId: string) => Promise<DocumentIndexTree>;
}

const statusLabel = (status: string) =>
  ({ ready: "可用", creating: "创建中", indexing: "索引中", error: "异常" }[status] || status || "未知");

const documentStageLabel = (document: KnowledgeBaseDocument) => {
  const stage = ({ parse: "解析", normalize: "合并规范化", enrich: "LLM 增强", publish: "发布", index: "构建索引", complete: "处理完成" }[document.current_stage]);
  const state = ({ queued: "排队中", running: "处理中", succeeded: "已完成", partial: "部分可用", failed: "失败", skipped: "已跳过" }[document.stage_state]);
  return `${stage} · ${state}`;
};

const documentProgressLabel = (document: KnowledgeBaseDocument) => {
  if (document.current_stage === "parse" && document.parse.chunks_total > 1) {
    return `${document.parse.chunks_completed}/${document.parse.chunks_total} 段 · ${document.parse.pages_completed}/${document.parse.pages_total} 页`;
  }
  const { completed, total, unit } = document.progress;
  if (completed === null || total === null) return null;
  const unitLabel = ({ pages: "页", chunks: "段", nodes: "节点", documents: "文档" } as Record<string, string>)[unit || ""] || "项";
  return `${completed}/${total} ${unitLabel}`;
};

const ftsStatusLabel = (status: KnowledgeBase["fts_status"]) =>
  ({ disabled: "未构建", pending: "等待构建", building: "构建中", ready: "已就绪", failed: "构建失败" }[status]);
const vectorStatusLabel = (status: KnowledgeBase["vector_status"]) =>
  ({ disabled: "未构建", pending: "等待构建", building: "构建中", ready: "已就绪", failed: "构建失败" }[status]);
const vectorStageLabel = (stage: string | null) =>
  ({ queued: "排队中", starting: "启动中", preparing: "准备中", creating_collection: "创建向量表", embedding: "生成 Embedding", publishing: "发布索引", completed: "已完成", failed: "失败" }[stage || ""] || stage || "等待进度");

const ftsReady = (item: KnowledgeBase) => item.fts_status === "ready" && item.fts_revision === item.content_version;
const vectorReady = (item: KnowledgeBase, embeddingModels: ModelProfile[]) => {
  const model = embeddingModels.find((profile) => profile.id === item.embedding_model_id);
  return Boolean(model && item.vector_status === "ready" && item.vector_revision === item.content_version && item.vector_model_updated_at === model.updated_at && item.vector_dimension === model.dimension);
};
const hasVectorIndex = (item: KnowledgeBase) => item.vector_revision !== null;
const modelLabel = (profile: ModelProfile) => profile.model?.trim() || profile.name;
const formatBytes = (bytes: number) => bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
const supportedFile = (file: File) => [".md", ".pdf", ".doc", ".docx"].some((extension) => file.name.toLowerCase().endsWith(extension));
const vectorProgressLabel = (item: KnowledgeBase) => {
  const stage = vectorStageLabel(item.vector_progress_stage);
  if (item.vector_documents_total === null) return stage;
  const completed = item.vector_documents_completed || 0;
  const records = item.vector_records_processed || 0;
  return `${stage} · ${completed}/${item.vector_documents_total} 个文档 · ${records} 条向量`;
};

export function KnowledgeBaseView({
  items,
  selectedId,
  documents,
  documentsLoading,
  documentsError,
  embeddingModels,
  onCreate,
  onOpen,
  onBack,
  onLoadDocuments,
  onUpload,
  onRebuildFts,
  onRebuildVector,
  onUpdateSummary,
  onUpdateHeadingRecovery,
  onUpdateName,
  onUpdateEmbeddingModel,
  onUpdateVectorEnabled,
  onDeleteDocument,
  onRetryDocument,
  onDeleteKnowledgeBase,
  onReadContent,
  onReadTree,
}: Props) {
  const [contentDocumentId, setContentDocumentId] = useState<string | null>(null);
  const [content, setContent] = useState<string | null>(null);
  const [contentLoading, setContentLoading] = useState(false);
  const [contentError, setContentError] = useState<string | null>(null);
  const [tree, setTree] = useState<DocumentIndexTree | null>(null);
  const [treeLoading, setTreeLoading] = useState(false);
  const [treeError, setTreeError] = useState<string | null>(null);
  const [summarySaving, setSummarySaving] = useState(false);
  const [headingRecoverySaving, setHeadingRecoverySaving] = useState(false);
  const [editingItem, setEditingItem] = useState<KnowledgeBase | null>(null);
  const [nameSaving, setNameSaving] = useState(false);
  const [nameError, setNameError] = useState<string | null>(null);
  const [openMenuId, setOpenMenuId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [documentQuery, setDocumentQuery] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [retryingDocumentId, setRetryingDocumentId] = useState<string | null>(null);
  const [uploadTargetId, setUploadTargetId] = useState<string | null>(null);
  const [queuedFiles, setQueuedFiles] = useState<File[]>([]);
  const [uploadProgress, setUploadProgress] = useState<{ loaded: number; total: number } | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const uploadAbort = useRef<AbortController | null>(null);
  const contentRequestVersion = useRef(0);
  const selected = items.find((item) => item.id === selectedId);
  useEffect(() => {
    setDocumentQuery("");
    setSettingsOpen(false);
    setOpenMenuId(null);
  }, [selectedId]);
  const openUpload = (id: string) => {
    setUploadTargetId(id);
    setQueuedFiles([]);
    setUploadProgress(null);
    setUploadError(null);
  };
  const queueFiles = (files: File[]) => {
    const supported = files.filter(supportedFile);
    const unsupported = files.length - supported.length;
    if (unsupported) setUploadError("已忽略不支持的文件，仅支持 Markdown、PDF 和 Word。");
    if (!supported.length) return;
    setQueuedFiles((current) => {
      const known = new Set(current.map((file) => `${file.name}:${file.size}:${file.lastModified}`));
      const additions = supported.filter((file) => !known.has(`${file.name}:${file.size}:${file.lastModified}`));
      const available = Math.max(0, 50 - current.length);
      if (additions.length > available) setUploadError("单次最多上传 50 个文件，超出的文件未加入。");
      return [...current, ...additions.slice(0, available)];
    });
  };
  const chooseFiles = (event: ChangeEvent<HTMLInputElement>) => {
    queueFiles(Array.from(event.target.files || []));
    event.target.value = "";
  };
  const dropFiles = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    queueFiles(Array.from(event.dataTransfer.files));
  };
  const closeUpload = () => {
    if (uploadProgress) return;
    setUploadTargetId(null);
    setQueuedFiles([]);
    setUploadError(null);
  };
  const submitUpload = async () => {
    if (!uploadTargetId || !queuedFiles.length || uploadProgress) return;
    const controller = new AbortController();
    uploadAbort.current = controller;
    const total = queuedFiles.reduce((sum, file) => sum + file.size, 0);
    setUploadProgress({ loaded: 0, total });
    setUploadError(null);
    try {
      await onUpload(uploadTargetId, queuedFiles, (loaded, reportedTotal) => setUploadProgress({ loaded, total: reportedTotal || total }), controller.signal);
      setUploadTargetId(null);
      setQueuedFiles([]);
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) setUploadError(error instanceof Error ? error.message : "上传失败，请重试。");
    } finally {
      uploadAbort.current = null;
      setUploadProgress(null);
    }
  };
  const cancelUpload = () => uploadAbort.current?.abort();
  const uploadModal = uploadTargetId ? <Modal title="上传文档" description="Markdown、PDF、Word · 单次最多 50 个文件" onClose={closeUpload}>
    <div className="upload-panel">
      <label className={`upload-dropzone ${uploadProgress ? "is-uploading" : ""}`} onDragOver={(event) => event.preventDefault()} onDrop={dropFiles}>
        <input className="file-input" type="file" multiple disabled={Boolean(uploadProgress)} accept=".md,.pdf,.doc,.docx,text/markdown,application/pdf,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document" onChange={chooseFiles} />
        <Upload size={24} aria-hidden="true" /><strong>拖拽文件或点击选择</strong>
      </label>
      {queuedFiles.length > 0 && <div className="upload-file-list">{queuedFiles.map((file) => <div className="upload-file" key={`${file.name}:${file.size}:${file.lastModified}`}><div><strong title={file.name}>{file.name}</strong><span>{formatBytes(file.size)}</span></div>{!uploadProgress && <button className="icon-button" type="button" title="移除文件" aria-label={`移除 ${file.name}`} onClick={() => setQueuedFiles((current) => current.filter((item) => item !== file))}><X size={16} /></button>}</div>)}</div>}
      {uploadProgress && <div className="upload-progress" role="status"><div><span>正在上传 {queuedFiles.length} 个文件</span><strong>{uploadProgress.total ? `${Math.min(100, Math.round(uploadProgress.loaded / uploadProgress.total * 100))}%` : "处理中"}</strong></div><progress value={uploadProgress.loaded} max={uploadProgress.total || 1} /></div>}
      {uploadError && <div className="upload-error" role="alert">{uploadError}</div>}
      <div className="modal-actions"><button className="outline-button" type="button" disabled={Boolean(uploadProgress)} onClick={closeUpload}>取消</button>{uploadProgress ? <button className="quiet-button danger-button" type="button" onClick={cancelUpload}>取消上传</button> : <button className="primary-button" type="button" disabled={!queuedFiles.length} onClick={() => void submitUpload()}>开始上传</button>}</div>
    </div>
  </Modal> : null;
  useEffect(() => () => uploadAbort.current?.abort(), []);
  const openContent = async (documentId: string) => {
    if (!selected) return;
    const requestVersion = ++contentRequestVersion.current;
    setContentDocumentId(documentId);
    setContent(null);
    setContentError(null);
    setContentLoading(true);
    setTree(null);
    setTreeError(null);
    setTreeLoading(true);
    // The tree is a companion view: a missing tree must not block the content.
    void onReadTree(selected.id, documentId).then((nextTree) => {
      if (requestVersion === contentRequestVersion.current) setTree(nextTree);
    }, (error: unknown) => {
      if (requestVersion === contentRequestVersion.current) setTreeError(error instanceof Error ? error.message : "无法读取索引树");
    }).finally(() => {
      if (requestVersion === contentRequestVersion.current) setTreeLoading(false);
    });
    try {
      const nextContent = await onReadContent(selected.id, documentId);
      if (requestVersion === contentRequestVersion.current) setContent(nextContent);
    } catch (error) {
      if (requestVersion === contentRequestVersion.current) setContentError(error instanceof Error ? error.message : "无法读取解析内容");
    } finally {
      if (requestVersion === contentRequestVersion.current) setContentLoading(false);
    }
  };
  const closeContent = () => {
    ++contentRequestVersion.current;
    setContentDocumentId(null);
    setContent(null);
    setContentError(null);
    setTree(null);
    setTreeError(null);
  };
  const openCard = (id: string) => {
    setOpenMenuId(null);
    closeContent();
    onOpen(id);
  };
  const onCardKeyDown = (event: KeyboardEvent<HTMLElement>, id: string) => {
    if (event.target !== event.currentTarget) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openCard(id);
    }
  };
  const confirmRebuild = (item: KnowledgeBase) => {
    const action = ftsReady(item) ? "重建" : "构建";
    if (!window.confirm(`确定${action}知识库“${item.name}”的全文索引吗？原有全文索引会被替换，期间可能短暂无法检索。`)) return;
    void onRebuildFts(item.id);
  };
  const confirmVectorRebuild = (item: KnowledgeBase) => {
    const action = hasVectorIndex(item) ? "重建" : "构建";
    if (!window.confirm(`确定${action}知识库“${item.name}”的向量索引吗？将调用 Embedding 模型并可能产生费用，构建期间语义检索可能暂时不可用。`)) return;
    void onRebuildVector(item.id);
  };
  const changeEmbeddingModel = (modelId: string) => {
    if (!selected || modelId === selected.embedding_model_id) return;
    if (selected.vector_enabled && !window.confirm("切换 Embedding 模型需要重新生成全部向量，可能产生模型调用费用。构建期间将暂时使用全文检索，确定继续吗？")) return;
    void onUpdateEmbeddingModel(selected.id, modelId);
  };
  const toggleSummary = async (event: ChangeEvent<HTMLInputElement>) => {
    if (!selected) return;
    setSummarySaving(true);
    try {
      await onUpdateSummary(selected.id, event.target.checked);
    } catch {
      // Keep the controlled checkbox unchanged until the server confirms it.
    } finally {
      setSummarySaving(false);
    }
  };
  const toggleHeadingRecovery = async (event: ChangeEvent<HTMLInputElement>) => {
    if (!selected) return;
    setHeadingRecoverySaving(true);
    try {
      await onUpdateHeadingRecovery(selected.id, event.target.checked);
    } catch {
      // Keep the controlled checkbox unchanged until the server confirms it.
    } finally {
      setHeadingRecoverySaving(false);
    }
  };
  const submitName = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!editingItem || nameSaving) return;
    const name = String(new FormData(event.currentTarget).get("name") || "").trim();
    if (!name) return;
    setNameSaving(true);
    setNameError(null);
    try {
      await onUpdateName(editingItem.id, name);
      setEditingItem(null);
    } catch (error) {
      setNameError(error instanceof Error ? error.message : "名称更新失败，请重试。");
    } finally {
      setNameSaving(false);
    }
  };
  useEffect(() => {
    if (!openMenuId) return;
    const closeMenu = (event: PointerEvent) => {
      if (!(event.target instanceof Element) || !event.target.closest(".kb-card-menu")) setOpenMenuId(null);
    };
    const closeMenuOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") setOpenMenuId(null);
    };
    document.addEventListener("pointerdown", closeMenu);
    document.addEventListener("keydown", closeMenuOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeMenu);
      document.removeEventListener("keydown", closeMenuOnEscape);
    };
  }, [openMenuId]);

  if (selected) {
    const filteredDocuments = documents.filter((document) => document.original_filename.toLocaleLowerCase().includes(documentQuery.trim().toLocaleLowerCase()));
    return (
      <section className="knowledge-workspace">
        <header className="knowledge-toolbar">
          <button className="icon-button" onClick={onBack} type="button" title="返回知识库" aria-label="返回知识库"><ArrowLeft size={18} /></button>
          <div className="knowledge-title"><h2>{selected.name}</h2>{selected.description && <p>{selected.description}</p>}</div>
          <div className="knowledge-toolbar-actions">
            <button className="icon-button" onClick={() => setSettingsOpen(true)} type="button" title="知识库设置" aria-label="知识库设置"><Settings2 size={18} /></button>
            <button className="primary-button knowledge-upload-button" onClick={() => openUpload(selected.id)} type="button" title="上传文档" aria-label="上传文档"><Upload size={15} /><span>上传文档</span></button>
          </div>
        </header>
        <div className="knowledge-document-toolbar">
          <div className="knowledge-document-label"><strong>文档</strong><span role="status">{filteredDocuments.length}</span></div>
          <div className="knowledge-index-status"><span className={`knowledge-status ${ftsReady(selected) ? "ready" : selected.fts_status === "ready" ? "pending" : selected.fts_status}`}>全文 {ftsReady(selected) ? "已就绪" : selected.fts_status === "ready" ? "待更新" : ftsStatusLabel(selected.fts_status)}</span><span className={`knowledge-status ${!selected.vector_enabled ? "disabled" : vectorReady(selected, embeddingModels) ? "ready" : selected.vector_status === "ready" ? "pending" : selected.vector_status}`}>向量 {selected.vector_enabled ? (vectorReady(selected, embeddingModels) ? "已就绪" : selected.vector_status === "ready" ? "待更新" : vectorStatusLabel(selected.vector_status)) : "已关闭"}</span></div>
          <label className="knowledge-search"><Search size={16} aria-hidden="true" /><input aria-label="搜索文档" placeholder="搜索文档" value={documentQuery} onChange={(event) => setDocumentQuery(event.target.value)} /></label>
          <button className="icon-button" onClick={() => onLoadDocuments(selected.id)} type="button" title="刷新文档" aria-label="刷新文档" disabled={documentsLoading}><RefreshCw size={16} className={documentsLoading ? "knowledge-spinning" : undefined} /></button>
        </div>
        {settingsOpen && <Modal title="知识库设置" description="" onClose={() => setSettingsOpen(false)}><div className="knowledge-settings">
          <section><h3>检索索引</h3><div className="knowledge-setting-row"><span>全文索引</span><button className="outline-button" onClick={() => confirmRebuild(selected)} type="button" disabled={selected.fts_status === "pending" || selected.fts_status === "building"}>{ftsReady(selected) ? "重建全文索引" : "构建全文索引"}</button></div>
            <label className="field detail-vector-model"><span>Embedding 模型</span><select value={selected.embedding_model_id || ""} onChange={(event) => changeEmbeddingModel(event.target.value)}><option value="" disabled>请选择 Embedding 模型</option>{embeddingModels.map((profile) => <option key={profile.id} value={profile.id}>{modelLabel(profile)}</option>)}</select></label>
            <div className="knowledge-setting-row"><label className="switch-control detail-vector-switch"><input type="checkbox" checked={selected.vector_enabled} disabled={!selected.embedding_model_id} onChange={(event) => void onUpdateVectorEnabled(selected.id, event.target.checked)} /><span>向量检索</span></label><button className="outline-button" onClick={() => confirmVectorRebuild(selected)} type="button" disabled={!selected.vector_enabled || !selected.embedding_model_id || selected.vector_status === "pending" || selected.vector_status === "building"}>{hasVectorIndex(selected) ? "重建向量索引" : "构建向量索引"}</button></div>
          </section>
          <section><h3>文档处理</h3><label className="switch-control detail-summary-switch"><input type="checkbox" checked={selected.summary_enabled} disabled={summarySaving} onChange={(event) => void toggleSummary(event)} /><span>生成摘要</span></label><label className="switch-control detail-heading-recovery-switch" title="仅影响之后上传的 MinerU PDF"><input type="checkbox" checked={selected.heading_recovery_enabled} disabled={headingRecoverySaving} onChange={(event) => void toggleHeadingRecovery(event)} /><span>修复 PDF 标题</span></label></section>
          <div className="modal-actions"><button className="primary-button" type="button" onClick={() => setSettingsOpen(false)}>完成</button></div>
        </div></Modal>}
        {(selected.vector_status === "pending" || selected.vector_status === "building") && <div className="document-content-state">向量索引进度：{vectorProgressLabel(selected)}</div>}
        {selected.vector_error && <div className="document-content-state is-error" role="alert">向量索引：{selected.vector_error}</div>}
        {selected.fts_error && <div className="document-content-state is-error" role="alert">全文索引：{selected.fts_error}</div>}
        {documentsError && <div className="document-content-state is-error" role="alert">{documentsError}</div>}
        {documentsLoading ? <div className="document-list-state">正在加载文档...</div> : documents.length ? (
          <div className="document-list">
            {filteredDocuments.map((document) => (
              <Fragment key={document.id}>
                <article className="document-row">
                  <div className="document-row-main"><div className="document-file-icon" aria-hidden="true">{document.file_extension === ".pdf" ? "PDF" : document.file_extension === ".doc" || document.file_extension === ".docx" ? "DOC" : "MD"}</div><div className="document-row-copy"><strong title={document.original_filename}>{document.original_filename}</strong><span>{formatBytes(document.size_bytes)}</span>{document.error_message && <small>{document.error_message}</small>}</div></div>
                  <div className="document-row-meta"><span className={`knowledge-status ${document.status === "ready" ? "ready" : document.status === "failed" ? "error" : "indexing"}`}>{document.status === "ready" ? "可用" : documentStageLabel(document)}</span>{document.status !== "ready" && documentProgressLabel(document) && <span>{documentProgressLabel(document)}</span>}{document.warnings.length > 0 && <span>{document.warnings.length} 条警告</span>}{Boolean(document.published_content_version) && document.status !== "failed" && <button className="icon-button" type="button" title="查看解析内容" aria-label={`查看 ${document.original_filename}`} onClick={() => void openContent(document.id)}><Eye size={17} /><span className="knowledge-sr-only">查看解析内容</span></button>}{document.status === "failed" && <button className="outline-button" type="button" disabled={retryingDocumentId !== null} onClick={async () => { setRetryingDocumentId(document.id); try { await onRetryDocument(selected.id, document.id); } finally { setRetryingDocumentId(null); } }}>{retryingDocumentId === document.id ? "重试中..." : `重试${document.failed_stage ? ({ parse: "解析", normalize: "规范化", enrich: "增强", index: "索引" }[document.failed_stage]) : "处理"}`}</button>}<div className="kb-card-menu"><button className="icon-button" type="button" title="更多操作" aria-label={`${document.original_filename}的更多操作`} aria-haspopup="menu" aria-expanded={openMenuId === `document:${document.id}`} onClick={() => setOpenMenuId(openMenuId === `document:${document.id}` ? null : `document:${document.id}`)}><MoreHorizontal size={18} /></button>{openMenuId === `document:${document.id}` && <div className="kb-card-menu-popover" role="menu"><button className="kb-card-menu-item is-danger" role="menuitem" type="button" onClick={() => { setOpenMenuId(null); void onDeleteDocument(selected.id, document.id); }}>删除文档</button></div>}</div></div>
                </article>
                {contentDocumentId === document.id && (
                  <DocumentReader
                    title={document.original_filename}
                    content={content}
                    contentLoading={contentLoading}
                    contentError={contentError}
                    tree={tree}
                    treeLoading={treeLoading}
                    treeError={treeError}
                    onClose={closeContent}
                  />
                )}
              </Fragment>
            ))}
          </div>
        ) : <div className="document-list-state">暂无文档</div>}
        {!documentsLoading && documents.length > 0 && !filteredDocuments.length && <div className="document-list-state">没有匹配的文档</div>}
        {uploadModal}
      </section>
    );
  }

  const filteredItems = items.filter((item) => `${item.name} ${item.description}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  return (
    <section className="knowledge-workspace">
      <header className="knowledge-toolbar"><nav className="knowledge-navigation" aria-label="知识库功能导航"><a href="#knowledge" aria-current="page"><BookOpen size={18} />知识库</a><span role="status">{filteredItems.length} 个</span></nav><div className="knowledge-toolbar-actions" role="search" aria-label="筛选知识库"><label className="knowledge-search"><Search size={16} aria-hidden="true" /><input aria-label="搜索知识库" placeholder="搜索知识库" value={query} disabled={!items.length} onChange={(event) => setQuery(event.target.value)} /></label><button className="primary-button" onClick={onCreate} type="button"><Plus size={15} />新建知识库</button></div></header>
      {items.length ? (
        <div className="knowledge-list">
          <div className="knowledge-list-labels"><span>知识库名称</span><span>文档数</span><span>状态</span><span>更新日期</span><span /></div>
          {filteredItems.map((item) => (
            <article className="list-item is-clickable" key={item.id} role="button" tabIndex={0} onClick={() => openCard(item.id)} onKeyDown={(event) => onCardKeyDown(event, item.id)}>
                <div className="list-item-main">
                  <div className="list-item-icon" aria-hidden="true"><BookOpen size={19} /></div>
                  <div className="list-item-copy"><strong title={item.name}>{item.name}</strong>{item.description && <p>{item.description}</p>}</div>
                </div>
                <span className="knowledge-document-count">{item.document_count || 0}</span>
                <span className={`knowledge-status ${item.status}`}>{statusLabel(item.status)}</span>
                <time className="knowledge-updated" dateTime={item.updated_at}>{new Date(item.updated_at).toLocaleDateString("zh-CN")}</time>
                <div className="kb-card-menu" onClick={(event) => event.stopPropagation()}>
                  <button className="icon-button kb-card-menu-trigger" onClick={() => setOpenMenuId((current) => current === item.id ? null : item.id)} type="button" title="更多操作" aria-label={`${item.name}的更多操作`} aria-haspopup="menu" aria-expanded={openMenuId === item.id}><MoreHorizontal size={18} /></button>
                  {openMenuId === item.id && <div className="kb-card-menu-popover" role="menu"><button className="kb-card-menu-item" onClick={() => { setOpenMenuId(null); setNameError(null); setEditingItem(item); }} type="button" role="menuitem">编辑名称</button><button className="kb-card-menu-item is-danger" onClick={() => { setOpenMenuId(null); void onDeleteKnowledgeBase(item.id); }} type="button" role="menuitem">删除知识库</button></div>}
                </div>
            </article>
          ))}
        </div>
      ) : <div className="knowledge-empty"><BookOpen size={28} /><h3>暂无知识库</h3></div>}
      {items.length > 0 && !filteredItems.length && <div className="knowledge-empty">没有匹配的知识库</div>}
      {editingItem && <Modal title="编辑知识库名称" description="" onClose={() => { if (!nameSaving) setEditingItem(null); }}><form className="form-grid" onSubmit={(event) => void submitName(event)}><div className="field"><label htmlFor="kb-edit-name">名称</label><input id="kb-edit-name" name="name" required maxLength={128} defaultValue={editingItem.name} /></div>{nameError && <div className="upload-error" role="alert">{nameError}</div>}<div className="modal-actions"><button className="outline-button" onClick={() => setEditingItem(null)} type="button" disabled={nameSaving}>取消</button><button className="primary-button" type="submit" disabled={nameSaving}>{nameSaving ? "保存中..." : "保存"}</button></div></form></Modal>}
      {uploadModal}
    </section>
  );
}
