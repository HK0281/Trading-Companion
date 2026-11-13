/** Read-only projections of saved research, separate from a current research chart. */
import type { PublicTurn } from "./api";
import { validChartDate } from "./charts";

export const reviewVersion = "research-review-v1";
export const legacyReviewNotice = "旧记录未保存完成时间，起点取所在对话最近保存日期之后，可能遗漏较早走势；继续该对话会使起点后移。";
export interface ReviewIdentity { conversation_id: string; sequence: number; request_id: string; instrument_id: string }
export interface ReviewFilters { instrument_id: string; from_date?: string; to_date?: string; cursor?: string }
export interface ReviewPlan {
  version: "trade-plan-v1" | "trade-plan-v2"; origin: "application"; execution_allowed: false;
  plan_id: string; instrument_id: string; status: "available"; code: string; purpose: "conditional_entry" | "holding_exit" | "wait";
  source_date: string; source: string | null; price_adjustment: string | null; data_authenticity: string | null; method: string;
  values: Record<string, string>; horizon?: string | null; horizon_profile?: string | null;
}
export interface ReviewPriceEligibility {
  enabled: boolean; code: string; message: string; start_date: string | null; end_date: string | null;
  basis: "completed_at" | "conversation_updated_at" | null; basis_at: string | null; notice: string | null;
  source?: string | null; price_adjustment?: string | null; data_authenticity?: string | null;
  source_date?: string | null; technical_request_id?: string | null;
}
export interface ResearchReviewItem extends ReviewIdentity {
  name: string; question: string; asked_at: string; daily_cutoff: string | null; completed_at: string | null;
  historical_type: "current" | "historical" | "unknown"; status: "available" | "unavailable" | "unrecorded";
  code?: string | null; rating: string | null; direction: string | null; conviction: string | null; horizon: string | null;
  trade_plan: ReviewPlan | null; prices: ReviewPriceEligibility;
}
export interface ResearchReviews {
  version: typeof reviewVersion; enabled: boolean; items: ResearchReviewItem[]; scanned: number;
  skipped_invalid: number; has_more: boolean; next_cursor: string | null;
}
export interface ResearchReviewDetail { version: typeof reviewVersion; item: ResearchReviewItem; question: string; result: PublicTurn }
export interface ResearchReviewPrices {
  version: typeof reviewVersion; status: "available" | "unavailable"; item: ResearchReviewItem;
  source: "tencent"; price_adjustment: "unadjusted"; data_authenticity: "provider_reported";
  retrieved_at: string | null; points: { date: string; close: number }[]; code?: string; message?: string;
}
const obj = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === "object" && !Array.isArray(v);
const txt = (v: unknown, max = 200): v is string => typeof v === "string" && v.length <= max && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(v);
const id = (v: unknown): v is string => typeof v === "string" && /^[a-f0-9]{32}$/.test(v);
const instrument = (v: unknown): v is string => typeof v === "string" && /^\d{6}\.(SH|SZ|BJ)$/.test(v);
const count = (v: unknown): v is number => typeof v === "number" && Number.isInteger(v) && v >= 0;
const nullable = (v: unknown, max = 200) => v === null || txt(v, max);
function time(v: unknown): v is string {
  if (typeof v !== "string") return false;
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/.exec(v);
  return !!match && validChartDate(match[1]) && Number(match[2]) < 24 && Number(match[3]) < 60 && Number(match[4]) < 60
    && (match[5] === "Z" || Number(match[5].slice(1, 3)) < 24 && Number(match[5].slice(4)) < 60) && Number.isFinite(Date.parse(v));
}
const shanghaiDate = (stamp: string, days = 0) => new Date(Date.parse(stamp) + (8 + days * 24) * 3600000).toISOString().slice(0, 10);
const date = (v: unknown): v is string => typeof v === "string" && validChartDate(v);
const nullableDate = (v: unknown) => v === null || date(v);
const nullableTime = (v: unknown) => v === null || time(v);
const keys = (v: Record<string, unknown>, expected: string[]) => Object.keys(v).sort().join() === [...expected].sort().join();
export const sameReview = (a: ReviewIdentity, b: ReviewIdentity) => a.conversation_id === b.conversation_id
  && a.sequence === b.sequence && a.request_id === b.request_id && a.instrument_id === b.instrument_id;
export const reviewKey = (v: ReviewIdentity) => `${v.conversation_id}:${v.sequence}:${v.request_id}:${v.instrument_id}`;
export function validReviewIdentity(v: unknown): v is ReviewIdentity {
  return obj(v) && id(v.conversation_id) && count(v.sequence) && v.sequence > 0 && v.sequence <= 500 && id(v.request_id) && instrument(v.instrument_id);
}
export function validReviewFilters(v: ReviewFilters): boolean {
  return instrument(v.instrument_id) && (v.from_date === undefined || date(v.from_date)) && (v.to_date === undefined || date(v.to_date))
    && (!v.from_date || !v.to_date || v.from_date <= v.to_date)
    && (v.cursor === undefined || txt(v.cursor, 4096) && v.cursor.length > 0);
}
const planFields: Record<ReviewPlan["purpose"], string[]> = {
  conditional_entry: ["reference_entry", "reference_stop", "target_1", "target_2", "reward_risk_1", "reward_risk_2", "atr", "atr_multiple"],
  holding_exit: ["reference_close", "exit_below"], wait: ["observe_below", "observe_above"],
};
function plan(v: unknown, instrumentId: string): v is ReviewPlan {
  if (!obj(v) || v.instrument_id !== instrumentId || !txt(v.plan_id) || !v.plan_id || v.status !== "available"
    || !txt(v.code) || v.origin !== "application" || v.execution_allowed !== false
    || !(v.version === "trade-plan-v1" && v.method === "atr_reference_plan_v1" || v.version === "trade-plan-v2" && v.method === "atr_reference_plan_v2")
    || !date(v.source_date) || !nullable(v.source) || !nullable(v.price_adjustment) || !nullable(v.data_authenticity)
    || typeof v.purpose !== "string" || !Object.hasOwn(planFields, v.purpose) || !obj(v.values)) return false;
  const expected = planFields[v.purpose as ReviewPlan["purpose"]];
  if (!keys(v.values, expected) || !Object.values(v.values).every(n => typeof n === "string" && n.length >= 1 && n.length <= 80
    && /^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(n) && Number.isFinite(Number(n)) && Number(n) > 0 && Number(n) <= 1e15)) return false;
  const n = (key: string) => Number((v.values as Record<string, string>)[key]);
  return v.purpose !== "conditional_entry" || n("reference_stop") < n("reference_entry") && n("reference_entry") < n("target_1") && n("target_1") < n("target_2");
}
function eligibility(v: unknown): v is ReviewPriceEligibility {
  if (!obj(v) || typeof v.enabled !== "boolean" || !txt(v.code) || !txt(v.message, 600)
    || !nullableDate(v.start_date) || !nullableDate(v.end_date) || ![null, "completed_at", "conversation_updated_at"].includes(v.basis as string | null)
    || !nullableTime(v.basis_at) || !nullable(v.notice, 600)) return false;
  if (v.basis !== null && !time(v.basis_at) || v.basis === "conversation_updated_at" && v.notice !== legacyReviewNotice) return false;
  if (v.enabled && (!date(v.start_date) || !date(v.end_date) || v.start_date > v.end_date || v.basis === null
    || !time(v.basis_at) || v.start_date !== shanghaiDate(v.basis_at, 1)
    || Date.parse(v.end_date) - Date.parse(v.start_date) > 365 * 86400000)) return false;
  return ["source", "price_adjustment", "data_authenticity", "technical_request_id"].every(k => v[k] === undefined || nullable(v[k]))
    && (v.source_date === undefined || nullableDate(v.source_date));
}
export function parseResearchReviewItem(v: unknown): ResearchReviewItem | null {
  if (!obj(v) || !validReviewIdentity(v) || !txt(v.name, 80) || !v.name || !txt(v.question, 2000) || !time(v.asked_at)
    || !nullableDate(v.daily_cutoff) || !nullableTime(v.completed_at)
    || !["current", "historical", "unknown"].includes(String(v.historical_type))
    || !["available", "unavailable", "unrecorded"].includes(String(v.status)) || v.code !== undefined && !nullable(v.code)
    || ![v.rating, v.direction, v.conviction, v.horizon].every(x => nullable(x)) || !eligibility(v.prices)
    || v.trade_plan !== null && !plan(v.trade_plan, v.instrument_id)) return null;
  if (v.prices.basis === "completed_at" && (v.completed_at === null || v.completed_at !== v.prices.basis_at)
    || v.prices.basis === "conversation_updated_at" && v.completed_at !== null) return null;
  return v as unknown as ResearchReviewItem;
}
export function parseResearchReviews(v: unknown, instrumentId?: string): ResearchReviews | null {
  if (!obj(v) || !keys(v, ["version", "enabled", "items", "scanned", "skipped_invalid", "has_more", "next_cursor"])
    || v.version !== reviewVersion || typeof v.enabled !== "boolean" || !count(v.scanned) || v.scanned > 20
    || !count(v.skipped_invalid) || v.skipped_invalid > v.scanned || typeof v.has_more !== "boolean"
    || !(v.next_cursor === null || txt(v.next_cursor, 4096) && v.next_cursor.length > 0) || v.has_more !== (v.next_cursor !== null)
    || !Array.isArray(v.items) || v.items.length > v.scanned || !v.items.every(item => parseResearchReviewItem(item)
      && (!instrumentId || item.instrument_id === instrumentId)) || new Set(v.items.map(reviewKey)).size !== v.items.length
    || !v.enabled && (v.items.length > 0 || v.scanned !== 0 || v.skipped_invalid !== 0 || v.has_more)) return null;
  return v as unknown as ResearchReviews;
}
export function parseResearchReviewDetail(v: unknown, identity: ReviewIdentity): ResearchReviewDetail | null {
  if (!obj(v) || !keys(v, ["version", "item", "question", "result"]) || v.version !== reviewVersion
    || !parseResearchReviewItem(v.item) || !sameReview(v.item as ResearchReviewItem, identity) || !txt(v.question, 2000)
    || !obj(v.result) || !txt(v.result.answer, 1048576) && v.result.answer !== null
    || ![v.result.status, v.result.code, v.result.stage, v.result.message].every(x => txt(x, 1048576))) return null;
  return v as unknown as ResearchReviewDetail;
}
export function parseResearchReviewPrices(v: unknown, identity: ReviewIdentity): ResearchReviewPrices | null {
  if (!obj(v) || v.version !== reviewVersion || !["available", "unavailable"].includes(String(v.status))
    || !parseResearchReviewItem(v.item) || !sameReview(v.item as ResearchReviewItem, identity)
    || v.source !== "tencent" || v.price_adjustment !== "unadjusted" || v.data_authenticity !== "provider_reported"
    || !nullableTime(v.retrieved_at) || !Array.isArray(v.points) || v.points.length > 366
    || v.code !== undefined && !txt(v.code) || v.message !== undefined && !txt(v.message, 600)) return null;
  const item = v.item as ResearchReviewItem;
  if (v.status === "unavailable") return v.points.length === 0 ? v as unknown as ResearchReviewPrices : null;
  if (!item.prices.enabled || !time(v.retrieved_at) || v.points.length === 0
    || item.prices.end_date! >= shanghaiDate(v.retrieved_at)
    || item.trade_plan && (item.trade_plan.price_adjustment !== "unadjusted" || item.trade_plan.data_authenticity !== "provider_reported")) return null;
  let previous = "";
  for (const point of v.points) {
    if (!obj(point) || !keys(point, ["date", "close"]) || !date(point.date) || point.date <= previous
      || point.date < item.prices.start_date! || point.date > item.prices.end_date! || typeof point.close !== "number"
      || !Number.isFinite(point.close) || point.close <= 0) return null;
    previous = point.date;
  }
  return v as unknown as ResearchReviewPrices;
}
export const reviewTime = (value: string) => new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
export const reviewType = (item: ResearchReviewItem) => ({ current: "当时研究", historical: "回溯研究", unknown: "研究时点类型未记录" })[item.historical_type];
export const reviewPriceLabels: Record<string, string> = { reference_entry: "原参考入场", reference_stop: "原参考止损", target_1: "原参考目标一", target_2: "原参考目标二",
  reference_close: "原参考收盘", exit_below: "原退出观察", observe_below: "原下沿观察", observe_above: "原上沿观察", atr: "原波动值 ATR", atr_multiple: "原波动倍数", reward_risk_1: "原盈亏比一", reward_risk_2: "原盈亏比二" };
