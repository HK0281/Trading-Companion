import type { PublicTurn } from "./api";

export interface AnswerPresentation {
  version: "answer-presentation-v1";
  kind: "research" | "knowledge" | "followup";
  indicator_ids?: string[];
  request_id: string | null;
  instrument_id: string | null;
  trade_date: string | null;
  headline: string;
  sections: { id: string; title: string; items: string[] }[];
}

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === "string" && value.length > 0 && value.length <= 2000;

export interface ConversationAnswer {
  version: "conversation-answer-v1";
  origin: "model" | "application";
  request_id: string;
  instrument_id: string;
  trade_date: string | null;
  grounding_scope: string;
  paragraphs: string[];
  citations: { role: "answer" | "outlook" | "watch_for"; fact_ids: string[] }[];
  evidence_snapshots?: {
    role: "primary" | "supplement";
    request_id: string;
    instrument_id: string;
    trade_date: string;
    source: string;
    start_date: string;
    as_of_date: string;
  }[];
}

/** Citation binding is not a semantic proof of the model's prose. */
export function parseConversation(turn: PublicTurn): ConversationAnswer | null {
  const value = turn.conversation;
  if (!record(value) || value.version !== "conversation-answer-v1"
    || (value.origin !== "model" && value.origin !== "application")
    || !turn.parameters || !turn.report?.request_id || value.request_id !== turn.report.request_id
    || value.instrument_id !== turn.parameters.instrument_id
    || !Array.isArray(value.paragraphs) || !value.paragraphs.length || value.paragraphs.length > 3
    || !value.paragraphs.every(text) || !Array.isArray(value.citations)
    || (value.trade_date !== null && (!text(value.trade_date)
      || value.trade_date < turn.parameters.start_date || value.trade_date > turn.parameters.as_of_date))
    || turn.report.fields.some((field) => field.trade_date !== value.trade_date)) return null;
  if (value.origin === "model") {
    const snapshotIds = new Set([value.request_id]);
    if (Object.prototype.hasOwnProperty.call(value, "evidence_snapshots")) {
      const snapshots = value.evidence_snapshots;
      if (!Array.isArray(snapshots) || snapshots.length < 1 || snapshots.length > 2
        || snapshots.filter((item) => record(item) && item.role === "primary").length !== 1
        || !snapshots.every((item) => record(item)
          && (item.role === "primary" || item.role === "supplement")
          && typeof item.request_id === "string" && /^[A-Za-z0-9_.-]{1,100}$/.test(item.request_id)
          && (item.role === "primary" ? item.request_id === value.request_id : item.request_id !== value.request_id)
          && item.instrument_id === turn.parameters!.instrument_id && item.trade_date === value.trade_date
          && typeof item.source === "string" && item.source === turn.parameters!.source
          && ["tencent", "akshare_eastmoney"].includes(item.source)
          && item.start_date === turn.parameters!.start_date && item.as_of_date === turn.parameters!.as_of_date)
        || new Set(snapshots.map((item) => item.request_id)).size !== snapshots.length) return null;
      for (const snapshot of snapshots) snapshotIds.add(snapshot.request_id);
    }
    if (value.grounding_scope !== "request_and_relation_claims_checked_prose_semantics_not_verified"
      || !text(value.trade_date) || !["completed", "limited"].includes(turn.status) || turn.parameters.source === "csv"
      || value.paragraphs.length !== 3 || value.citations.length !== 3
      || !value.citations.every((item, index) => record(item)
        && item.role === ["answer", "outlook", "watch_for"][index]
        && Array.isArray(item.fact_ids) && item.fact_ids.length > 0 && item.fact_ids.length <= 8
        && item.fact_ids.every((id) => text(id)
          && [...snapshotIds].some((requestId) => id.startsWith(`${requestId}|${value.instrument_id}|${value.trade_date}|`)))
        && new Set(item.fact_ids).size === item.fact_ids.length)) return null;
  } else if (value.grounding_scope !== "application_data_readiness" || value.citations.length
    || Object.prototype.hasOwnProperty.call(value, "evidence_snapshots")) return null;
  return value as unknown as ConversationAnswer;
}

/** Optional presentation errors never invalidate the core response or trigger a retry. */
export function parsePresentation(turn: PublicTurn): AnswerPresentation | null {
  const value = turn.presentation;
  if (!record(value) || value.version !== "answer-presentation-v1"
    || (value.kind !== "research" && value.kind !== "knowledge" && value.kind !== "followup") || !text(value.headline)
    || !Array.isArray(value.sections) || value.sections.length > 12
    || !value.sections.every((section) => record(section) && text(section.id) && text(section.title)
      && Array.isArray(section.items) && section.items.length > 0 && section.items.length <= 100 && section.items.every(text))
    || new Set(value.sections.map((section) => section.id)).size !== value.sections.length) return null;
  if (value.kind === "research" || value.kind === "followup") {
    if (!turn.parameters || !turn.report?.request_id || value.request_id !== turn.report.request_id
      || value.instrument_id !== turn.parameters.instrument_id || !text(value.trade_date)
      || !turn.report.fields.length || turn.report.fields.some((field) => field.trade_date !== value.trade_date)
      || value.trade_date < turn.parameters.start_date || value.trade_date > turn.parameters.as_of_date) return null;
    if (value.kind === "followup" && (!Array.isArray(value.indicator_ids) || value.indicator_ids.length > 12
      || !value.indicator_ids.every((id) => text(id) && turn.report!.fields.some((field) => field.indicator_id === id))
      || new Set(value.indicator_ids).size !== value.indicator_ids.length)) return null;
  } else if (turn.parameters !== null || turn.report !== null
    || value.request_id !== null || value.instrument_id !== null || value.trade_date !== null
    || turn.status !== "completed") return null;
  return value as unknown as AnswerPresentation;
}

export const formatNumber = (value: number) => new Intl.NumberFormat("zh-CN", { maximumSignificantDigits: 7 }).format(value);
export const unitLabel: Record<string, string> = { "CNY/share": "元/股", shares: "股", multiple: "倍", index_points: "点" };
export const unavailableLabel: Record<string, string> = {
  insufficient_history: "历史不足", undefined: "未定义", calculation_error: "计算失败",
};
export const sourceLabel = (source: unknown) => source === "synthetic" || source === "csv" ? "人工行情 · 非真实"
  : source === "tencent" ? "腾讯历史日线" : source === "akshare_eastmoney" ? "AKShare / 东方财富历史日线" : "来源未提供";
