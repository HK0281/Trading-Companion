import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MarketContext } from "../src/MarketContext";
import { marketContextNotice, parseMarketContext, queryContextDates, validContextDate, validContextTimestamp,
  type MarketContextData, type MarketContextParameters } from "../src/marketContextData";

const request = "market-context-request-1";
const parameters: MarketContextParameters = { instrument_id: "000001.SZ", start_date: "2026-09-21", as_of_date: "2026-09-25" };
function fixture(): MarketContextData {
  return { version: "research-market-context-v1", status: "available", code: "reported_market_context", request_id: request,
    instrument_id: parameters.instrument_id, start_date: parameters.start_date, as_of_date: parameters.as_of_date,
    snapshot_id: "fixture:2026-10-22", retrieved_at: "2026-10-22T12:30:00+08:00", data_authenticity: "synthetic",
    validation_scope: "reported_business_date_context_not_point_in_time", historical_availability: "not_established",
    security: { status: "available", reason: "reported_state_at_analysis_date", effective_from: "2026-01-01", effective_to: "2026-12-31",
      published_at: "2026-09-20T12:30:00+08:00", source: "synthetic_fixture", reference: "security:000001.SZ",
      board: "szse_main", listing_date: "2026-09-01", st_status: "not_st", suspension_status: "not_full_day_suspended", listing_age_calendar_days: 24 },
    calendar: { exchange: "SZ", coverage_start: "2026-09-21", coverage_end: "2026-09-25", published_at: "2026-09-20T12:30:00+08:00",
      source: "synthetic_fixture", reference: "calendar:SZ", open_dates: ["2026-09-21", "2026-09-22", "2026-09-24", "2026-09-25"],
      closed_dates: ["2026-09-23"], unknown_dates: [], missing_bar_dates: ["2026-09-24"], unexpected_bar_dates: [] },
    conflicts: [], warnings: ["not_point_in_time", "calendar_not_instrument_tradability", "no_trading_rules_evaluated", "synthetic_context_not_market_facts",
      "retrieved_after_analysis_cutoff", "open_dates_without_bars_do_not_imply_suspension"],
  };
}
function markLimited(data: MarketContextData) { data.status = "limited"; data.code = "market_context_limited"; }
function hideSecurity(data: MarketContextData) {
  data.security.status = "unknown"; data.security.board = "unknown"; data.security.st_status = "unknown";
  data.security.suspension_status = "unknown"; data.security.listing_date = null; data.security.listing_age_calendar_days = null; markLimited(data);
}
function unknownCalendar(data: MarketContextData) {
  data.calendar.open_dates = []; data.calendar.closed_dates = []; data.calendar.unknown_dates = queryContextDates(data.start_date, data.as_of_date);
  data.calendar.missing_bar_dates = []; data.calendar.unexpected_bar_dates = []; markLimited(data);
}

describe("market context binding and envelope", () => {
  it("accepts a complete request-bound reported-date context", () => {
    expect(parseMarketContext(fixture(), parameters, request)).toEqual(fixture());
  });
  it.each([null, undefined, 5, "context", [], {}, { version: "research-market-context-v1", status: "unavailable", code: "context_not_configured" }])("safely rejects absent or unavailable input %j", (value) => {
    expect(parseMarketContext(value, parameters, request)).toBeNull();
  });
  it.each([
    ["version", "v2"], ["status", "complete"], ["code", "all_context_verified"], ["request_id", "other-request"],
    ["instrument_id", "600000.SH"], ["start_date", "2026-09-20"], ["as_of_date", "2026-09-26"],
    ["snapshot_id", "private/path.json"], ["snapshot_id", "x".repeat(101)], ["snapshot_id", ":leading-punctuation"],
    ["retrieved_at", null], ["retrieved_at", "2026-10-22T12:30:00"], ["retrieved_at", "2026-02-30T12:30:00Z"],
    ["data_authenticity", "verified"], ["validation_scope", "point_in_time_verified"], ["historical_availability", "established"],
    ["conflicts", ["untrusted <img>"]], ["warnings", ["not_point_in_time", "not_point_in_time"]],
    ["warnings", Array.from({ length: 49 }, (_, index) => `warning_${index}`)],
  ])("rejects an invalid envelope field %s=%j", (key, value) => {
    expect(parseMarketContext({ ...fixture(), [key]: value }, parameters, request)).toBeNull();
  });
  it("does not render a context without the confirmed report identity and scope", () => {
    expect(parseMarketContext(fixture(), parameters, undefined)).toBeNull();
    expect(parseMarketContext(fixture(), null, request)).toBeNull();
  });
  it.each(["status", "code", "instrument_id", "snapshot_id", "retrieved_at", "data_authenticity", "validation_scope", "historical_availability", "security", "calendar", "warnings", "conflicts"]) ("rejects a JSON object in %s without coercion or throwing", (key) => {
    const data = { ...fixture(), [key]: { toString: 0 } };
    expect(() => parseMarketContext(data, parameters, request)).not.toThrow();
    expect(parseMarketContext(data, parameters, request)).toBeNull();
  });
  it("rejects contradictory available/limited envelopes", () => {
    const data = fixture(); markLimited(data); expect(parseMarketContext(data, parameters, request)).toBeNull();
    data.conflicts = ["bar_on_reported_closed_date"]; expect(parseMarketContext(data, parameters, request)).not.toBeNull();
    data.status = "available"; data.code = "reported_market_context"; expect(parseMarketContext(data, parameters, request)).toBeNull();
  });
  it("accepts unverified local provenance only for the matching exchange or a curated source", () => {
    const data = fixture(); data.data_authenticity = "unverified_local_snapshot"; data.security.source = "szse"; data.calendar.source = "curated_local";
    expect(parseMarketContext(data, parameters, request)).not.toBeNull();
    data.security.source = "sse"; expect(parseMarketContext(data, parameters, request)).toBeNull();
    data.security.source = "synthetic_fixture"; expect(parseMarketContext(data, parameters, request)).toBeNull();
  });
});

describe("market context dates and publication provenance", () => {
  it.each(["2026-02-30", "2025-02-29", "2026-13-01", "2026-00-10", "0000-01-01", "2026-9-01", "2026-09-01extra"]) ("does not silently repair an impossible date %s", (date) => {
    expect(validContextDate(date)).toBe(false);
  });
  it("enumerates valid calendar days, including leap dates, without inferring open dates", () => {
    expect(queryContextDates("2024-02-28", "2024-03-01")).toEqual(["2024-02-28", "2024-02-29", "2024-03-01"]);
    expect(queryContextDates("2026-09-25", "2026-09-21")).toEqual([]);
    expect(queryContextDates("2024-01-01", "2025-01-01")).toEqual([]);
    expect(queryContextDates("2024-01-01", "2024-12-31")).toHaveLength(366);
  });
  it.each(["2026-09-25T25:00:00Z", "2026-09-25T12:60:00Z", "2026-09-25T12:30:60Z", "2026-09-25T12:30:00+24:00", "2026-09-25T12:30:00+08:60", "2026-09-25T12:30:00"]) ("requires a valid timezone-aware timestamp %s", (value) => {
    expect(validContextTimestamp(value)).toBe(false);
  });
  it("accepts timezone-aware microsecond timestamps", () => {
    expect(validContextTimestamp("2026-09-25T12:30:00.123456+08:00")).toBe(true);
  });
  it("rejects publication after collection", () => {
    const data = fixture(); data.security.published_at = "2026-11-01T00:00:00Z";
    expect(parseMarketContext(data, parameters, request)).toBeNull();
  });
  it("hides late-published states even if their business interval covers the analysis date", () => {
    const data = fixture(); hideSecurity(data); data.security.reason = "published_after_analysis_cutoff";
    data.security.published_at = "2026-09-26T00:00:00+08:00";
    expect(parseMarketContext(data, parameters, request)).not.toBeNull();
    data.security.st_status = "not_st"; expect(parseMarketContext(data, parameters, request)).toBeNull();
  });
  it("uses the Shanghai analysis cutoff, rather than merely comparing timestamp date strings", () => {
    const data = fixture(); data.security.published_at = "2026-09-25T16:00:00Z";
    expect(parseMarketContext(data, parameters, request)).toBeNull();
    data.security.published_at = "2026-09-25T15:59:59Z";
    expect(parseMarketContext(data, parameters, request)).not.toBeNull();
  });
});

describe("security context cannot replace unknowns with normal states", () => {
  it.each([
    ["status", "normal"], ["reason", "trusted"], ["board", "main"], ["st_status", "normal"], ["suspension_status", "tradable"],
    ["source", "custom_provider"], ["source", { toString: 0 }], ["reference", "https://example.com/private"], ["reference", "x".repeat(101)],
    ["effective_from", "2026-09-26"], ["effective_to", "2026-09-24"], ["effective_to", null], ["effective_from", null],
    ["published_at", "2026-09-20T10:00:00"], ["listing_date", "2026-02-30"], ["listing_age_calendar_days", 25],
    ["listing_age_calendar_days", -1], ["listing_age_calendar_days", .5], ["listing_age_calendar_days", Infinity],
  ])("rejects invalid or contradictory security field %s=%j", (key, value) => {
    const data = fixture(); Object.assign(data.security, { [key]: value });
    expect(() => parseMarketContext(data, parameters, request)).not.toThrow();
    expect(parseMarketContext(data, parameters, request)).toBeNull();
  });
  it("accepts partial reported fields without promoting the unknown ST status", () => {
    const data = fixture(); data.security.status = "limited"; data.security.reason = "partial_state_unknown"; data.security.st_status = "unknown"; markLimited(data);
    expect(parseMarketContext(data, parameters, request)).not.toBeNull();
    data.security.status = "available"; data.security.reason = "reported_state_at_analysis_date";
    expect(parseMarketContext(data, parameters, request)).toBeNull();
  });
  it("accepts missing security coverage only with hidden values and null provenance", () => {
    const data = fixture(); hideSecurity(data); data.security.reason = "state_not_covered"; data.security.effective_from = null; data.security.effective_to = null;
    data.security.source = null; data.security.reference = null; data.security.published_at = null;
    expect(parseMarketContext(data, parameters, request)).not.toBeNull();
    data.security.listing_date = "2026-09-01"; data.security.listing_age_calendar_days = 24;
    expect(parseMarketContext(data, parameters, request)).toBeNull();
  });
  it("allows safe provenance with an unknown publication time but hides every business field", () => {
    const data = fixture(); hideSecurity(data); data.security.reason = "publication_time_unknown"; data.security.published_at = null;
    expect(parseMarketContext(data, parameters, request)).not.toBeNull();
    data.security.board = "szse_main"; expect(parseMarketContext(data, parameters, request)).toBeNull();
  });
  it("rejects a board from another exchange", () => {
    const data = fixture(); data.security.board = "star";
    expect(parseMarketContext(data, parameters, request)).toBeNull();
  });
  it("counts listing calendar days from zero and preserves future listing dates with null age", () => {
    const data = fixture(); data.security.listing_date = data.as_of_date; data.security.listing_age_calendar_days = 0;
    expect(parseMarketContext(data, parameters, request)).not.toBeNull();
    data.security.listing_date = "2026-09-26"; data.security.listing_age_calendar_days = null;
    expect(parseMarketContext(data, parameters, request)).not.toBeNull();
  });
});

describe("calendar coverage and conflict classification", () => {
  it.each([
    (data: MarketContextData) => { data.calendar.open_dates.push("2026-09-25"); },
    (data: MarketContextData) => { data.calendar.closed_dates = ["2026-09-22"]; },
    (data: MarketContextData) => { data.calendar.closed_dates = []; },
    (data: MarketContextData) => { data.calendar.unknown_dates = ["2026-09-26"]; },
    (data: MarketContextData) => { data.calendar.open_dates.reverse(); },
    (data: MarketContextData) => { data.calendar.missing_bar_dates = ["2026-09-23"]; },
    (data: MarketContextData) => { data.calendar.unexpected_bar_dates = ["2026-09-24"]; },
    (data: MarketContextData) => { data.calendar.exchange = "SH"; },
    (data: MarketContextData) => { data.calendar.coverage_start = "2026-09-22"; },
    (data: MarketContextData) => { data.calendar.coverage_end = "2026-09-20"; },
    (data: MarketContextData) => { data.calendar.coverage_end = null; },
    (data: MarketContextData) => { data.calendar.reference = null; },
    (data: MarketContextData) => { data.calendar.published_at = null; },
    (data: MarketContextData) => { data.calendar.published_at = "2026-09-26T00:00:00+08:00"; },
    (data: MarketContextData) => { data.calendar.open_dates = Array(367).fill("2026-09-21"); },
  ])("rejects invalid calendar partition or provenance case %#", (change) => {
    const data = fixture(); change(data); expect(parseMarketContext(data, parameters, request)).toBeNull();
  });
  it("accepts partial coverage but only marks dates inside it as open or closed", () => {
    const data = fixture(); data.calendar.coverage_start = "2026-09-22"; data.calendar.open_dates.shift(); data.calendar.unknown_dates = ["2026-09-21"]; markLimited(data);
    expect(parseMarketContext(data, parameters, request)).not.toBeNull();
  });
  it("accepts a missing calendar block without making weekend assumptions", () => {
    const data = fixture(); unknownCalendar(data); data.calendar.coverage_start = null; data.calendar.coverage_end = null;
    data.calendar.published_at = null; data.calendar.source = null; data.calendar.reference = null;
    expect(parseMarketContext(data, parameters, request)).not.toBeNull();
  });
  it("keeps all dates unknown when calendar publication is missing or late", () => {
    const data = fixture(); unknownCalendar(data); data.calendar.published_at = null;
    expect(parseMarketContext(data, parameters, request)).not.toBeNull();
    data.calendar.published_at = "2026-09-26T00:00:00+08:00"; expect(parseMarketContext(data, parameters, request)).not.toBeNull();
  });
  it("does not classify unknown dates as unexpected bars", () => {
    const data = fixture(); unknownCalendar(data); data.calendar.unexpected_bar_dates = ["2026-09-23"];
    expect(parseMarketContext(data, parameters, request)).toBeNull();
  });
  it("does not downgrade missing open-date bars to a suspension fact", () => {
    const data = fixture(); expect(data.calendar.missing_bar_dates).not.toHaveLength(0);
    expect(parseMarketContext(data, parameters, request)?.status).toBe("available");
    expect(marketContextNotice("open_dates_without_bars_do_not_imply_suspension")).toMatch(/不等于股票停牌/);
  });
  it("retains bounded unknown notice codes only as generic limitations", () => {
    const data = fixture(); data.warnings.push("future_unknown_warning");
    expect(parseMarketContext(data, parameters, request)).not.toBeNull();
    expect(marketContextNotice("future_unknown_warning")).toMatch(/未识别/);
    expect(marketContextNotice("__proto__")).toMatch(/未识别/);
    expect(marketContextNotice("bar_on_reported_closed_date")).toMatch(/未识别/);
    expect(marketContextNotice("bar_on_reported_closed_date", "conflict")).toMatch(/未删除或修改行情/);
  });
});

describe("read-only market context card", () => {
  it("omits absent context and safely degrades invalid auxiliary data", () => {
    const { container, rerender } = render(<MarketContext value={null} parameters={parameters} reportRequestId={request} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<MarketContext value={{ status: "unavailable" }} parameters={parameters} reportRequestId={request} />);
    expect(screen.getByRole("note")).toHaveTextContent("不影响研究答案、指标证据或行情展示");
    rerender(<MarketContext value={{ ...fixture(), security: { toString: 0 } }} parameters={parameters} reportRequestId={request} />);
    expect(screen.getByRole("note")).toHaveTextContent("未知状态没有被当作正常状态");
  });
  it("shows synthetic provenance, business date, coverage, collection and the non-PIT boundary", () => {
    render(<MarketContext value={fixture()} parameters={parameters} reportRequestId={request} />);
    expect(screen.getByText("人工样例 · 非真实市场事实")).toBeInTheDocument();
    expect(screen.getByText(/业务分析日：2026-09-25/)).toBeInTheDocument();
    expect(screen.getByText(/采集时间：2026-10-22T12:30:00/)).toBeInTheDocument();
    expect(screen.getByText(/业务有效期：2026-01-01 — 2026-12-31/)).toBeInTheDocument();
    expect(screen.getByText(/这不是 point-in-time 数据/)).toBeInTheDocument();
    expect(screen.getByText("24 个自然日")).toBeInTheDocument();
    expect(screen.getByText(/上市自然日数不是交易日数/)).toBeInTheDocument();
  });
  it("marks an unverified local snapshot separately from synthetic data", () => {
    const data = fixture(); data.data_authenticity = "unverified_local_snapshot"; data.security.source = "szse"; data.calendar.source = "curated_local";
    render(<MarketContext value={data} parameters={parameters} reportRequestId={request} />);
    expect(screen.getByText("本地快照 · 内容与来源未独立核实")).toBeInTheDocument();
    expect(screen.getByText(/来源：深交所（快照标注）/)).toBeInTheDocument();
  });
  it("marks history as a saved context rather than a refreshed query", () => {
    render(<MarketContext value={fixture()} parameters={parameters} reportRequestId={request} historical />);
    expect(screen.getByText("已保存上下文 · 非新查询")).toBeInTheDocument();
    expect(screen.getByText(/没有重新查询或刷新证券状态/)).toBeInTheDocument();
  });
  it("keeps unknown ST and suspension values explicit instead of normal", () => {
    const data = fixture(); data.security.status = "limited"; data.security.reason = "partial_state_unknown";
    data.security.st_status = "unknown"; data.security.suspension_status = "unknown"; markLimited(data);
    render(<MarketContext value={data} parameters={parameters} reportRequestId={request} />);
    expect(screen.getByText("未知，不能视为非 ST")).toBeInTheDocument();
    expect(screen.getByText("未知，不能视为正常交易")).toBeInTheDocument();
  });
  it("shows open/closed/unknown counts and expandable dates without making a request", () => {
    const fetch = vi.spyOn(globalThis, "fetch");
    render(<MarketContext value={fixture()} parameters={parameters} reportRequestId={request} />);
    const calendar = screen.getByRole("region", { name: "交易所日历" });
    expect(within(calendar).getByText("4 日")).toBeInTheDocument();
    expect(within(calendar).getByText("1 日")).toBeInTheDocument();
    const summary = screen.getByText("报告开市日期：4 日 · 展开日期");
    fireEvent.click(summary); expect(summary.closest("details")).toHaveAttribute("open");
    expect(within(summary.closest("details")!).getByText("2026-09-21")).toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });
  it("does not label missing open-date bars as suspension or silently alter conflicts", () => {
    const data = fixture(); data.conflicts = ["bar_on_reported_closed_date"]; markLimited(data);
    render(<MarketContext value={data} parameters={parameters} reportRequestId={request} />);
    expect(screen.getByText(/开市但缺少日线不等于停牌/)).toBeInTheDocument();
    expect(screen.getByText(/仅提示来源冲突，未删除或修改行情/)).toBeInTheDocument();
    expect(screen.getByText(/未评估涨跌停、委托资格或完整交易规则/)).toBeInTheDocument();
  });
  it("renders unfamiliar code only as a fixed generic reminder, never raw text or HTML", () => {
    const data = fixture(); data.warnings.push("UNTRUSTED_ASSURED_TRADABLE");
    const { container } = render(<MarketContext value={data} parameters={parameters} reportRequestId={request} />);
    expect(screen.getByText(/快照包含未识别的限制项/)).toBeInTheDocument();
    expect(container).not.toHaveTextContent("UNTRUSTED_ASSURED_TRADABLE");
    expect(container.querySelector("[style], img, script")).toBeNull();
  });
  it("describes a future listing date without inventing a negative age", () => {
    const data = fixture(); data.security.listing_date = "2026-09-26"; data.security.listing_age_calendar_days = null;
    render(<MarketContext value={data} parameters={parameters} reportRequestId={request} />);
    expect(screen.getByText("尚未到所报上市日")).toBeInTheDocument();
  });
});
