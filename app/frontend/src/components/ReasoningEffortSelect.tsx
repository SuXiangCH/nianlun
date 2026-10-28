import type { SelectHTMLAttributes } from "react";
import type { ReasoningEffort, ReasoningEffortOverride } from "../types";

export function ReasoningEffortSelect({ override = false, ...props }: SelectHTMLAttributes<HTMLSelectElement> & { override?: boolean }) {
  return <select {...props}>
    {override && <option value="inherit">跟随模型</option>}
    <option value={override ? "default" : ""}>不配置</option>
    <option value="low">低</option>
    <option value="medium">中</option>
    <option value="high">高</option>
  </select>;
}

export const reasoningEffortLabel = (effort: ReasoningEffort | null | undefined): string => effort ? { low: "低", medium: "中", high: "高" }[effort] : "不配置";

export const resolveReasoningEffort = (override: ReasoningEffortOverride | undefined, model: ReasoningEffort | null | undefined): ReasoningEffort | null => override === "default" ? null : !override || override === "inherit" ? model ?? null : override;
