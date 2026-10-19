// @vitest-environment jsdom

import { act, type ComponentProps } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ModelManagementView } from "./ModelManagementView";
import type { ModelProfile } from "../../types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const profile = (overrides: Partial<ModelProfile> = {}): ModelProfile => ({
  id: "llm-1", kind: "llm", name: "chat-model", model: "chat-model", context_window_tokens: 128000,
  supports_vision: null,
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

  it("edits and saves all three vision states without exposing the field for embedding models", async () => {
    const props = await renderView({ onUpdateModel: vi.fn().mockResolvedValue(profile()) });
    for (const [value, expected] of [["true", true], ["false", false], ["", null]] as const) {
      await click("编辑 chat-model");
      const select = container.querySelector<HTMLSelectElement>("#model-profile-vision");
      expect(select).not.toBeNull();
      await act(async () => {
        select!.value = value;
        select!.dispatchEvent(new Event("change", { bubbles: true }));
      });
      await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
      expect(props.onUpdateModel).toHaveBeenLastCalledWith("llm-1", expect.objectContaining({ supports_vision: expected }));
    }
    await click("Embedding1"); await click("编辑 embed-model");
    expect(container.querySelector("#model-profile-vision")).toBeNull();
  });

  it("defaults new models to unconfirmed vision and preserves an existing selection", async () => {
    await renderView({ onListModels: vi.fn().mockResolvedValue([profile({ supports_vision: true })]) });
    expect(container.querySelector(".model-vision-status")?.textContent).toContain("支持视觉");
    await click("编辑 chat-model");
    expect(container.querySelector<HTMLSelectElement>("#model-profile-vision")?.value).toBe("true");
    await click("取消"); await click("新建模型");
    expect(container.querySelector<HTMLSelectElement>("#model-profile-vision")?.value).toBe("");
  });

  it("distinguishes unsupported vision from an unconfirmed capability in the list", async () => {
    await renderView({ onListModels: vi.fn().mockResolvedValue([
      profile({ id: "supported", supports_vision: true }),
      profile({ id: "unsupported", supports_vision: false }),
      profile({ id: "unconfirmed", supports_vision: null }),
    ]) });
    expect(Array.from(container.querySelectorAll(".model-vision-status"), (item) => item.textContent)).toEqual(["支持视觉", "不支持视觉", "视觉未确认"]);
  });

  it("tests the unsaved vision setting and displays text and image results separately", async () => {
    const props = await renderView({ onTestModel: vi.fn().mockResolvedValue({ target: "llm", ok: false, message: "图片检测失败", checks: [
      { capability: "text", status: "passed", message: "连接成功" },
      { capability: "vision", status: "failed", message: "接口拒绝图片请求" },
    ] }) });
    await click("编辑 chat-model");
    await act(async () => {
      const select = container.querySelector<HTMLSelectElement>("#model-profile-vision")!;
      select.value = "true"; select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await click("测试连接");
    expect(props.onTestModel).toHaveBeenCalledWith("llm-1", expect.objectContaining({ supports_vision: true }));
    expect(container.querySelectorAll(".model-check-result")).toHaveLength(2);
    expect(container.querySelector(".model-check-result.is-passed")?.textContent).toContain("文本连接");
    expect(container.querySelector(".model-check-result.is-failed")?.textContent).toContain("图片输入");
    expect(container.querySelector<HTMLSelectElement>("#model-profile-vision")?.value).toBe("true");
  });

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
