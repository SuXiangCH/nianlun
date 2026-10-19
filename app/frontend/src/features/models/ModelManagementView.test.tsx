// @vitest-environment jsdom

import { act, type ComponentProps } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ModelManagementView } from "./ModelManagementView";
import type { ModelProfile } from "../../types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const profile = (overrides: Partial<ModelProfile> = {}): ModelProfile => ({
  id: "llm-1", kind: "llm", name: "chat-model", model: "chat-model", context_window_tokens: 128000,
  base_url: "https://model.test/v1", api_key_configured: true, enabled: true, api_mode: "saas_precision",
  dimension: null, model_version: "vlm", language: "ch", is_ocr: false, enable_table: true,
  enable_formula: true, page_ranges: "", is_default: false,
  created_at: "2026-10-15T00:00:00Z", updated_at: "2026-10-15T00:00:00Z", ...overrides,
});

describe("Model management", () => {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => { container = document.createElement("div"); document.body.appendChild(container); root = createRoot(container); });
  afterEach(() => { act(() => root.unmount()); container.remove(); vi.restoreAllMocks(); });
  const renderView = async (overrides: Partial<ComponentProps<typeof ModelManagementView>> = {}) => {
    const props: ComponentProps<typeof ModelManagementView> = {
      onListModels: vi.fn().mockResolvedValue([profile(), profile({ id: "embedding-1", kind: "embedding", model: "embed-model", dimension: 1024 })]),
      onCreateModel: vi.fn(), onUpdateModel: vi.fn(), onDeleteModel: vi.fn().mockResolvedValue(null),
      onSetDefaultModel: vi.fn().mockResolvedValue(profile({ is_default: true })),
      onTestModel: vi.fn().mockResolvedValue({ target: "llm", ok: true, message: "连接成功" }),
      onTestConfig: vi.fn(), ...overrides,
    };
    await act(async () => root.render(<ModelManagementView {...props} />));
    return props;
  };
  const click = async (label: string) => {
    const button = Array.from(container.querySelectorAll("button")).find((item) => item.getAttribute("aria-label") === label || item.textContent?.trim() === label);
    expect(button, label).toBeTruthy();
    await act(async () => button!.click());
  };

  it("uses compact navigation and filters the current category without repeated names", async () => {
    await renderView();
    expect(container.querySelector("nav[aria-label='模型功能导航']")?.textContent).toContain("模型");
    expect(container.textContent).not.toContain("按类别维护");
    expect(container.querySelectorAll(".model-profile-copy")[0].textContent?.match(/chat-model/g)).toHaveLength(1);
    expect(container.querySelector("[role=tab][aria-selected=true]")?.textContent).toContain("LLM");
    const search = container.querySelector<HTMLInputElement>("[aria-label='搜索模型']")!;
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(search, "missing");
      search.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(container.textContent).toContain("没有匹配的模型");
    await click("Embedding1");
    expect(container.textContent).toContain("embed-model");
    expect(container.textContent).toContain("1024 维");
    expect(container.querySelector("[aria-label='搜索模型']")?.getAttribute("value")).toBe("");
  });

  it("keeps test results visible and puts default and delete actions in the menu", async () => {
    const props = await renderView();
    expect(container.textContent).not.toContain("设为默认");
    expect(container.textContent).not.toContain("删除模型");
    await click("测试 chat-model");
    expect(props.onTestModel).toHaveBeenCalledWith("llm-1", undefined);
    expect(container.textContent).toContain("连接成功");
    await click("chat-model的更多操作");
    await click("设为默认");
    expect(props.onSetDefaultModel).toHaveBeenCalledWith("llm-1");
    expect(container.querySelector(".model-profile-title")?.textContent).toContain("默认");
    await click("chat-model的更多操作");
    expect(container.querySelector<HTMLButtonElement>("[role=menuitem].danger-button")?.disabled).toBe(true);
    act(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(container.querySelector("[role=menu]")).toBeNull();
  });

  it("preserves existing credentials when saving an edited model without a new key", async () => {
    const props = await renderView({ onUpdateModel: vi.fn().mockResolvedValue(profile()) });
    await click("编辑 chat-model");
    expect(container.querySelector<HTMLInputElement>("#model-profile-key")?.value).toBe("");
    await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(props.onUpdateModel).toHaveBeenCalledWith("llm-1", expect.not.objectContaining({ api_key: expect.anything() }));
    expect(container.querySelector("[role=dialog]")).toBeNull();
  });

  it("confirms deletion and keeps a failed deletion visible", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const props = await renderView({ onDeleteModel: vi.fn().mockRejectedValue(new Error("模型正在使用")) });
    await click("chat-model的更多操作"); await click("删除模型");
    expect(props.onDeleteModel).toHaveBeenCalledWith("llm-1");
    expect(container.textContent).toContain("模型正在使用");
    expect(container.querySelector(".model-profile-title")?.textContent).toContain("chat-model");
  });

  it("keeps loading, empty and failed list states usable", async () => {
    await renderView({ onListModels: vi.fn().mockRejectedValue(new Error("无法加载模型")) });
    expect(container.textContent).toContain("无法加载模型");
    expect(container.querySelector("[aria-label='刷新模型']")).toBeTruthy();
    expect(container.querySelector(".model-toolbar .primary-button")?.textContent).toContain("新建模型");
  });
});
