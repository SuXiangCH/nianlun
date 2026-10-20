import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../../api/client";
import { Modal } from "../../components/Modal";
import type { ApplicationPublication, WecomChannelState, WecomChannelUpdateInput } from "../../types";

export const WECOM_STALE_MS = 90_000;

export interface WecomStatus {
  label: "已过期" | "状态未知" | "异常" | "在线" | "离线";
  tone: "is-live" | "is-error" | "";
}

export function isWecomExpired(channel: WecomChannelState, now = Date.now()): boolean {
  if (!channel.expires_at) return false;
  const expiresAt = new Date(channel.expires_at).getTime();
  return Number.isFinite(expiresAt) && expiresAt <= now;
}

export function wecomStatus(channel: WecomChannelState, now = Date.now()): WecomStatus {
  if (isWecomExpired(channel, now)) return { label: "已过期", tone: "is-error" };
  const reportedAt = channel.last_reported_at ? new Date(channel.last_reported_at).getTime() : Number.NaN;
  if (!Number.isFinite(reportedAt) || now - reportedAt > WECOM_STALE_MS) return { label: "状态未知", tone: "" };
  if (channel.status === "error") return { label: "异常", tone: "is-error" };
  if (channel.status === "online") return { label: "在线", tone: "is-live" };
  return { label: "离线", tone: "" };
}

export function useWecomNow(channel: WecomChannelState | null | undefined, active = true): number {
  const [now, setNow] = useState(Date.now);
  const expiresAt = channel?.expires_at;
  const reportedAt = channel?.last_reported_at;
  useEffect(() => {
    if (!active) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const update = () => {
      const current = Date.now();
      setNow(current);
      const next = [
        expiresAt ? new Date(expiresAt).getTime() : Number.NaN,
        reportedAt ? new Date(reportedAt).getTime() + WECOM_STALE_MS + 1 : Number.NaN,
      ].filter((deadline) => Number.isFinite(deadline) && deadline > current);
      if (next.length) timer = setTimeout(update, Math.min(Math.min(...next) - current, 2_147_483_647));
    };
    update();
    return () => clearTimeout(timer);
  }, [active, expiresAt, reportedAt]);
  return now;
}

export function WecomStatusBadge({ channel, now }: { channel: WecomChannelState; now?: number }) {
  const clock = useWecomNow(channel, now === undefined);
  const status = wecomStatus(channel, now ?? clock);
  return <span className={`application-state ${status.tone}`} title={channel.error_code || undefined}>{status.label}</span>;
}

export function toDatetimeLocal(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (part: number) => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function WecomChannelFields({ channel, disabled = false }: { channel?: WecomChannelState; disabled?: boolean }) {
  return <>
    <div className="field"><label htmlFor="wecom-bot-id">机器人 Bot ID</label><input id="wecom-bot-id" name="bot_id" required maxLength={256} defaultValue={channel?.bot_id || ""} placeholder="企业微信智能机器人 ID" autoComplete="off" disabled={disabled} /></div>
    <div className="field"><label htmlFor="wecom-secret">Secret</label><input id="wecom-secret" name="secret" type="password" required={!channel} maxLength={512} defaultValue="" placeholder={channel ? "留空表示不修改" : "机器人 Secret"} autoComplete="new-password" disabled={disabled} /><small>Secret 仅用于网关鉴权，保存后不会回填。</small></div>
    <div className="field"><label htmlFor="wecom-default-response">默认回复</label><textarea id="wecom-default-response" name="default_response" required maxLength={1000} defaultValue={channel?.default_response || "暂时无法回答，请稍后再试。"} disabled={disabled} /></div>
    <div className="field"><label htmlFor="wecom-expires-at">到期时间</label><input id="wecom-expires-at" name="expires_at" type="datetime-local" defaultValue={toDatetimeLocal(channel?.expires_at ?? null)} disabled={disabled} /><small>留空表示长期有效，过期后渠道将停止连接。</small></div>
  </>;
}

function expiresAtFromForm(form: FormData): string | null {
  const value = String(form.get("expires_at") || "");
  return value ? new Date(value).toISOString() : null;
}

export function WecomChannelDialog({ appId, channel, onClose, onUpdated }: { appId: string; channel: WecomChannelState; onClose: () => void; onUpdated: (publication: ApplicationPublication) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;
    const form = new FormData(event.currentTarget);
    const secret = String(form.get("secret") || "");
    const body: WecomChannelUpdateInput = {
      expected_config_revision: channel.config_revision,
      bot_id: String(form.get("bot_id") || "").trim(),
      default_response: String(form.get("default_response") || "").trim(),
      expires_at: expiresAtFromForm(form),
      ...(secret ? { secret } : {}),
    };
    setBusy(true); setError("");
    try { onUpdated(await api.updateWecomChannel(appId, body)); }
    catch (failure) { setError(failure instanceof Error ? failure.message : "保存失败，请重试"); setBusy(false); }
  };
  return <Modal title="企业微信渠道" description="配置智能机器人长连接。Secret 不会回显。" onClose={() => { if (!busy) onClose(); }}>
    <form className="form-grid" onSubmit={(event) => void submit(event)}>
      <WecomChannelFields channel={channel} disabled={busy} />
      {error && <p className="application-error" role="alert">{error}</p>}
      <div className="modal-actions"><button type="button" className="outline-button" disabled={busy} onClick={onClose}>取消</button><button type="submit" className="primary-button" disabled={busy}>{busy ? "保存中…" : "保存配置"}</button></div>
    </form>
  </Modal>;
}

export function wecomCreateBody(form: FormData): { bot_id: string; secret: string; default_response: string; expires_at: string | null } {
  return {
    bot_id: String(form.get("bot_id") || "").trim(),
    secret: String(form.get("secret") || ""),
    default_response: String(form.get("default_response") || "").trim(),
    expires_at: expiresAtFromForm(form),
  };
}
