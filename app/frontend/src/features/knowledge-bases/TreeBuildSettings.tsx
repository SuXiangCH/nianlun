import { useState, type FormEvent } from "react";
import { Save } from "lucide-react";
import type { KnowledgeBase, KnowledgeBaseTreeBuildOptions } from "../../types";

export function TreeBuildSettings({ knowledgeBase, onSave }: {
  knowledgeBase: KnowledgeBase;
  onSave: (options: KnowledgeBaseTreeBuildOptions) => Promise<void>;
}) {
  const [enabled, setEnabled] = useState(knowledgeBase.subtree_folding_enabled);
  const [threshold, setThreshold] = useState(String(knowledgeBase.min_subtree_tokens ?? 1200));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const tokens = Number(threshold);
  const valid = Number.isSafeInteger(tokens) && tokens > 0;
  const dirty = enabled !== knowledgeBase.subtree_folding_enabled || tokens !== (knowledgeBase.min_subtree_tokens ?? 1200);
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!valid || saving) return;
    setSaving(true);
    setError(null);
    try {
      await onSave({ subtree_folding_enabled: enabled, min_subtree_tokens: tokens });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "合并设置保存失败");
    } finally {
      setSaving(false);
    }
  };
  return <form className="tree-build-settings" onSubmit={(event) => void save(event)} aria-busy={saving}>
    <label className="switch-control"><input aria-label="小子树合并" type="checkbox" checked={enabled} disabled={saving} onChange={(event) => setEnabled(event.target.checked)} /><span>小子树合并</span></label>
    <div className="tree-build-settings-controls">
      <label className="field" title="子树总 token 数低于阈值时，将子节点正文并入父节点"><span>合并阈值（tokens）</span><input aria-label="合并阈值（tokens）" type="number" min="1" step="1" required value={threshold} disabled={!enabled || saving} onChange={(event) => setThreshold(event.target.value)} /></label>
      <button className="outline-button" type="submit" disabled={!dirty || !valid || saving}><Save size={14} aria-hidden="true" />{saving ? "保存中" : "保存"}</button>
    </div>
    {error && <div className="document-content-state is-error" role="alert">{error}</div>}
  </form>;
}
