import { vi } from "vitest";
import type { Health, MessageResponse, SessionCredentials, SessionView } from "../src/api";
import type { ResearchChartData } from "../src/charts";

export const ID = "b".repeat(32);
export const health: Health = { status: "ok", provider: "fake", data_mode: "synthetic", trading_enabled: false };
export const credentials: SessionCredentials = { session_id: "a".repeat(32), access_token: "fake-capability-for-tests-only-123456",
  provider: "fake", data_mode: "synthetic", limits: { messages: 20, model_calls: 60 } };
export const idle: SessionView = { state: "idle", active_request_id: null, messages_used: 1, model_calls_forwarded: 3 };
export function reply(id = ID): MessageResponse {
  return { request_id: id, replayed: false, result: { status: "completed", code: "research_completed", stage: "explanation",
    answer: "人工样例 / 非真实行情。收盘价高于SMA(3)。", message: "研究完成。",
    parameters: { instrument_id: "000001.SZ", start_date: "2026-09-21", as_of_date: "2026-09-25" },
    candidates: [{ name: "平安银行", instrument_id: "000001.SZ" }], applied_defaults: [],
    model_calls_attempted: 3, analyses_used: 1, report: { limitations: [], fields: [
      { indicator_id: "sma:3", field: "value", label: "SMA(3)", trade_date: "2026-09-25", value: 10.166666,
        unit: "CNY/share", status: "available", missing_bars: 0, fact_id: "request:fact:sma" },
    ] } } };
}
export function chartReply(id = ID): MessageResponse {
  const value = reply(id);
  const chart: ResearchChartData = {
    version: "research-chart-v1", status: "available", code: "aligned_research_series",
    validation_scope: "request_bound_aligned_series_not_full_historical_evidence", request_id: "research-fixture",
    instrument_id: "000001.SZ", start_date: "2026-09-21", as_of_date: "2026-09-25", source: "csv",
    price_adjustment: "unknown", data_authenticity: "synthetic", retrieved_at: "2026-09-26T02:00:00Z",
    context: "current", saved_at: null,
    bars: [
      { date: "2026-09-21", open: 10, high: 10.3, low: 9.9, close: 10.2, volume: 100000 },
      { date: "2026-09-22", open: 10.2, high: 10.5, low: 10, close: 10.35, volume: 120000 },
      { date: "2026-09-25", open: 10.1, high: 10.5, low: 10, close: 10.4, volume: 130000 },
    ],
    series: [{ id: "sma(window=3):value", indicator_id: "sma(window=3)", kind: "sma", parameters: { window: 3 },
      algorithm_version: "sma-close-v1", field: "value", label: "SMA(3)", unit: "CNY/share", start_effect_warning: false,
      values: [null, null, 10.316667], statuses: ["insufficient_history", "insufficient_history", "available"] }],
  };
  value.result.chart = chart;
  value.result.parameters = { ...value.result.parameters!, source: "csv", indicators: [{ kind: "sma", window: 3 }] };
  value.result.report!.request_id = chart.request_id;
  value.result.report!.fields[0] = { ...value.result.report!.fields[0], indicator_id: "sma(window=3)", value: 10.316667 };
  return value;
}
export function marketContextReply(id = ID): MessageResponse {
  const value = chartReply(id);
  value.result.market_context = {
    version: "research-market-context-v1", status: "available", code: "reported_market_context",
    request_id: value.result.report!.request_id, ...value.result.parameters!,
    snapshot_id: "artificial-market-context-20260920-v1", retrieved_at: "2026-09-20T02:00:00+08:00",
    data_authenticity: "synthetic", validation_scope: "reported_business_date_context_not_point_in_time",
    historical_availability: "not_established",
    security: { status: "available", reason: "reported_state_at_analysis_date", effective_from: "2026-09-21", effective_to: "2026-09-27",
      published_at: "2026-09-20T01:00:00+08:00", source: "synthetic_fixture", reference: "artificial-security-not-market-facts",
      board: "szse_main", listing_date: "2026-09-01", st_status: "not_st", suspension_status: "not_full_day_suspended", listing_age_calendar_days: 24 },
    calendar: { exchange: "SZ", coverage_start: "2026-09-21", coverage_end: "2026-09-27", published_at: "2026-09-20T01:00:00+08:00",
      source: "synthetic_fixture", reference: "artificial-calendar-not-market-facts",
      open_dates: ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"],
      closed_dates: [], unknown_dates: [], missing_bar_dates: ["2026-09-23", "2026-09-24"], unexpected_bar_dates: [] },
    conflicts: [], warnings: ["not_point_in_time", "calendar_not_instrument_tradability", "no_trading_rules_evaluated",
      "synthetic_context_not_market_facts", "open_dates_without_bars_do_not_imply_suspension"],
  };
  return value;
}
export function mockApi() {
  return {
    health: vi.fn(async () => ({ ...health })), create: vi.fn(async () => ({ ...credentials })),
    inspect: vi.fn(async () => ({ ...idle })), send: vi.fn(async (_session: SessionCredentials, id: string, _question: string) => reply(id)),
    history: vi.fn(async () => { const response = reply(); response.result.model_calls_attempted = 0; return response; }),
    cancel: vi.fn(async (_session: SessionCredentials, _id: string) => {}),
    reset: vi.fn(async () => ({ ...idle })), end: vi.fn(async () => {}),
  };
}
export function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
