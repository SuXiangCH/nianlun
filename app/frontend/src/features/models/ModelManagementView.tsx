import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Modal } from "../../components/Modal";
import type {
  ModelConfigTestRequest,
  ModelConfigTestResult,
  ModelKind,
  ModelProfile,
  ModelProfileRequest,
} from "../../types";

interface Props {
  onListModels: () => Promise<ModelProfile[]>;
  onCreateModel: (config: ModelProfileRequest) => Promise<ModelProfile>;
  onUpdateModel: (id: string, config: ModelProfileRequest) => Promise<ModelProfile>;
  onDeleteModel: (id: string) => Promise<null>;
  onSetDefaultModel: (id: string) => Promise<ModelProfile>;
  onTestModel: (id: string, config?: ModelConfigTestRequest) => Promise<ModelConfigTestResult>;
  onTestConfig: (config: ModelConfigTestRequest) => Promise<ModelConfigTestResult>;
}

type ModelDraft = Omit<ModelProfileRequest, "api_key"> & { api_key: string };

const categoryMeta: Record<ModelKind, { label: string; kicker: string; description: string }> = {
  llm: { label: "LLM", kicker: "01 / CHAT", description: "管理对话模型和 OpenAI-compatible Chat API。" },
  embedding: { label: "Embedding", kicker: "02 / RETRIEVAL", description: "管理语义检索模型和向量维度；具体知识库在知识库页面选择模型。" },
  parser: { label: "文档解析", kicker: "03 / INGESTION", description: "管理 MinerU SaaS 或私有部署解析配置。" },
};

const parserProfileLabel = (apiMode: "saas_precision" | "self_hosted") => (
  apiMode === "self_hosted" ? "MinerU 私有部署" : "MinerU SaaS"
);

const newDraft = (kind: ModelKind): ModelDraft => ({
  kind,
  name: "",
  model: null,
  context_window_tokens: null,
  base_url: "",
  api_key: "",
  enabled: true,
  api_mode: "saas_precision",
  dimension: null,
  model_version: "vlm",
  language: "ch",
  is_ocr: false,
  enable_table: true,
  enable_formula: true,
  page_ranges: "",
  is_default: false,
});

const draftFromProfile = (profile: ModelProfile): ModelDraft => ({
  kind: profile.kind,
  name: profile.name,
  model: profile.model,
  context_window_tokens: profile.context_window_tokens,
  base_url: profile.base_url || "",
  api_key: "",
  enabled: true,
  api_mode: profile.api_mode,
  dimension: profile.dimension,
  model_version: profile.model_version,
  language: profile.language,
  is_ocr: profile.is_ocr,
  enable_table: profile.enable_table,
  enable_formula: profile.enable_formula,
  page_ranges: profile.page_ranges,
  is_default: profile.is_default,
});

const testRequestFromDraft = (draft: ModelDraft, clearKey: boolean): ModelConfigTestRequest => ({
  target: draft.kind,
  model: draft.model,
  base_url: draft.base_url,
  dimension: draft.dimension,
  ...(draft.kind === "parser" ? { api_mode: draft.api_mode } : {}),
  ...(clearKey ? { api_key: null } : draft.api_key.trim() ? { api_key: draft.api_key.trim() } : {}),
});

const requestFromDraft = (draft: ModelDraft, clearKey: boolean): ModelProfileRequest => {
  const { api_key, ...values } = draft;
  return {
    ...values,
    name: draft.kind === "parser" ? parserProfileLabel(draft.api_mode) : draft.model?.trim() || "",
    ...(clearKey ? { api_key: null } : api_key.trim() ? { api_key: api_key.trim() } : {}),
  };
};

export function ModelManagementView({
  onListModels,
  onCreateModel,
  onUpdateModel,
  onDeleteModel,
  onSetDefaultModel,
  onTestModel,
  onTestConfig,
}: Props) {
  const [profiles, setProfiles] = useState<ModelProfile[]>([]);
  const [kind, setKind] = useState<ModelKind>("llm");
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");
  const [editor, setEditor] = useState<{ id: string | null; draft: ModelDraft } | null>(null);
  const [clearKey, setClearKey] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, ModelConfigTestResult>>({});

  const editorTestKey = editor ? `draft:${editor.id ?? "new"}` : null;

  const clearEditorTestResult = () => {
    if (!editorTestKey) return;
    setTestResults((current) => {
      const { [editorTestKey]: _discarded, ...remaining } = current;
      return remaining;
    });
  };

  const loadProfiles = useCallback(() => {
    setLoading(true);
    return onListModels()
      .then((items) => setProfiles(items))
      .catch((error: unknown) => setMessage(error instanceof Error ? error.message : "模型列表加载失败"))
      .finally(() => setLoading(false));
  }, [onListModels]);

  useEffect(() => {
    void loadProfiles();
  }, [loadProfiles]);

  const openCreate = () => {
    setEditor({ id: null, draft: newDraft(kind) });
    setClearKey(false);
    setMessage("");
  };

  const openEdit = (profile: ModelProfile) => {
    setEditor({ id: profile.id, draft: draftFromProfile(profile) });
    setClearKey(false);
    setMessage("");
  };

  const updateDraft = <K extends keyof ModelDraft>(key: K, value: ModelDraft[K]) => {
    setEditor((current) => current ? { ...current, draft: { ...current.draft, [key]: value } } : current);
    setMessage("");
    clearEditorTestResult();
  };

  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!editor) return;
    setSaving(true);
    setMessage("");
    try {
      const request = requestFromDraft(editor.draft, clearKey);
      const saved = editor.id
        ? await onUpdateModel(editor.id, request)
        : await onCreateModel(request);
      setProfiles((current) => editor.id
        ? current.map((item) => item.id === saved.id ? saved : item)
        : [...current, saved]);
      setKind(saved.kind);
      clearEditorTestResult();
      setEditor(null);
      setClearKey(false);
      setMessage("模型配置已保存");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "模型配置保存失败");
    } finally {
      setSaving(false);
    }
  };

  const testProfile = async (profile: ModelProfile, override?: ModelConfigTestRequest) => {
    setTesting(profile.id);
    try {
      const result = await onTestModel(profile.id, override);
      setTestResults((current) => ({ ...current, [profile.id]: result }));
    } catch (error) {
      setTestResults((current) => ({
        ...current,
        [profile.id]: {
          target: profile.kind,
          ok: false,
          message: error instanceof Error ? error.message : "模型连接测试失败",
        },
      }));
    } finally {
      setTesting(null);
    }
  };

  const testEditor = async () => {
    if (!editor || !editorTestKey) return;
    const request = testRequestFromDraft(editor.draft, clearKey);
    const missing = !editor.draft.base_url.trim()
      ? "请先配置 API URL"
      : editor.draft.kind !== "parser" && !editor.draft.model?.trim()
        ? "请先配置模型名称"
        : null;
    if (missing) {
      setTestResults((current) => ({
        ...current,
        [editorTestKey]: {
          target: editor.draft.kind,
          ok: false,
          message: missing,
        },
      }));
      return;
    }
    setTesting(editorTestKey);
    try {
      const result = editor.id
        ? await onTestModel(editor.id, request)
        : await onTestConfig(request);
      setTestResults((current) => ({ ...current, [editorTestKey]: result }));
    } catch (error) {
      setTestResults((current) => ({
        ...current,
        [editorTestKey]: {
          target: editor.draft.kind,
          ok: false,
          message: error instanceof Error ? error.message : "模型连接测试失败",
        },
      }));
    } finally {
      setTesting(null);
    }
  };

  const setDefault = async (profile: ModelProfile) => {
    setTesting(`default:${profile.id}`);
    try {
      const saved = await onSetDefaultModel(profile.id);
      setProfiles((current) => current.map((item) => item.kind === saved.kind
        ? { ...item, is_default: item.id === saved.id }
        : item));
      setMessage(`${profile.name} 已设为默认模型`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "默认模型切换失败");
    } finally {
      setTesting(null);
    }
  };

  const remove = async (profile: ModelProfile) => {
    if (profile.is_default || !window.confirm(`确定删除模型“${profile.name}”吗？`)) return;
    try {
      await onDeleteModel(profile.id);
      setProfiles((current) => current.filter((item) => item.id !== profile.id));
      setMessage("模型已删除");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "模型删除失败");
    }
  };

  const visibleProfiles = profiles.filter((item) => item.kind === kind);
  const meta = categoryMeta[kind];
  const editorResult = editorTestKey ? testResults[editorTestKey] : undefined;

  return (
    <section className="model-management model-catalog">
      <div className="section-heading">
        <div>
          <h2>模型管理</h2>
          <p>按类别维护模型连接配置。LLM 和文档解析可设置默认项，Embedding 由知识库单独选择。</p>
        </div>
        <button className="primary-button" type="button" onClick={openCreate}>+ 新建{meta.label}模型</button>
      </div>

      <div className="model-category-tabs" role="tablist" aria-label="模型类别">
        {(Object.keys(categoryMeta) as ModelKind[]).map((item) => (
          <button
            key={item}
            className={kind === item ? "is-active" : ""}
            type="button"
            role="tab"
            aria-selected={kind === item}
            onClick={() => { setKind(item); setMessage(""); }}
          >
            {categoryMeta[item].label}
            <span>{profiles.filter((profile) => profile.kind === item).length}</span>
          </button>
        ))}
      </div>

      <div className="model-catalog-heading">
        <div>
          <span className="model-section-kicker">{meta.kicker}</span>
          <h3>{meta.label}</h3>
          <p>{meta.description}</p>
        </div>
        <span className="tag ready">{visibleProfiles.length} 个配置</span>
      </div>

      {loading ? <div className="loading" role="status">正在加载模型列表...</div> : visibleProfiles.length ? (
        <div className="model-profile-list">
          {visibleProfiles.map((profile) => {
            const result = testResults[profile.id];
            return (
              <article className="model-profile-item" key={profile.id}>
                <div className="model-profile-main">
                  <div className="model-profile-icon" aria-hidden="true">{profile.kind === "llm" ? "◌" : profile.kind === "embedding" ? "∿" : "▤"}</div>
                  <div className="model-profile-copy">
                    <div className="model-profile-title">
                      <strong>{profile.kind === "parser" ? parserProfileLabel(profile.api_mode) : profile.model || "模型名称未配置"}</strong>
                      {profile.kind !== "embedding" && profile.is_default && <span className="tag ready">默认</span>}
                    </div>
                    <p>{profile.model || (profile.kind === "parser" ? `${profile.model_version} · ${profile.language}` : "模型名称未配置")}</p>
                    <small>{profile.base_url || "URL 未配置"} · {profile.api_key_configured ? "Key 已配置" : "Key 未配置"}</small>
                  </div>
                </div>
                <div className="model-profile-meta">
                  {profile.kind === "embedding" && <span>{profile.dimension ? `${profile.dimension} 维` : "默认维度"}</span>}
                  {result && <span className={`model-test-result ${result.ok ? "is-success" : "is-error"}`}>{result.message}</span>}
                  <div className="model-profile-actions">
                    <button className="quiet-button" type="button" disabled={testing !== null} onClick={() => void testProfile(profile)}>测试</button>
                    {profile.kind !== "embedding" && !profile.is_default && <button className="quiet-button" type="button" disabled={testing !== null} onClick={() => void setDefault(profile)}>设为默认</button>}
                    <button className="outline-button" type="button" disabled={testing !== null} onClick={() => openEdit(profile)}>编辑</button>
                    <button className="quiet-button danger-button" type="button" disabled={profile.is_default || testing !== null} onClick={() => void remove(profile)}>删除</button>
                  </div>
                </div>
              </article>
            );
          })}
        </div>
      ) : (
        <div className="empty-state model-catalog-empty">
          <div>
            <div className="empty-state-mark" aria-hidden="true">+</div>
            <h2>还没有{meta.label}模型</h2>
            <p>创建一个配置后，可以在这里测试并设置默认模型。</p>
            <button className="primary-button" type="button" onClick={openCreate}>创建第一个</button>
          </div>
        </div>
      )}

      {message && <p className="model-catalog-message" role="status">{message}</p>}

      {editor && (
        <Modal
          title={`${editor.id ? "编辑" : "新建"}${categoryMeta[editor.draft.kind].label}模型`}
          description={editor.draft.kind === "embedding" ? "保存后可在知识库页面选择此 Embedding 模型。" : "保存后可将此配置设为当前类别的默认模型。"}
          onClose={() => {
            if (!saving && testing === null) {
              clearEditorTestResult();
              setEditor(null);
            }
          }}
        >
          <form className="form-grid model-editor" onSubmit={save}>
            {editor.draft.kind !== "parser" && <div className="field">
              <label htmlFor="model-profile-model">模型名称</label>
              <input id="model-profile-model" required maxLength={256} value={editor.draft.model || ""} disabled={saving || testing !== null} onChange={(event) => updateDraft("model", event.target.value || null)} placeholder="例如：gpt-4.1-mini" />
            </div>}
            {editor.draft.kind === "llm" && <div className="field">
              <label htmlFor="model-profile-context-window">上下文窗口（tokens）</label>
              <input id="model-profile-context-window" type="number" min={1} max={10000000} step={1} value={editor.draft.context_window_tokens ?? ""} disabled={saving || testing !== null} onChange={(event) => updateDraft("context_window_tokens", event.target.value ? Number(event.target.value) : null)} placeholder="例如：128000" />
              <span className="field-help">达到该模型上下文窗口的 80% 时自动压缩历史对话。</span>
            </div>}
            {editor.draft.kind === "parser" && <>
              <div className="field">
                <label htmlFor="model-parser-mode">对接模式</label>
                <select id="model-parser-mode" value={editor.draft.api_mode} disabled={saving || testing !== null} onChange={(event) => updateDraft("api_mode", event.target.value as "saas_precision" | "self_hosted")}>
                  <option value="saas_precision">MinerU SaaS Precision</option>
                  <option value="self_hosted">MinerU 私有部署</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="model-parser-version">解析模型</label>
                <select id="model-parser-version" value={editor.draft.model_version} disabled={saving || testing !== null} onChange={(event) => updateDraft("model_version", event.target.value as "pipeline" | "vlm")}>
                  <option value="vlm">VLM · 复杂版式</option>
                  <option value="pipeline">Pipeline · 标准解析</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="model-parser-language">文档语言</label>
                <select id="model-parser-language" value={editor.draft.language} disabled={saving || testing !== null} onChange={(event) => updateDraft("language", event.target.value)}>
                  <option value="ch">中英文</option>
                  <option value="ch_server">中英日</option>
                  <option value="en">英文</option>
                  <option value="japan">日文为主</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="model-parser-pages">页码范围（可选）</label>
                <input id="model-parser-pages" maxLength={256} value={editor.draft.page_ranges} disabled={saving || testing !== null} onChange={(event) => updateDraft("page_ranges", event.target.value)} placeholder="例如：1-20 或 2,4-6" />
              </div>
            </>}
            {editor.draft.kind === "embedding" && <>
              <div className="field">
                <label htmlFor="model-embedding-dimension">向量维度</label>
                <input id="model-embedding-dimension" type="number" min={1} max={100000} step={1} value={editor.draft.dimension ?? ""} disabled={saving || testing !== null} onChange={(event) => updateDraft("dimension", event.target.value ? Number(event.target.value) : null)} placeholder="使用默认维度" />
                <span className="field-help">知识库选择此模型后，切换维度需要在对应知识库重新构建向量索引。</span>
              </div>
            </>}
            <div className="field">
              <label htmlFor="model-profile-url">API URL</label>
              <input id="model-profile-url" required type="url" maxLength={2048} value={editor.draft.base_url || ""} disabled={saving || testing !== null} onChange={(event) => updateDraft("base_url", event.target.value)} placeholder={editor.draft.kind === "parser" && editor.draft.api_mode === "self_hosted" ? "例如：http://mineru.internal:8000" : editor.draft.kind === "parser" ? "例如：https://mineru.net" : "例如：https://api.example.com/v1"} />
            </div>
            <div className="field">
              <label htmlFor="model-profile-key">{editor.draft.kind === "parser" && editor.draft.api_mode === "self_hosted" ? "API Key（可选）" : "API Key"}</label>
              <input id="model-profile-key" type="password" maxLength={4096} autoComplete="new-password" value={editor.draft.api_key} disabled={saving || testing !== null} onChange={(event) => { updateDraft("api_key", event.target.value); setClearKey(false); }} placeholder={editor.draft.kind === "parser" && editor.draft.api_mode === "self_hosted" ? "私有服务默认无需；网关鉴权时填写" : editor.id ? "已配置时留空保持不变" : "输入 API Key"} />
              {editor.id && <label className="secret-clear"><input type="checkbox" checked={clearKey} disabled={saving || testing !== null} onChange={(event) => setClearKey(event.target.checked)} />清除已保存 Key</label>}
            </div>
            {editor.draft.kind === "parser" && <div className="parser-toggle-grid model-editor-toggles">
              <label className="parser-toggle"><input type="checkbox" checked={editor.draft.is_ocr} disabled={saving || testing !== null} onChange={(event) => updateDraft("is_ocr", event.target.checked)} /><span><strong>OCR</strong><small>扫描 PDF 文字识别</small></span></label>
              <label className="parser-toggle"><input type="checkbox" checked={editor.draft.enable_table} disabled={saving || testing !== null} onChange={(event) => updateDraft("enable_table", event.target.checked)} /><span><strong>表格识别</strong><small>保留表格结构</small></span></label>
              <label className="parser-toggle"><input type="checkbox" checked={editor.draft.enable_formula} disabled={saving || testing !== null} onChange={(event) => updateDraft("enable_formula", event.target.checked)} /><span><strong>公式识别</strong><small>识别数学公式</small></span></label>
            </div>}
            {editor.draft.kind !== "embedding" && <label className="secret-clear"><input type="checkbox" checked={editor.draft.is_default} disabled={saving || testing !== null} onChange={(event) => updateDraft("is_default", event.target.checked)} />设为默认模型</label>}
            {message && <span className="model-test-result is-error" role="alert">{message}</span>}
            {editorResult && <span className={`model-test-result ${editorResult.ok ? "is-success" : "is-error"}`} role={editorResult.ok ? "status" : "alert"}>{editorResult.message}</span>}
            <div className="modal-actions">
              <button className="quiet-button" type="button" disabled={saving || testing !== null} onClick={() => void testEditor()}>{testing === editorTestKey ? "测试中..." : "测试连接"}</button>
              <button className="outline-button" type="button" disabled={saving || testing !== null} onClick={() => setEditor(null)}>取消</button>
              <button className="primary-button" type="submit" disabled={saving || testing !== null}>{saving ? "保存中..." : "保存"}</button>
            </div>
          </form>
        </Modal>
      )}
    </section>
  );
}
