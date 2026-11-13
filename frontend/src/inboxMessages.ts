/** Local headline receipts; no model classifications, source fetching or browser persistence. */
import { validChartDate } from "./charts";

export const inboxVersion = "personal-memory-notifications-v1";
export type InboxSource = "sina_company_news" | "eastmoney_announcements";
export type InboxSourceStatus = "not_checked" | "available" | "empty" | "partial" | "unavailable";
export interface InboxFilters { unread_only: boolean; instrument_id?: string; cursor?: string }
export interface InboxMessage {
  message_id: string; instrument_id: string; name: string; source: InboxSource; title: string; url: string;
  published_date: string; published_at: string | null; publication_precision: "date" | "datetime";
  first_seen_at: string; retrieved_at: string; discovery: "manual" | "research"; baseline: boolean; read: boolean; read_at: string | null;
}
export interface InboxSourceState {
  instrument_id: string; name: string; source: InboxSource; initialized: boolean;
  last_success_at: string | null; last_attempt_at: string | null; status: InboxSourceStatus; code: string;
  skipped_count: number; evicted_unread_count: number; evicted_at: string | null;
}
export interface MessageInboxData {
  version: typeof inboxVersion; enabled: boolean; limit: 20; items: InboxMessage[]; unread_count: number;
  source_states: InboxSourceState[]; collection_revision: string | null; has_more: boolean; next_cursor: string | null;
  sidecar_status?: "inbox_sidecar_saved" | "inbox_sidecar_unavailable" | null;
}
export interface InboxCheckResult {
  version: typeof inboxVersion; enabled: true; request_id: string; replayed: boolean; status: "completed" | "partial" | "cancelled";
  attempted_requests: number; results: { instrument_id: string; source: InboxSource; status: InboxSourceStatus; code: string; saved: number; duplicates: number; filtered_count: number; baseline: boolean }[];
  next_cursor: string | null; remaining_sources: number; checked_stocks: number; total_stocks: number; inbox: MessageInboxData;
}
const obj = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === "object" && !Array.isArray(v);
const keys = (v: Record<string, unknown>, expected: string[]) => Object.keys(v).sort().join() === [...expected].sort().join();
const txt = (v: unknown, max = 600): v is string => typeof v === "string" && v.length > 0 && v.length <= max && v.trim() === v && !/[\u0000-\u001f\u007f]/.test(v);
export const validInboxInstrument = (v: unknown): v is string => typeof v === "string" && /^\d{6}\.(SH|SZ|BJ)$/.test(v);
export const validInboxMessageId = (v: unknown): v is string => typeof v === "string" && /^[0-9a-f]{64}$/.test(v);
export const validInboxRequestId = (v: unknown): v is string => typeof v === "string" && /^[0-9a-f]{32}$/.test(v);
const count = (v: unknown): v is number => typeof v === "number" && Number.isSafeInteger(v) && v >= 0;
const source = (v: unknown): v is InboxSource => v === "sina_company_news" || v === "eastmoney_announcements";
const status = (v: unknown): v is InboxSourceStatus => typeof v === "string" && ["not_checked", "available", "empty", "partial", "unavailable"].includes(v);
const code = (v: unknown): v is string => typeof v === "string" && /^[A-Za-z0-9_]{1,100}$/.test(v);
export const validInboxCursor = (v: unknown): v is string => txt(v, 8192);
const cursor = (v: unknown) => v === null || validInboxCursor(v);
const date = (v: unknown): v is string => typeof v === "string" && validChartDate(v);
function stamp(v: unknown): v is string {
  if (typeof v !== "string") return false;
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/.exec(v);
  return !!match && date(match[1]) && Number(match[2]) < 24 && Number(match[3]) < 60 && Number(match[4]) < 60
    && (match[5] === "Z" || Number(match[5].slice(1, 3)) < 24 && Number(match[5].slice(4)) < 60) && Number.isFinite(Date.parse(v));
}
const nullableStamp = (v: unknown) => v === null || stamp(v);
export function validInboxFilters(filters: InboxFilters) {
  return typeof filters.unread_only === "boolean" && (filters.instrument_id === undefined || validInboxInstrument(filters.instrument_id))
    && (filters.cursor === undefined || validInboxCursor(filters.cursor));
}
export function inboxMessageUrl(value: unknown, itemSource: InboxSource, instrumentId: string): string | null {
  if (typeof value !== "string" || value.length > 2000) return null;
  if (itemSource === "sina_company_news") return /^https:\/\/(?:finance\.sina\.com\.cn\/(?:[A-Za-z0-9_-]+\/)*doc-[a-z0-9]+\.shtml|cj\.sina\.cn\/articles\/view\/[0-9]+\/[A-Za-z0-9]+)$/.test(value) ? value : null;
  return new RegExp(`^https://data\\.eastmoney\\.com/notices/detail/${instrumentId.slice(0, 6)}/AN[0-9]{12,40}\\.html$`).test(value) ? value : null;
}
function message(v: unknown): v is InboxMessage {
  if (!obj(v) || !keys(v, ["message_id", "instrument_id", "name", "source", "title", "url", "published_date", "published_at", "publication_precision", "first_seen_at", "retrieved_at", "discovery", "baseline", "read", "read_at"])
    || !validInboxMessageId(v.message_id) || !validInboxInstrument(v.instrument_id) || !txt(v.name, 80) || !source(v.source)
    || !txt(v.title) || !inboxMessageUrl(v.url, v.source, v.instrument_id) || !date(v.published_date) || !nullableStamp(v.published_at)
    || !["date", "datetime"].includes(String(v.publication_precision)) || !stamp(v.first_seen_at) || !stamp(v.retrieved_at)
    || !["manual", "research"].includes(String(v.discovery)) || typeof v.baseline !== "boolean" || typeof v.read !== "boolean" || !nullableStamp(v.read_at)) return false;
  return (v.publication_precision === "date" ? v.published_at === null : stamp(v.published_at))
    && (v.read ? stamp(v.read_at) : v.read_at === null);
}
function sourceState(v: unknown): v is InboxSourceState {
  return obj(v) && keys(v, ["instrument_id", "name", "source", "initialized", "last_success_at", "last_attempt_at", "status", "code", "skipped_count", "evicted_unread_count", "evicted_at"])
    && validInboxInstrument(v.instrument_id) && txt(v.name, 80) && source(v.source) && typeof v.initialized === "boolean"
    && nullableStamp(v.last_success_at) && nullableStamp(v.last_attempt_at) && status(v.status) && code(v.code)
    && count(v.skipped_count) && count(v.evicted_unread_count) && nullableStamp(v.evicted_at)
    && (v.evicted_unread_count === 0 || stamp(v.evicted_at));
}
export function parseMessageInbox(v: unknown, filters?: InboxFilters): MessageInboxData | null {
  if (!obj(v) || !keys(v, ["version", "enabled", "limit", "items", "unread_count", "source_states", "collection_revision", "has_more", "next_cursor", ...("sidecar_status" in v ? ["sidecar_status"] : [])])
    || v.version !== inboxVersion || typeof v.enabled !== "boolean" || v.limit !== 20 || !Array.isArray(v.items) || v.items.length > 20
    || !(v.sidecar_status == null || v.sidecar_status === "inbox_sidecar_saved" || v.sidecar_status === "inbox_sidecar_unavailable")
    || !v.items.every(message) || new Set(v.items.map(item => item.message_id)).size !== v.items.length
    || !count(v.unread_count) || !Array.isArray(v.source_states) || v.source_states.length > 100 || !v.source_states.every(sourceState)
    || new Set(v.source_states.map(item => `${item.instrument_id}:${item.source}`)).size !== v.source_states.length
    || !(v.collection_revision === null || validInboxMessageId(v.collection_revision)) || typeof v.has_more !== "boolean"
    || !cursor(v.next_cursor) || v.has_more !== (v.next_cursor !== null)) return null;
  if (v.enabled ? v.collection_revision === null : v.collection_revision !== null || v.items.length > 0 || v.source_states.length > 0 || v.unread_count !== 0 || v.has_more) return null;
  if (filters && v.items.some(item => filters.unread_only && item.read || filters.instrument_id && item.instrument_id !== filters.instrument_id)) return null;
  return v as unknown as MessageInboxData;
}
export function parseInboxCheck(v: unknown, requestId: string): InboxCheckResult | null {
  if (!obj(v) || !keys(v, ["version", "enabled", "request_id", "replayed", "status", "attempted_requests", "results", "next_cursor", "remaining_sources", "checked_stocks", "total_stocks", "inbox"])
    || v.version !== inboxVersion || v.enabled !== true || v.request_id !== requestId || !validInboxRequestId(v.request_id) || typeof v.replayed !== "boolean"
    || !["completed", "partial", "cancelled"].includes(String(v.status)) || !count(v.attempted_requests) || v.attempted_requests > 10
    || !Array.isArray(v.results) || v.results.length > 10 || !v.results.every(item => obj(item)
      && keys(item, ["instrument_id", "source", "status", "code", "saved", "duplicates", "filtered_count", "baseline"]) && validInboxInstrument(item.instrument_id)
      && source(item.source) && status(item.status) && code(item.code) && count(item.saved) && item.saved <= 5
      && count(item.duplicates) && item.duplicates <= 5 && item.saved + item.duplicates <= 5
      && count(item.filtered_count) && item.filtered_count <= 50 && typeof item.baseline === "boolean")
    || new Set(v.results.map(item => `${item.instrument_id}:${item.source}`)).size !== v.results.length
    || new Set(v.results.map(item => item.instrument_id)).size > 5 || !cursor(v.next_cursor)
    || !count(v.remaining_sources) || v.remaining_sources > 100 || !count(v.checked_stocks) || !count(v.total_stocks) || v.total_stocks > 50
    || v.checked_stocks > v.total_stocks || !parseMessageInbox(v.inbox)) return null;
  return v as unknown as InboxCheckResult;
}
export const inboxSourceLabel = (value: InboxSource) => value === "sina_company_news" ? "新浪新闻" : "东方财富公告";
export const inboxStatusLabel: Record<InboxSourceStatus, string> = { not_checked: "未检查", available: "已取得标题", empty: "本次有限范围为空", partial: "部分失败", unavailable: "失败" };
export const inboxTime = (value: string) => new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
