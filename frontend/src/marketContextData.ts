/** Read-only reported-date context, never a point-in-time or execution guarantee. */
export interface MarketContextParameters { instrument_id: string; start_date: string; as_of_date: string }
export type ContextSource = "synthetic_fixture" | "sse" | "szse" | "bse" | "curated_local";
export type SecurityBoard = "sse_main" | "star" | "szse_main" | "chinext" | "bse" | "unknown";
export type SecurityReason = "reported_state_at_analysis_date" | "partial_state_unknown" | "state_not_covered"
  | "publication_time_unknown" | "published_after_analysis_cutoff";
export interface ContextSecurity {
  status: "available" | "limited" | "unknown"; reason: SecurityReason;
  effective_from: string | null; effective_to: string | null; published_at: string | null;
  source: ContextSource | null; reference: string | null; board: SecurityBoard; listing_date: string | null;
  st_status: "not_st" | "st" | "star_st" | "unknown";
  suspension_status: "full_day_suspended" | "not_full_day_suspended" | "unknown";
  listing_age_calendar_days: number | null;
}
export interface ContextCalendar {
  exchange: "SH" | "SZ" | "BJ"; coverage_start: string | null; coverage_end: string | null;
  published_at: string | null; source: ContextSource | null; reference: string | null;
  open_dates: string[]; closed_dates: string[]; unknown_dates: string[]; missing_bar_dates: string[]; unexpected_bar_dates: string[];
}
export interface MarketContextData {
  version: "research-market-context-v1"; status: "available" | "limited";
  code: "reported_market_context" | "market_context_limited";
  request_id: string; instrument_id: string; start_date: string; as_of_date: string;
  snapshot_id: string; retrieved_at: string;
  data_authenticity: "synthetic" | "unverified_local_snapshot";
  validation_scope: "reported_business_date_context_not_point_in_time"; historical_availability: "not_established";
  security: ContextSecurity; calendar: ContextCalendar; conflicts: string[]; warnings: string[];
}

const object = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const safeId = (value: unknown): value is string => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$/.test(value);
const requestId = (value: unknown): value is string => typeof value === "string" && /^[A-Za-z0-9_.-]{1,100}$/.test(value);
const member = <T extends string>(value: unknown, allowed: readonly T[]): value is T => typeof value === "string" && allowed.includes(value as T);
const sources = ["synthetic_fixture", "sse", "szse", "bse", "curated_local"] as const;
const reasons = ["reported_state_at_analysis_date", "partial_state_unknown", "state_not_covered", "publication_time_unknown", "published_after_analysis_cutoff"] as const;

export function validContextDate(value: unknown): value is string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith("0000")) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(date.valueOf()) && date.toISOString().slice(0, 10) === value;
}
const dateOrNull = (value: unknown): value is string | null => value === null || validContextDate(value);
export function validContextTimestamp(value: unknown): value is string {
  if (typeof value !== "string" || value.length > 40) return false;
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  return !!match && validContextDate(match[1]) && Number(match[2]) < 24 && Number(match[3]) < 60 && Number(match[4]) < 60
    && (match[5] === "Z" || Number(match[5].slice(1, 3)) < 24 && Number(match[5].slice(4)) < 60)
    && Number.isFinite(Date.parse(value));
}
const timestampOrNull = (value: unknown): value is string | null => value === null || validContextTimestamp(value);

/** Enumerate calendar dates in UTC, not local time or an inferred exchange schedule. */
export function queryContextDates(start: string, end: string): string[] {
  if (!validContextDate(start) || !validContextDate(end) || start > end || Date.parse(end) - Date.parse(start) > 365 * 86400000) return [];
  const result: string[] = [];
  for (let stamp = Date.parse(start); stamp <= Date.parse(end); stamp += 86400000) result.push(new Date(stamp).toISOString().slice(0, 10));
  return result;
}

function metadata(value: Record<string, unknown>): boolean {
  return timestampOrNull(value.published_at) && (value.source === null || member(value.source, sources))
    && (value.reference === null || safeId(value.reference)) && ((value.source === null) === (value.reference === null));
}
function security(value: unknown, asOf: string, exchange: string): value is ContextSecurity {
  if (!object(value) || !member(value.status, ["available", "limited", "unknown"])
    || !member(value.reason, reasons) || !metadata(value) || !dateOrNull(value.effective_from) || !dateOrNull(value.effective_to)
    || (value.effective_from === null) !== (value.effective_to === null)
    || value.effective_from !== null && (value.effective_from > asOf || value.effective_to !== null && value.effective_to < asOf)
    || !member(value.board, ["sse_main", "star", "szse_main", "chinext", "bse", "unknown"])
    || !member(value.st_status, ["not_st", "st", "star_st", "unknown"])
    || !member(value.suspension_status, ["full_day_suspended", "not_full_day_suspended", "unknown"])
    || !dateOrNull(value.listing_date)
    || value.listing_age_calendar_days !== null && (typeof value.listing_age_calendar_days !== "number"
      || !Number.isSafeInteger(value.listing_age_calendar_days) || value.listing_age_calendar_days < 0 || value.listing_age_calendar_days > 3660000)) return false;
  if (value.board !== "unknown" && (["sse_main", "star"].includes(value.board) ? exchange !== "SH"
    : ["szse_main", "chinext"].includes(value.board) ? exchange !== "SZ" : exchange !== "BJ")) return false;
  const expectedAge = value.listing_date === null || value.listing_date > asOf ? null : (Date.parse(asOf) - Date.parse(value.listing_date)) / 86400000;
  if (value.listing_age_calendar_days !== expectedAge) return false;
  const allUnknown = value.board === "unknown" && value.st_status === "unknown" && value.suspension_status === "unknown" && value.listing_date === null;
  if (value.reason === "state_not_covered") return value.status === "unknown" && allUnknown
    && value.effective_from === null && value.effective_to === null && value.published_at === null && value.source === null && value.reference === null;
  if (value.effective_from === null || value.source === null || value.reference === null) return false;
  if (value.reason === "publication_time_unknown") return value.status === "unknown" && allUnknown && value.published_at === null;
  const cutoff = Date.parse(`${asOf}T23:59:59.999+08:00`);
  if (value.reason === "published_after_analysis_cutoff") return value.status === "unknown" && allUnknown
    && typeof value.published_at === "string" && Date.parse(value.published_at) > cutoff;
  if (typeof value.published_at !== "string" || Date.parse(value.published_at) > cutoff) return false;
  const complete = value.board !== "unknown" && value.st_status !== "unknown" && value.suspension_status !== "unknown" && value.listing_date !== null;
  return value.reason === "reported_state_at_analysis_date" ? value.status === "available" && complete
    : value.status === "limited" && !complete;
}

function dateList(value: unknown, start: string, end: string): value is string[] {
  return Array.isArray(value) && value.length <= 366 && value.every((item, index) => validContextDate(item)
    && item >= start && item <= end && (index === 0 || value[index - 1] < item));
}
function calendar(value: unknown, dates: string[], exchange: string): value is ContextCalendar {
  if (!object(value) || value.exchange !== exchange || !metadata(value) || !dateOrNull(value.coverage_start) || !dateOrNull(value.coverage_end)
    || (value.coverage_start === null) !== (value.coverage_end === null)
    || value.coverage_start !== null && value.coverage_end !== null && value.coverage_start > value.coverage_end) return false;
  const start = dates[0], end = dates.at(-1)!;
  if (![value.open_dates, value.closed_dates, value.unknown_dates, value.missing_bar_dates, value.unexpected_bar_dates].every((item) => dateList(item, start, end))) return false;
  const open = value.open_dates as string[], closed = value.closed_dates as string[], unknown = value.unknown_dates as string[];
  const combined = [...open, ...closed, ...unknown];
  if (combined.length !== dates.length || new Set(combined).size !== dates.length || dates.some((date) => !combined.includes(date))) return false;
  if (!(value.missing_bar_dates as string[]).every((date) => open.includes(date)) || !(value.unexpected_bar_dates as string[]).every((date) => closed.includes(date))) return false;
  if (value.coverage_start === null) return value.published_at === null && value.source === null && value.reference === null && unknown.length === dates.length;
  if (value.source === null || value.reference === null || value.coverage_end === null) return false;
  if (Date.parse(value.coverage_end) - Date.parse(value.coverage_start) > 3659 * 86400000) return false;
  if ([...open, ...closed].some((date) => date < (value.coverage_start as string) || date > (value.coverage_end as string))) return false;
  if (value.published_at === null && unknown.length !== dates.length) return false;
  if (typeof value.published_at === "string" && Date.parse(value.published_at) > Date.parse(`${end}T23:59:59.999+08:00`) && unknown.length !== dates.length) return false;
  return true;
}
const notices = (value: unknown): value is string[] => Array.isArray(value) && value.length <= 48 && value.every(safeId) && new Set(value).size === value.length;

/** Auxiliary context must fail independently from the already confirmed research answer. */
export function parseMarketContext(value: unknown, parameters: MarketContextParameters | null, reportRequestId: string | undefined): MarketContextData | null {
  if (!object(value) || !parameters || !requestId(reportRequestId) || value.request_id !== reportRequestId
    || value.version !== "research-market-context-v1" || !member(value.status, ["available", "limited"])
    || value.code !== (value.status === "available" ? "reported_market_context" : "market_context_limited")
    || typeof value.instrument_id !== "string" || !/^\d{6}\.(SH|SZ|BJ)$/.test(value.instrument_id)
    || value.instrument_id !== parameters.instrument_id || value.start_date !== parameters.start_date || value.as_of_date !== parameters.as_of_date
    || !validContextDate(value.start_date) || !validContextDate(value.as_of_date)
    || !safeId(value.snapshot_id) || !validContextTimestamp(value.retrieved_at)
    || !member(value.data_authenticity, ["synthetic", "unverified_local_snapshot"])
    || value.validation_scope !== "reported_business_date_context_not_point_in_time" || value.historical_availability !== "not_established"
    || !notices(value.conflicts) || !notices(value.warnings)) return null;
  const dates = queryContextDates(value.start_date, value.as_of_date);
  const exchange = value.instrument_id.slice(-2);
  if (!dates.length || !security(value.security, value.as_of_date, exchange) || !calendar(value.calendar, dates, exchange)) return null;
  const source = { SH: "sse", SZ: "szse", BJ: "bse" }[exchange];
  for (const item of [value.security, value.calendar]) {
    if (item.published_at !== null && Date.parse(item.published_at) > Date.parse(value.retrieved_at)) return null;
    if (item.source !== null && (value.data_authenticity === "synthetic" ? item.source !== "synthetic_fixture"
      : item.source !== source && item.source !== "curated_local")) return null;
  }
  const limited = value.security.status !== "available" || value.calendar.unknown_dates.length > 0 || value.conflicts.length > 0;
  if (value.status !== (limited ? "limited" : "available")) return null;
  return value as unknown as MarketContextData;
}

/** Unknown bounded codes receive a fixed warning, never an invented business interpretation. */
export function marketContextNotice(code: string, category: "warning" | "conflict" = "warning"): string {
  const messages: Record<string, string> = {
    not_point_in_time: "这是业务日期上下文；尚未证明这些信息在历史分析时点可获得，不属于 point-in-time 数据。",
    calendar_not_instrument_tradability: "交易所开市不等于该股票当日可交易。",
    no_trading_rules_evaluated: "未评估涨跌停、委托资格或其他交易规则，不保证可交易。",
    synthetic_context_not_market_facts: "人工样例只用于验证程序流程，不代表真实市场事实。",
    retrieved_after_analysis_cutoff: "快照采集晚于分析日截止；采集时间不能证明历史时点已知。",
    security_context_unknown_or_partial: "证券状态未覆盖或仅部分已知；未知项不能当作正常状态。",
    calendar_context_unknown_or_partial: "部分日期的交易所日历未知；未按周末或惯例补全。",
    calendar_not_configured_for_exchange: "此快照没有配置对应交易所的日历。",
    calendar_publication_time_unknown: "日历的发布时间未知；未据此判定开市或休市。",
    calendar_published_after_analysis_cutoff: "日历在分析日截止后才发布；历史查询中的日期状态保持未知。",
    open_dates_without_bars_do_not_imply_suspension: "报告为开市但缺少日线的日期，不等于股票停牌；也可能是数据缺失。",
    analysis_precedes_reported_listing_date: "分析日早于所报上市日；尚不能计算已上市自然日数。",
    bar_on_reported_closed_date: "日线出现在快照报告的休市日期；仅提示来源冲突，未删除或修改行情。",
    bar_before_reported_listing_date: "日线日期早于所报上市日；仅提示来源冲突，未删除或修改行情。",
    bar_during_reported_full_day_suspension: "日线与所报全天停牌状态冲突；仅提示来源冲突，未删除或修改行情。",
  };
  const conflict = ["bar_on_reported_closed_date", "bar_before_reported_listing_date", "bar_during_reported_full_day_suspension"];
  return Object.hasOwn(messages, code) && conflict.includes(code) === (category === "conflict")
    ? messages[code] : "快照包含未识别的限制项；未将其解释为业务事实，请核对来源。";
}
