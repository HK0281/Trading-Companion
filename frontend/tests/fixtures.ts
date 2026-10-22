import { vi } from "vitest";
import type { Health, MessageResponse, SessionCredentials, SessionView } from "../src/api";

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
