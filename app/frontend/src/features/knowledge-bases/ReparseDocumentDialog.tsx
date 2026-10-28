import { useState } from "react";
import { RefreshCw } from "lucide-react";
import { Modal } from "../../components/Modal";
import type { KnowledgeBaseDocument } from "../../types";

export function ReparseDocumentDialog({ document, onReparse, onClose }: {
  document: KnowledgeBaseDocument;
  onReparse: (isOcr: boolean | null) => Promise<void>;
  onClose: () => void;
}) {
  const [ocr, setOcr] = useState("inherit");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    if (saving) return;
    setSaving(true);
    setError(null);
    try {
      await onReparse(ocr === "inherit" ? null : ocr === "true");
      onClose();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "重新解析失败");
    } finally {
      setSaving(false);
    }
  };
  return <Modal title="重新解析" description={document.original_filename} onClose={() => { if (!saving) onClose(); }}>
    <form className="form-grid" aria-busy={saving} onSubmit={(event) => { event.preventDefault(); void submit(); }}>
      <label className="field"><span>OCR</span><select aria-label="重新解析 OCR" value={ocr} disabled={saving} onChange={(event) => setOcr(event.target.value)}><option value="inherit">跟随知识库</option><option value="false">自动识别</option><option value="true">强制 OCR</option></select></label>
      {error && <div className="document-content-state is-error" role="alert">{error}</div>}
      <div className="modal-actions"><button className="outline-button" type="button" disabled={saving} onClick={onClose}>取消</button><button className="primary-button" type="submit" disabled={saving}><RefreshCw size={14} aria-hidden="true" />{saving ? "提交中..." : "重新解析"}</button></div>
    </form>
  </Modal>;
}
