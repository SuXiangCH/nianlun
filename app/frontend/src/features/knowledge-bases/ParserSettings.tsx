import { useEffect, useRef, useState } from "react";
import type { KnowledgeBaseParserOptions } from "../../types";

const inheritedOptions: KnowledgeBaseParserOptions = { is_ocr: null, enable_table: null, enable_formula: null, language: null };
type ParserField = keyof KnowledgeBaseParserOptions;
const optionValue = (value: boolean | null) => value === null ? "inherit" : String(value);

export function ParserSettings({ options = inheritedOptions, onSave }: {
  options: KnowledgeBaseParserOptions;
  onSave: (options: KnowledgeBaseParserOptions) => Promise<void>;
}) {
  const [language, setLanguage] = useState(options.language || "");
  const [saving, setSaving] = useState<ParserField | null>(null);
  const [error, setError] = useState<{ field: ParserField; message: string } | null>(null);
  const pending = useRef(false);
  useEffect(() => { setLanguage(options.language || ""); }, [options.language]);
  const save = async <K extends ParserField>(field: K, value: KnowledgeBaseParserOptions[K]) => {
    if (pending.current || value === options[field]) return;
    pending.current = true;
    setSaving(field);
    setError(null);
    try {
      await onSave({ ...options, [field]: value });
    } catch (cause) {
      setError({ field, message: cause instanceof Error ? cause.message : "解析设置保存失败" });
      if (field === "language") setLanguage(options.language || "");
    } finally {
      pending.current = false;
      setSaving(null);
    }
  };
  const feedback = (field: ParserField) => <>
    {saving === field && <span className="field-help" role="status">保存中...</span>}
    {error?.field === field && <span className="parser-setting-error" role="alert">{error.message}</span>}
  </>;
  return <section className="knowledge-parser-settings" aria-busy={saving !== null}>
    <h3>文档解析</h3>
    {([
      ["is_ocr", "OCR"], ["enable_table", "表格识别"], ["enable_formula", "公式识别"],
    ] as const).map(([field, label]) => <label className="field" key={field}>
      <span>{label}</span>
      <select aria-label={label} value={optionValue(options[field])} disabled={saving !== null} onChange={(event) => void save(field, event.target.value === "inherit" ? null : event.target.value === "true")}>
        <option value="inherit">跟随全局</option>
        <option value="false">{field === "is_ocr" ? "自动识别" : "关闭"}</option>
        <option value="true">{field === "is_ocr" ? "强制 OCR" : "开启"}</option>
      </select>
      {feedback(field)}
    </label>)}
    <label className="field"><span>解析语言</span><input aria-label="解析语言" value={language} maxLength={32} placeholder="跟随全局" disabled={saving !== null} onChange={(event) => setLanguage(event.target.value)} onBlur={() => void save("language", language.trim() || null)} />{feedback("language")}</label>
  </section>;
}
