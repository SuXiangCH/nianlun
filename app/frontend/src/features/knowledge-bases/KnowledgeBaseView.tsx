import { Fragment, useEffect, useRef, useState } from "react";
import type { ChangeEvent, DragEvent, FormEvent, KeyboardEvent } from "react";
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

const documentStatusLabel = (status: KnowledgeBaseDocument["status"]) =>
  ({ uploaded: "已上传", parsing: "解析中", parsed: "已解析", indexing: "建立索引", ready: "可检索", failed: "解析失败", deleted: "已删除" }[status]);

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
  const [editingItem, setEditingItem] = useState<KnowledgeBase | null>(null);
  const [nameSaving, setNameSaving] = useState(false);
  const [nameError, setNameError] = useState<string | null>(null);
  const [openMenuId, setOpenMenuId] = useState<string | null>(null);
  const [retryingDocumentId, setRetryingDocumentId] = useState<string | null>(null);
  const [uploadTargetId, setUploadTargetId] = useState<string | null>(null);
  const [queuedFiles, setQueuedFiles] = useState<File[]>([]);
  const [uploadProgress, setUploadProgress] = useState<{ loaded: number; total: number } | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const uploadAbort = useRef<AbortController | null>(null);
  const contentRequestVersion = useRef(0);
  const selected = items.find((item) => item.id === selectedId);
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
  const uploadModal = uploadTargetId ? <Modal title="上传文档" description="选择文件后确认上传。文件提交后会继续在后台解析和建立索引。" onClose={closeUpload}>
    <div className="upload-panel">
      <label className={`upload-dropzone ${uploadProgress ? "is-uploading" : ""}`} onDragOver={(event) => event.preventDefault()} onDrop={dropFiles}>
        <input className="file-input" type="file" multiple disabled={Boolean(uploadProgress)} accept=".md,.pdf,.doc,.docx,text/markdown,application/pdf,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document" onChange={chooseFiles} />
        <strong>拖拽文件到这里，或点击选择文件</strong><span>支持 Markdown、PDF、Word，单次最多 50 个文件</span>
      </label>
      {queuedFiles.length > 0 && <div className="upload-file-list">{queuedFiles.map((file) => <div className="upload-file" key={`${file.name}:${file.size}:${file.lastModified}`}><div><strong title={file.name}>{file.name}</strong><span>{formatBytes(file.size)}</span></div>{!uploadProgress && <button className="icon-button" type="button" title="移除文件" aria-label={`移除 ${file.name}`} onClick={() => setQueuedFiles((current) => current.filter((item) => item !== file))}>×</button>}</div>)}</div>}
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
    return (
      <section>
        <div className="detail-heading">
          <button className="quiet-button" onClick={onBack} type="button">← 知识库</button>
          <div className="detail-heading-main">
            <div className="detail-kicker">知识库详情</div>
            <h2>{selected.name}</h2>
            <p>{selected.description || "未填写描述"}</p>
          </div>
          <div className="detail-actions">
            <button className="primary-button" onClick={() => openUpload(selected.id)} type="button">上传文档</button>
          </div>
        </div>
        <div className="detail-summary">
          <span>{selected.document_count || 0} 个文档</span>
          <span className={`tag ${selected.status}`}>{statusLabel(selected.status)}</span>
          <span className={`tag ${ftsReady(selected) ? "ready" : selected.fts_status}`}>全文 {ftsReady(selected) ? "已就绪" : ftsStatusLabel(selected.fts_status)}</span>
          <button className="outline-button" onClick={() => confirmRebuild(selected)} type="button" disabled={selected.fts_status === "pending" || selected.fts_status === "building"}>{ftsReady(selected) ? "重建全文索引" : "构建全文索引"}</button>
          <span className={`tag ${vectorReady(selected, embeddingModels) ? "ready" : selected.vector_status}`}>向量 {selected.vector_enabled ? (vectorReady(selected, embeddingModels) ? "已就绪" : vectorStatusLabel(selected.vector_status)) : "已关闭"}</span>
          <label className="field detail-vector-model"><span>Embedding 模型</span><select value={selected.embedding_model_id || ""} onChange={(event) => changeEmbeddingModel(event.target.value)}><option value="" disabled>请选择 Embedding 模型</option>{embeddingModels.map((profile) => <option key={profile.id} value={profile.id}>{modelLabel(profile)}</option>)}</select></label>
          <label className="switch-control detail-vector-switch"><input type="checkbox" checked={selected.vector_enabled} disabled={!selected.embedding_model_id} onChange={(event) => void onUpdateVectorEnabled(selected.id, event.target.checked)} /><span>{selected.vector_enabled ? "向量检索已开启" : "向量检索已关闭"}</span></label>
          <button className="outline-button" onClick={() => confirmVectorRebuild(selected)} type="button" disabled={!selected.vector_enabled || !selected.embedding_model_id || selected.vector_status === "pending" || selected.vector_status === "building"}>{hasVectorIndex(selected) ? "重建向量索引" : "构建向量索引"}</button>
          <button className="quiet-button" onClick={() => onLoadDocuments(selected.id)} type="button">刷新文档</button><label className="switch-control detail-summary-switch"><input type="checkbox" checked={selected.summary_enabled} disabled={summarySaving} onChange={(event) => void toggleSummary(event)} /><span>{selected.summary_enabled ? "摘要生成已开启" : "摘要生成已关闭"}</span></label>
        </div>
        {(selected.vector_status === "pending" || selected.vector_status === "building") && <div className="document-content-state">向量索引进度：{vectorProgressLabel(selected)}</div>}
        {selected.vector_error && <div className="document-content-state is-error">向量索引：{selected.vector_error}</div>}
        {documentsError && <div className="document-content-state is-error">{documentsError}</div>}
        {documentsLoading ? <div className="document-list-state">正在加载文档...</div> : documents.length ? (
          <div className="document-list">
            {documents.map((document) => (
              <Fragment key={document.id}>
                <article className="document-row">
                  <div className="document-row-main"><div className="document-file-icon" aria-hidden="true">{document.file_extension === ".pdf" ? "PDF" : document.file_extension === ".doc" || document.file_extension === ".docx" ? "DOC" : "MD"}</div><div className="document-row-copy"><strong title={document.original_filename}>{document.original_filename}</strong><span>{formatBytes(document.size_bytes)} · {document.parser === "mineru" ? "MinerU Precision" : "Markdown"}</span>{document.error_message && <small>{document.error_message}</small>}</div></div>
                  <div className="document-row-meta"><span className={`tag ${document.status === "ready" ? "ready" : document.status === "failed" ? "error" : "indexing"}`}>{documentStatusLabel(document.status)}</span>{document.latest_task?.extracted_pages && <span>{document.latest_task.extracted_pages}{document.latest_task.total_pages ? `/${document.latest_task.total_pages}` : ""} 页</span>}<button className="outline-button" type="button" disabled={!document.parsed_content_version || document.status === "failed"} onClick={() => void openContent(document.id)}>查看解析内容</button>{document.status === "failed" && <button className="outline-button" type="button" disabled={retryingDocumentId !== null} onClick={async () => { setRetryingDocumentId(document.id); try { await onRetryDocument(selected.id, document.id); } finally { setRetryingDocumentId(null); } }}>{retryingDocumentId === document.id ? "重试中..." : "重试解析"}</button>}<button className="quiet-button danger-button" type="button" onClick={() => void onDeleteDocument(selected.id, document.id)}>删除文档</button></div>
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
        ) : <div className="document-list-state">这个知识库还没有文档，上传 Markdown、PDF 或 Word 开始建立内容。</div>}
        {uploadModal}
      </section>
    );
  }

  return (
    <section>
      <div className="section-heading"><div><h2>知识库</h2><p>为 Agent 准备可检索、可持续更新的资料。</p></div><div className="section-actions"><button className="primary-button" onClick={onCreate} type="button">+ 新建知识库</button></div></div>
      {items.length ? (
        <div className="list-grid">
          {items.map((item) => (
            <article className="list-item is-clickable" key={item.id} role="button" tabIndex={0} onClick={() => openCard(item.id)} onKeyDown={(event) => onCardKeyDown(event, item.id)}>
              <div className="kb-card-heading">
                <div className="list-item-main">
                  <div className="list-item-icon" aria-hidden="true">▤</div>
                  <div className="list-item-copy"><strong title={item.name}>{item.name}</strong><p>{item.description || "未填写描述"}</p></div>
                </div>
                <div className="kb-card-menu" onClick={(event) => event.stopPropagation()}>
                  <button className="icon-button kb-card-menu-trigger" onClick={() => setOpenMenuId((current) => current === item.id ? null : item.id)} type="button" title="更多操作" aria-label={`${item.name}的更多操作`} aria-haspopup="menu" aria-expanded={openMenuId === item.id}>⋯</button>
                  {openMenuId === item.id && <div className="kb-card-menu-popover" role="menu"><button className="kb-card-menu-item" onClick={() => { setOpenMenuId(null); setNameError(null); setEditingItem(item); }} type="button" role="menuitem">编辑名称</button><button className="kb-card-menu-item is-danger" onClick={() => { setOpenMenuId(null); void onDeleteKnowledgeBase(item.id); }} type="button" role="menuitem">删除知识库</button></div>}
                </div>
              </div>
              <div className="kb-card-meta"><span>{item.document_count || 0} 个文档</span><span className={`tag ${item.status}`}>{statusLabel(item.status)}</span></div>
            </article>
          ))}
        </div>
      ) : <div className="empty-state"><div><div className="empty-state-mark" aria-hidden="true">▤</div><h2>还没有知识库</h2><p>创建一个知识库，然后上传 Markdown、PDF 或 Word 文档。</p><button className="primary-button" onClick={onCreate} type="button">创建第一个知识库</button></div></div>}
      {editingItem && <Modal title="编辑知识库名称" description="修改后不会影响已有文档和索引。" onClose={() => { if (!nameSaving) setEditingItem(null); }}><form className="form-grid" onSubmit={(event) => void submitName(event)}><div className="field"><label htmlFor="kb-edit-name">名称</label><input id="kb-edit-name" name="name" required maxLength={128} defaultValue={editingItem.name} /></div>{nameError && <div className="upload-error" role="alert">{nameError}</div>}<div className="modal-actions"><button className="outline-button" onClick={() => setEditingItem(null)} type="button" disabled={nameSaving}>取消</button><button className="primary-button" type="submit" disabled={nameSaving}>{nameSaving ? "保存中..." : "保存"}</button></div></form></Modal>}
      {uploadModal}
    </section>
  );
}
