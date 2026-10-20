// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isWecomExpired, toDatetimeLocal, wecomStatus, WecomChannelFields, WecomStatusBadge } from "./ApplicationWecomChannel";
import type { WecomChannelState } from "../../types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const channel: WecomChannelState = {
  id: "wecom-1", bot_id: "bot-1", has_secret: true, default_response: "稍后再试",
  enabled: true, active_release: null, deployment_version: 0, config_revision: 1,
  status: "offline", error_code: null, expires_at: null, has_unpublished_changes: false,
  dependencies_ready: true, dependency_error: null, last_deployed_at: null, last_reported_at: null,
};
const now = Date.parse("2026-10-20T12:00:00Z");

describe("WeCom channel status", () => {
  it("prefers expiry, then stale reports, then the reported connection state", () => {
    expect(wecomStatus({ ...channel, expires_at: "2026-10-19T00:00:00Z" }, now)).toEqual({ label: "已过期", tone: "is-error" });
    expect(wecomStatus({ ...channel, last_reported_at: null }, now)).toEqual({ label: "状态未知", tone: "" });
    expect(wecomStatus({ ...channel, last_reported_at: new Date(now - 90_001).toISOString() }, now).label).toBe("状态未知");
    expect(wecomStatus({ ...channel, status: "error", error_code: "AUTH_FAILED", last_reported_at: new Date(now).toISOString() }, now)).toEqual({ label: "异常", tone: "is-error" });
    expect(wecomStatus({ ...channel, status: "online", last_reported_at: new Date(now).toISOString() }, now)).toEqual({ label: "在线", tone: "is-live" });
    expect(wecomStatus({ ...channel, status: "offline", last_reported_at: new Date(now).toISOString() }, now)).toEqual({ label: "离线", tone: "" });
  });

  it("treats only timestamps at or before now as expired", () => {
    expect(isWecomExpired({ ...channel, expires_at: new Date(now).toISOString() }, now)).toBe(true);
    expect(isWecomExpired({ ...channel, expires_at: new Date(now + 1).toISOString() }, now)).toBe(false);
    expect(isWecomExpired(channel, now)).toBe(false);
  });

  it("round-trips an expiry into the datetime-local field", () => {
    const local = toDatetimeLocal("2026-10-20T12:34:00Z");
    expect(local).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/);
    expect(new Date(local).getTime()).toBe(Date.parse("2026-10-20T12:34:00Z"));
    expect(toDatetimeLocal(null)).toBe("");
  });
});

describe("WeCom channel fields", () => {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => { container = document.createElement("div"); document.body.appendChild(container); root = createRoot(container); });
  afterEach(() => { act(() => root.unmount()); container.remove(); vi.useRealTimers(); });

  it("ages an online badge into unknown without another render", async () => {
    vi.useFakeTimers(); vi.setSystemTime(now);
    act(() => { root.render(<WecomStatusBadge channel={{ ...channel, status: "online", last_reported_at: new Date(now).toISOString() }} />); });
    expect(container.textContent).toBe("在线");
    await act(async () => { await vi.advanceTimersByTimeAsync(90_000); });
    expect(container.textContent).toBe("在线");
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(container.textContent).toBe("状态未知");
  });

  it("expires a badge while idle and clears its clock when unmounted", async () => {
    vi.useFakeTimers(); vi.setSystemTime(now);
    act(() => { root.render(<WecomStatusBadge channel={{ ...channel, status: "online", last_reported_at: new Date(now).toISOString(), expires_at: new Date(now + 10_000).toISOString() }} />); });
    expect(container.textContent).toBe("在线");
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(container.textContent).toBe("已过期");
    act(() => { root.render(null); });
    expect(vi.getTimerCount()).toBe(0);
  });

  it("never prefills the secret and keeps the bot id for editing", () => {
    act(() => { root.render(<form><WecomChannelFields channel={channel} /></form>); });
    expect(container.querySelector<HTMLInputElement>("#wecom-secret")?.value).toBe("");
    expect(container.querySelector<HTMLInputElement>("#wecom-secret")?.placeholder).toBe("留空表示不修改");
    expect(container.querySelector<HTMLInputElement>("#wecom-bot-id")?.value).toBe("bot-1");
    expect(container.querySelector<HTMLInputElement>("#wecom-secret")?.required).toBe(false);
  });

  it("requires the secret when creating a channel", () => {
    act(() => { root.render(<form><WecomChannelFields /></form>); });
    expect(container.querySelector<HTMLInputElement>("#wecom-secret")?.required).toBe(true);
  });
});
