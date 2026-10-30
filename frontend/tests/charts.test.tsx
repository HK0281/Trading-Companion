import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ResearchChart } from "../src/ResearchChart";
import { chartScale, lineSegments, parseResearchChart, parseIntervalChart, validChartDate,
  type ChartParameters, type ChartSeries, type ResearchChartData, type IntervalChartData, type ChartInterval } from "../src/charts";

afterEach(() => vi.unstubAllGlobals());

function capabilities(hourly = false) {
  return { version: "research-chart-capabilities-v1", research_id: request, intervals: {
    "1w": { enabled: true, code: "ready" },
    "60m": { enabled: hourly, code: hourly ? "ready" : "chart_hourly_source_unsupported" },
    "120m": { enabled: hourly, code: hourly ? "ready" : "chart_hourly_source_unsupported" },
  } };
}

function intervalFixture(daily: ResearchChartData, interval: ChartInterval = "1w"): IntervalChartData {
  const hourly = interval !== "1w", start = "2026-09-25T09:30:00+08:00";
  const end = hourly ? `2026-09-25T${interval === "60m" ? "10:30" : "11:30"}:00+08:00` : "2026-09-25T15:00:00+08:00";
  return { ...daily, version: "research-chart-interval-v1", code: "chart_only_series", interval, timezone: "Asia/Shanghai",
    retrieved_at: "2026-09-26T02:00:00Z", coverage: { start_at: start, end_at: end, bar_count: 1 },
    bars: [{ date: hourly ? end : "2026-09-25", start_at: start, end_at: end, complete: hourly,
      open: 10, high: 14, low: 9, close: 13, volume: 300 }],
    series: daily.series.map((s) => ({ ...s, values: [null], statuses: ["insufficient_history"] })),
  };
}

function continuousHourlyFixture(daily: ResearchChartData): IntervalChartData {
  const value = intervalFixture(daily, "60m");
  value.bars = [["09:30", "10:30"], ["10:30", "11:30"], ["13:00", "14:00"], ["14:00", "15:00"]].map(([start, end]) => ({
    ...value.bars[0], date: `2026-09-25T${end}:00+08:00`,
    start_at: `2026-09-25T${start}:00+08:00`, end_at: `2026-09-25T${end}:00+08:00`,
  }));
  value.series = value.series.map((series) => ({ ...series,
    values: Array(4).fill(null), statuses: Array(4).fill("insufficient_history"),
  }));
  value.coverage = { start_at: value.bars[0].start_at, end_at: value.bars.at(-1)!.end_at, bar_count: 4 };
  return value;
}

describe("independent chart intervals", () => {
  it("accepts adjacent morning and afternoon hourly bars while rejecting duplicates and reverse order", async () => {
    const daily = { ...fixture(), source: "tencent", price_adjustment: "unadjusted", data_authenticity: "provider_reported" } as ResearchChartData;
    const value = continuousHourlyFixture(daily);
    expect(parseIntervalChart(value, daily, "60m")).toEqual(value);
    const duplicate = structuredClone(value); duplicate.bars[1] = { ...duplicate.bars[0] };
    expect(parseIntervalChart(duplicate, daily, "60m")).toBeNull();
    const reversed = structuredClone(value); reversed.bars.reverse();
    expect(parseIntervalChart(reversed, daily, "60m")).toBeNull();
    const { container } = render(<ResearchChart value={daily} parameters={{ ...parameters, source: "tencent" }} reportRequestId={request}
      capabilities={capabilities(true)} loadInterval={vi.fn().mockResolvedValue(value)} />);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "1小时" })));
    expect(screen.getByRole("group", { name: "互动1小时图" })).toBeVisible();
    expect(container.querySelectorAll(".chart-volume")).toHaveLength(4);
    expect(screen.queryByText(/所选周期暂不可用/)).not.toBeInTheDocument();
  });

  it("refreshes the authenticity warning and cancels old interval work when the same research snapshot changes", async () => {
    const daily = fixture();
    let resolve: (value: unknown) => void = () => {};
    const load = vi.fn().mockImplementation(() => new Promise((done) => { resolve = done; }));
    const { rerender } = render(<ResearchChart value={daily} parameters={parameters} reportRequestId={request}
      capabilities={capabilities()} loadInterval={load} />);
    fireEvent.click(screen.getByRole("button", { name: "周线" }));
    const updated = { ...daily, data_authenticity: "unverified_csv" } as ResearchChartData;
    rerender(<ResearchChart value={updated} parameters={parameters} reportRequestId={request}
      capabilities={capabilities()} loadInterval={load} />);
    expect(load.mock.calls[0][1].aborted).toBe(true);
    expect(screen.getByText("CSV 输入 · 真实性未核验").closest("p")).toHaveClass("chart-warning");
    expect(screen.queryByText("人工样例 · 非真实行情")).not.toBeInTheDocument();
    await act(async () => resolve(intervalFixture(daily)));
    expect(plot()).toBeVisible(); expect(screen.queryByRole("group", { name: "互动周线图" })).not.toBeInTheDocument();
    expect(screen.getByText("CSV 输入 · 真实性未核验")).toBeVisible();
  });

  it("defaults to daily without a request and keeps source capability restrictions", async () => {
    const load = vi.fn().mockResolvedValue(intervalFixture(fixture()));
    render(<ResearchChart value={fixture()} parameters={parameters} reportRequestId={request} capabilities={capabilities()} loadInterval={load} />);
    expect(screen.getByRole("button", { name: "日线" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "1小时" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "2小时" })).toBeDisabled();
    expect(load).not.toHaveBeenCalled();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "周线" })));
    expect(load).toHaveBeenCalledOnce();
    expect(screen.getByRole("group", { name: "互动周线图" })).toBeVisible();
    expect(screen.getByText(/本周未完/)).toBeVisible();
    expect(screen.getByText("人工样例 · 非真实行情")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "日线" }));
    expect(plot()).toBeVisible(); expect(load).toHaveBeenCalledOnce();
  });

  it("discards cancelled old responses and falls back on failure without damaging daily", async () => {
    const daily = { ...fixture(), source: "tencent", price_adjustment: "unadjusted", data_authenticity: "provider_reported" } as ResearchChartData;
    let resolve: (value: unknown) => void = () => {};
    const load = vi.fn().mockImplementationOnce(() => new Promise((done) => { resolve = done; }))
      .mockResolvedValueOnce(intervalFixture(daily, "120m")).mockRejectedValueOnce(new Error("private"));
    const { unmount } = render(<ResearchChart value={daily} parameters={{ ...parameters, source: "tencent" }} reportRequestId={request}
      capabilities={capabilities(true)} loadInterval={load} />);
    fireEvent.click(screen.getByRole("button", { name: "1小时" }));
    expect(screen.getByRole("button", { name: "1小时" })).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("button", { name: "1小时" })).toHaveTextContent("1小时…");
    expect(screen.getByText(/正在加载1小时/)).toBeVisible();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "2小时" })));
    expect(load.mock.calls[0][1].aborted).toBe(true);
    expect(screen.getByRole("group", { name: "互动2小时图" })).toBeVisible();
    expect(screen.getByRole("button", { name: "2小时" })).toHaveAttribute("aria-busy", "false");
    await act(async () => resolve(intervalFixture(daily, "60m")));
    expect(screen.getByRole("group", { name: "互动2小时图" })).toBeVisible();
    expect(screen.getByText(/分钟数据有延迟、非实时/)).toBeVisible();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "周线" })));
    expect(plot()).toBeVisible(); expect(screen.getByText(/所选周期暂不可用/)).toBeVisible();
    expect(screen.queryByText("private")).not.toBeInTheDocument();
    unmount(); expect(load.mock.calls[2][1].aborted).toBe(true);
  });

  it("rejects cross-research, timestamp, source, window, field and size pollution", () => {
    const daily = fixture();
    expect(parseIntervalChart(intervalFixture(daily), daily, "1w")).not.toBeNull();
    for (const mutate of [
      (v: IntervalChartData) => { v.request_id = "other"; },
      (v: IntervalChartData) => { v.source = "tencent"; },
      (v: IntervalChartData) => { v.bars[0].end_at = "2026-09-25T15:00:00Z"; },
      (v: IntervalChartData) => { v.bars[0].complete = true; },
      (v: IntervalChartData) => { v.bars[0].start_at = "2026-09-20T09:30:00+08:00"; },
      (v: IntervalChartData) => { v.series[0].unit = "shares"; },
      (v: IntervalChartData) => { v.series[0].parameters.window = 4; },
      (v: IntervalChartData) => { v.coverage.bar_count = 2; },
      (v: IntervalChartData) => { (v as unknown as Record<string, unknown>).extra = "x".repeat(256 * 1024); },
    ]) {
      const candidate = intervalFixture(structuredClone(daily)); mutate(candidate);
      expect(parseIntervalChart(candidate, daily, "1w")).toBeNull();
    }
  });
});

const request = "chart-request-1";
const parameters: ChartParameters = { instrument_id: "000001.SZ", start_date: "2026-09-21", as_of_date: "2026-09-25", source: "csv",
  indicators: [{ kind: "sma", window: 3 }, { kind: "rsi", window: 2 }] };
function fixture(): ResearchChartData {
  return {
    version: "research-chart-v1", status: "available", code: "aligned_research_series",
    validation_scope: "request_bound_aligned_series_not_full_historical_evidence", request_id: request,
    instrument_id: "000001.SZ", start_date: "2026-09-21", as_of_date: "2026-09-25", source: "csv", price_adjustment: "unknown",
    data_authenticity: "synthetic", retrieved_at: null, context: "current", saved_at: null,
    bars: [
      { date: "2026-09-21", open: 10, high: 12, low: 9, close: 11, volume: 100 },
      { date: "2026-09-22", open: 11, high: 12, low: 9, close: 10, volume: 50 },
      { date: "2026-09-24", open: 11, high: 11, low: 11, close: 11, volume: 0 },
      { date: "2026-09-25", open: 12, high: 14, low: 11, close: 13, volume: 150 },
    ],
    series: [
      { id: "sma(window=3):value", indicator_id: "sma(window=3)", kind: "sma", parameters: { window: 3 },
        algorithm_version: "sma-close-v1", field: "value", label: "简单移动平均", unit: "CNY/share", start_effect_warning: false,
        values: [null, null, 10.666, 11.333], statuses: ["insufficient_history", "insufficient_history", "available", "available"] },
      { id: "rsi(window=2):value", indicator_id: "rsi(window=2)", kind: "rsi", parameters: { window: 2 },
        algorithm_version: "rsi-wilder-flat50-v1", field: "value", label: "RSI", unit: "index_points", start_effect_warning: true,
        values: [null, null, 50, 80], statuses: ["insufficient_history", "insufficient_history", "available", "available"] },
    ],
  };
}

function extendedFixture(): { data: ResearchChartData; scope: ChartParameters } {
  const data = fixture();
  const series = (kind: string, field: string, unit: ChartSeries["unit"], values: (number | null)[],
    statuses: ChartSeries["statuses"], label: string): ChartSeries => {
    const params: Record<string, number> = kind === "obv" ? {} : { window: 2 };
    const id = kind === "obv" ? "obv()" : `${kind}(window=2)`;
    const versions: Record<string, string> = { adx: "adx-wilder-double-smoothed-v1", obv: "obv-snapshot-zero-seed-v1", vwma: "vwma-close-volume-weighted-v1" };
    return { id: `${id}:${field}`, indicator_id: id, kind, parameters: params, algorithm_version: versions[kind],
      field, label, unit, start_effect_warning: kind !== "vwma", values, statuses };
  };
  data.series = [
    series("adx", "value", "index_points", [null, null, null, 25], ["insufficient_history", "insufficient_history", "insufficient_history", "available"], "ADX(2) 趋势强度"),
    series("adx", "plus_di", "index_points", [null, null, 20, 35], ["insufficient_history", "insufficient_history", "available", "available"], "ADX(2) 正向指标"),
    series("adx", "minus_di", "index_points", [null, null, 30, 15], ["insufficient_history", "insufficient_history", "available", "available"], "ADX(2) 负向指标"),
    series("obv", "value", "shares", [0, -50, -50, 100], ["available", "available", "available", "available"], "OBV 能量潮"),
    series("vwma", "value", "CNY/share", [null, 10.666, 10, 13], ["insufficient_history", "available", "available", "available"], "VWMA(2) 成交量加权均线"),
  ];
  return { data, scope: { ...parameters, indicators: [{ kind: "adx", window: 2 }, { kind: "obv" }, { kind: "vwma", window: 2 }] } };
}

function multiFieldFixture(): ResearchChartData {
  const data = fixture();
  const specs: { kind: string; parameters: Record<string, number>; id: string; algorithm: string; fields: string[]; unit: ChartSeries["unit"] }[] = [
    { kind: "boll", parameters: { window: 3, stddev_multiplier: 2 }, id: "boll(window=3,stddev_multiplier=2.0)", algorithm: "boll-population-ddof0-v1", fields: ["middle", "upper", "lower"], unit: "CNY/share" },
    { kind: "macd", parameters: { fast: 2, slow: 3, signal: 2 }, id: "macd(fast=2,slow=3,signal=2)", algorithm: "macd-sma-seed-double-histogram-v1", fields: ["dif", "dea", "histogram"], unit: "CNY/share" },
    { kind: "volume", parameters: { window: 3 }, id: "volume(window=3)", algorithm: "volume-prior-records-ratio-v1", fields: ["volume_shares", "volume_ma", "previous_volume_ma", "relative_volume"], unit: "shares" },
  ];
  data.series = [data.series[0], ...specs.flatMap((spec) => spec.fields.map((field) => ({
    id: `${spec.id}:${field}`, indicator_id: spec.id, kind: spec.kind, parameters: spec.parameters, algorithm_version: spec.algorithm,
    field, label: field, unit: field === "relative_volume" ? "multiple" : spec.unit,
    start_effect_warning: false, values: [1, 2, 3, 4], statuses: ["available", "available", "available", "available"],
  } as ChartSeries)))];
  return data;
}

describe("request-bound chart parser", () => {
  it("accepts a complete aligned chart and its exact indicator scope", () => {
    expect(parseResearchChart(fixture(), parameters, request)).toEqual(fixture());
  });
  it.each([undefined, null, 4, "chart", [], {}, { status: "unavailable", code: "chart_data_unavailable" }])("returns null for absent, unavailable or malformed payload %j", (value) => {
    expect(parseResearchChart(value, parameters, request)).toBeNull();
  });
  it.each([
    ["version", "research-chart-v2"], ["status", "pending"], ["code", "trusted"], ["validation_scope", "all_evidence_verified"],
    ["request_id", "other-request"], ["instrument_id", "000002.SZ"], ["start_date", "2026-09-20"], ["as_of_date", "2026-09-26"],
    ["source", "tencent"], ["price_adjustment", "unadjusted"], ["data_authenticity", "provider_reported"],
    ["retrieved_at", "2026-09-25T12:30:00"], ["retrieved_at", "2026-02-30T12:30:00Z"],
    ["retrieved_at", "2026-09-25T25:30:00Z"], ["retrieved_at", "2026-09-25T12:30:00+25:00"],
    ["context", "real_time"], ["context", "saved"], ["saved_at", "2026-09-25T12:30:00Z"],
  ])("rejects invalid envelope %s=%j", (key, value) => {
    const data = { ...fixture(), [key]: value };
    expect(parseResearchChart(data, parameters, request)).toBeNull();
  });
  it("requires the report request ID and confirmed scope even when the chart looks valid", () => {
    expect(parseResearchChart(fixture(), parameters, undefined)).toBeNull();
    expect(parseResearchChart(fixture(), null, request)).toBeNull();
    expect(parseResearchChart(fixture(), { ...parameters, indicators: [{ kind: "ema", window: 3 }] }, request)).toBeNull();
    expect(parseResearchChart(fixture(), { ...parameters, indicators: [{ kind: "sma", window: 3 }] }, request)).toBeNull();
    expect(parseResearchChart(fixture(), { ...parameters, indicators: [] }, request)).toBeNull();
    expect(parseResearchChart(fixture(), { ...parameters, indicators: [{ kind: "sma", window: 3 }, { kind: "sma", window: 3 }] }, request)).toBeNull();
  });
  it.each(["source", "context", "data_authenticity", "price_adjustment", "retrieved_at", "saved_at"]) ("rejects JSON objects in %s without coercion or an exception", (key) => {
    const data = { ...fixture(), [key]: { toString: 0 } };
    expect(() => parseResearchChart(data, parameters, request)).not.toThrow();
    expect(parseResearchChart(data, parameters, request)).toBeNull();
  });
  it("can display old API scopes without indicators but still validates the chart schema", () => {
    expect(parseResearchChart(fixture(), { ...parameters, indicators: undefined }, request)).not.toBeNull();
  });
  it.each(["2026-02-30", "2025-02-29", "2026-13-01", "2026-00-01", "2026-09-00", "2026-9-01", "0000-01-01", "2026-01-01junk"]) ("rejects invalid date without Date normalization: %s", (date) => {
    expect(validChartDate(date)).toBe(false);
  });
  it("accepts real leap dates and offset-aware retrieval timestamps", () => {
    expect(validChartDate("2024-02-29")).toBe(true);
    const data = fixture(); data.retrieved_at = "2026-09-25T12:30:00.123456+08:00";
    expect(parseResearchChart(data, parameters, request)).not.toBeNull();
  });
  it("accepts a saved chart only with an aware save time", () => {
    const data = fixture(); data.context = "saved"; data.saved_at = "2026-09-25T12:30:00Z";
    expect(parseResearchChart(data, parameters, request)).not.toBeNull();
  });
  it("enforces the remote source's unadjusted/provider-reported combination", () => {
    const data = fixture(); data.source = "tencent"; data.price_adjustment = "unadjusted"; data.data_authenticity = "provider_reported";
    expect(parseResearchChart(data, { ...parameters, source: "tencent" }, request)).not.toBeNull();
    data.price_adjustment = "unknown";
    expect(parseResearchChart(data, { ...parameters, source: "tencent" }, request)).toBeNull();
  });
  it.each([
    (data: ResearchChartData) => { data.bars = []; },
    (data: ResearchChartData) => { data.bars = Array(367).fill(data.bars[0]); },
    (data: ResearchChartData) => { data.bars[1].date = data.bars[0].date; },
    (data: ResearchChartData) => { data.bars.reverse(); },
    (data: ResearchChartData) => { data.bars[0].date = "2026-09-20"; },
    (data: ResearchChartData) => { data.bars[0].date = "2026-02-30"; },
    (data: ResearchChartData) => { data.bars[0].high = 5; },
    (data: ResearchChartData) => { data.bars[0].low = 15; },
    (data: ResearchChartData) => { data.bars[0].close = -1; },
    (data: ResearchChartData) => { data.bars[0].open = Number.POSITIVE_INFINITY; },
    (data: ResearchChartData) => { data.bars[0].volume = .5; },
    (data: ResearchChartData) => { data.bars[0].volume = -1; },
    (data: ResearchChartData) => { data.bars[0].volume = 1e16; },
    (data: ResearchChartData) => { data.series[0].values.pop(); },
    (data: ResearchChartData) => { data.series[0].statuses.pop(); },
    (data: ResearchChartData) => { data.series[0].values[3] = NaN; },
    (data: ResearchChartData) => { data.series[0].values[3] = 1e16; },
    (data: ResearchChartData) => { data.series[0].values[0] = 0; },
    (data: ResearchChartData) => { data.series[0].values[3] = null; },
    (data: ResearchChartData) => { data.series[0].statuses[3] = "pending" as never; },
    (data: ResearchChartData) => { data.series[0].unit = "shares"; },
    (data: ResearchChartData) => { data.series[0].parameters = { window: 3, extra: 8 }; },
    (data: ResearchChartData) => { data.series[0].parameters.window = 121; },
    (data: ResearchChartData) => { data.series[0].parameters.window = 2; },
    (data: ResearchChartData) => { data.series[0].algorithm_version = "ema-sma-seed-v1"; },
    (data: ResearchChartData) => { data.series[0].field = "unexpected"; },
    (data: ResearchChartData) => { data.series[0].id = "other"; },
    (data: ResearchChartData) => { data.series[0].kind = "__proto__"; },
    (data: ResearchChartData) => { data.series[0].start_effect_warning = "true" as never; },
    (data: ResearchChartData) => { data.series[1].values[3] = 101; },
    (data: ResearchChartData) => { data.series.push(data.series[0]); },
    (data: ResearchChartData) => { data.series = []; },
  ])("rejects malformed bars/series case %#", (change) => {
    const data = fixture(); change(data); expect(parseResearchChart(data, parameters, request)).toBeNull();
  });
  it("rejects more than twelve distinct indicator instances", () => {
    const data = fixture(); data.series = Array.from({ length: 13 }, (_, index) => ({ ...data.series[0],
      id: `sma(window=${index + 1}):value`, indicator_id: `sma(window=${index + 1})`, parameters: { window: index + 1 } }));
    expect(parseResearchChart(data, { ...parameters, indicators: undefined }, request)).toBeNull();
  });
  it("accepts full BOLL/MACD/volume field definitions and rejects omitted fields", () => {
    const data = multiFieldFixture();
    expect(parseResearchChart(data, { ...parameters, indicators: undefined }, request)).not.toBeNull();
    data.series.pop(); expect(parseResearchChart(data, { ...parameters, indicators: undefined }, request)).toBeNull();
  });
  it("accepts aligned ADX components, signed integer OBV and price-unit VWMA", () => {
    const { data, scope } = extendedFixture();
    expect(parseResearchChart(data, scope, request)).toEqual(data);
  });
  it("retains undefined VWMA points for a zero-volume window without filling the line", () => {
    const { data, scope } = extendedFixture();
    data.series[4].values[2] = null; data.series[4].statuses[2] = "undefined";
    expect(parseResearchChart(data, scope, request)).not.toBeNull();
  });
  it.each([
    (data: ResearchChartData) => { data.series[0].values[3] = 101; },
    (data: ResearchChartData) => { data.series[1].values[3] = -1; },
    (data: ResearchChartData) => { data.series[2].unit = "shares"; },
    (data: ResearchChartData) => { data.series.splice(1, 1); },
    (data: ResearchChartData) => { data.series[3].values[1] = -50.25; },
    (data: ResearchChartData) => { data.series[3].values[1] = Number.MAX_SAFE_INTEGER + 1; },
    (data: ResearchChartData) => { data.series[3].parameters = { window: 2 }; },
    (data: ResearchChartData) => { data.series[3].indicator_id = "obv(window=2)"; data.series[3].id = "obv(window=2):value"; },
    (data: ResearchChartData) => { data.series[3].indicator_id = "obv(,)"; data.series[3].id = "obv(,):value"; },
    (data: ResearchChartData) => { data.series[3].values[2] = null; data.series[3].statuses[2] = "undefined"; },
    (data: ResearchChartData) => { data.series[4].values[3] = 0; },
    (data: ResearchChartData) => { data.series[4].algorithm_version = "sma-close-v1"; },
  ])("rejects invalid extended indicator contracts case %#", (change) => {
    const { data, scope } = extendedFixture(); change(data);
    expect(parseResearchChart(data, scope, request)).toBeNull();
  });
});

describe("chart geometry", () => {
  it("breaks paths at every missing value without replacing it by zero", () => {
    expect(lineSegments([null, 2, null, 4, 5, null, 8], (index) => index * 10, (value) => value * 2)).toEqual(["10,4", "30,8 40,10", "60,16"]);
    expect(lineSegments([null, null], (index) => index, (value) => value)).toEqual([]);
  });
  it("keeps flat, zero, single-point and empty scales finite", () => {
    for (const values of [[0], [10, 10], [], [1e15]]) {
      const scale = chartScale(values, 20, 200, true);
      expect(scale.max).toBeGreaterThan(scale.min); expect(Number.isFinite(scale.project(values[0] ?? 0))).toBe(true);
    }
  });
});

function readout() { return screen.getByRole("note", { name: "选定K线读数" }); }
function plot() { return screen.getByRole("group", { name: "互动日线图" }); }
function pointer(svg: SVGSVGElement, type: string, values: Record<string, unknown> = {}) {
  const event = new Event(type, { bubbles: true });
  Object.assign(event, { pointerId: 1, pointerType: "mouse", isPrimary: true, button: 0, clientX: 187, clientY: 100 }, values);
  fireEvent(svg, event);
}
function pointerSurface() {
  const svg = screen.getByRole("img") as unknown as SVGSVGElement;
  vi.spyOn(svg, "getBoundingClientRect").mockReturnValue({ left: 100, top: 50, width: 480, height: 264.5 } as DOMRect);
  const captures = new Set<number>();
  const capture = vi.fn((id: number) => { captures.add(id); });
  const release = vi.fn((id: number) => { captures.delete(id); });
  Object.defineProperties(svg, {
    setPointerCapture: { value: capture }, hasPointerCapture: { value: (id: number) => captures.has(id) },
    releasePointerCapture: { value: release },
  });
  return { svg, capture, release };
}

describe("research chart component", () => {
  it("omits an absent chart and degrades invalid charts without throwing", () => {
    const { rerender, container } = render(<ResearchChart value={null} parameters={parameters} reportRequestId={request} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<ResearchChart value={{ status: "unavailable" }} parameters={parameters} reportRequestId={request} />);
    expect(screen.getByRole("note")).toHaveTextContent("不影响上方已确认的研究答案与证据");
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });
  it("shows the chart immediately with concise provenance and no retired tables or slider", () => {
    const { container, rerender } = render(<ResearchChart value={fixture()} parameters={parameters} reportRequestId={request} />);
    expect(screen.getByRole("img", { name: /历史日 K 线/ })).toBeVisible();
    expect(screen.getByText("人工样例 · 非真实行情").closest("p")).toHaveClass("chart-warning");
    expect(screen.getByText(/复权：未知/)).toHaveTextContent("来源：CSV");
    expect(screen.getByText(/历史日线，非实时/)).toBeVisible();
    expect(container.querySelector("details, table, input[type=range], [style]")).toBeNull();
    expect(screen.queryByRole("note", { name: "选定K线读数" })).not.toBeInTheDocument();
    expect(container.querySelector(".chart-crosshair")).toBeNull();
    expect(screen.queryByText("数据口径与采集记录")).not.toBeInTheDocument();
    rerender(<ResearchChart value={{ ...fixture(), data_authenticity: "unverified_csv" }} parameters={parameters} reportRequestId={request} />);
    expect(screen.getByText("CSV 输入 · 真实性未核验").closest("p")).toHaveClass("chart-warning");
  });
  it("marks both saved context and explicitly opened history as a snapshot", () => {
    const data = fixture(); data.context = "saved"; data.saved_at = "2026-09-25T12:30:00Z";
    const { rerender } = render(<ResearchChart value={data} parameters={parameters} reportRequestId={request} />);
    expect(screen.getByText(/已保存快照，非新分析/)).toBeVisible();
    rerender(<ResearchChart value={fixture()} parameters={parameters} reportRequestId={request} historical />);
    expect(screen.getByText(/已保存快照，非新分析/)).toBeVisible();
  });
  it("hides hover and click inspection on leave but keeps keyboard inspection until blur", () => {
    const { container } = render(<ResearchChart value={fixture()} parameters={parameters} reportRequestId={request} />);
    const { svg } = pointerSurface();
    pointer(svg, "pointerover", { relatedTarget: document.body }); expect(readout()).toHaveTextContent("2026-09-21");
    pointer(svg, "pointerout", { relatedTarget: document.body });
    expect(screen.queryByRole("note", { name: "选定K线读数" })).not.toBeInTheDocument();
    expect(container.querySelector(".chart-crosshair")).toBeNull();
    pointer(svg, "pointerdown"); pointer(svg, "pointerup");
    expect(plot()).toHaveFocus(); expect(readout()).toBeVisible();
    pointer(svg, "pointerout", { relatedTarget: document.body });
    expect(plot()).toHaveFocus();
    expect(screen.queryByRole("note", { name: "选定K线读数" })).not.toBeInTheDocument();
    act(() => { plot().blur(); plot().focus(); });
    expect(readout()).toBeVisible();
    pointer(svg, "pointerout", { relatedTarget: document.body });
    expect(readout()).toBeVisible();
    act(() => plot().blur());
    expect(screen.queryByRole("note", { name: "选定K线读数" })).not.toBeInTheDocument();
    expect(container.querySelector(".chart-crosshair")).toBeNull();
  });
  it("moves by keyboard, keeps missing readings explicit and announces only date and close", () => {
    render(<ResearchChart value={fixture()} parameters={parameters} reportRequestId={request} />);
    act(() => plot().focus()); expect(plot()).toHaveFocus();
    fireEvent.keyDown(plot(), { key: "Home" });
    expect(readout()).toHaveTextContent("2026-09-21");
    expect(within(readout()).getAllByText("历史不足")).toHaveLength(2);
    expect(within(readout()).getAllByText("—")).toHaveLength(2);
    expect(readout()).toHaveTextContent("量100 股");
    expect(screen.getByRole("status")).toHaveTextContent(/^2026-09-21，收盘 11 元\/股$/);
    fireEvent.keyDown(plot(), { key: "ArrowLeft" }); expect(readout()).toHaveTextContent("2026-09-21");
    fireEvent.keyDown(plot(), { key: "ArrowRight" }); expect(readout()).toHaveTextContent("2026-09-22");
    fireEvent.keyDown(plot(), { key: "ArrowRight" }); expect(readout()).toHaveTextContent("量0 股");
    fireEvent.keyDown(plot(), { key: "End" }); expect(readout()).toHaveTextContent("2026-09-25");
    fireEvent.keyDown(plot(), { key: "ArrowRight" }); expect(readout()).toHaveTextContent("2026-09-25");
  });
  it("converts scaled pointer coordinates, clamps both ends and moves both crosshair lines without live chatter", () => {
    const { container } = render(<ResearchChart value={fixture()} parameters={parameters} reportRequestId={request} />);
    const { svg } = pointerSurface();
    fireEvent.keyDown(plot(), { key: "End" });
    const announcement = screen.getByRole("status").textContent;
    pointer(svg, "pointermove");
    expect(readout()).toHaveTextContent("2026-09-21");
    expect(container.querySelector(".chart-selection-x")).toHaveAttribute("x1", "174");
    expect(container.querySelector(".chart-selection-y")).toHaveAttribute("y1", "100");
    expect(readout()).toHaveClass("chart-tooltip-right");
    pointer(svg, "pointermove", { clientX: 900, clientY: 900 });
    expect(readout()).toHaveTextContent("2026-09-25");
    expect(readout()).toHaveClass("chart-tooltip-left");
    expect(container.querySelector(".chart-selection-y")).toHaveAttribute("y1", "487");
    pointer(svg, "pointermove", { clientX: 0, clientY: 0 });
    expect(readout()).toHaveTextContent("2026-09-21");
    expect(container.querySelector(".chart-selection-y")).toHaveAttribute("y1", "28");
    expect(screen.getByRole("status").textContent).toBe(announcement);
  });
  it.each(["pointerup", "pointercancel", "lostpointercapture"])("ends touch selection on %s and ignores extra fingers", (ending) => {
    render(<ResearchChart value={fixture()} parameters={parameters} reportRequestId={request} />);
    const { svg, capture, release } = pointerSurface();
    pointer(svg, "pointermove", { pointerType: "touch" });
    expect(screen.queryByRole("note", { name: "选定K线读数" })).not.toBeInTheDocument();
    pointer(svg, "pointerdown", { pointerType: "touch" });
    expect(capture).toHaveBeenCalledWith(1); expect(plot()).toHaveFocus();
    pointer(svg, "pointerdown", { pointerType: "touch", pointerId: 2, isPrimary: false, clientX: 900 });
    pointer(svg, "pointermove", { pointerType: "touch", pointerId: 2, clientX: 900 });
    pointer(svg, "pointerout", { pointerType: "touch", pointerId: 2, isPrimary: false, relatedTarget: document.body });
    expect(capture).toHaveBeenCalledOnce(); expect(readout()).toHaveTextContent("2026-09-21");
    pointer(svg, "pointermove", { pointerType: "touch", clientX: 900 });
    expect(readout()).toHaveTextContent("2026-09-25");
    pointer(svg, ending, { pointerType: "touch" });
    if (ending !== "lostpointercapture") expect(release).toHaveBeenCalledWith(1);
    pointer(svg, "pointermove", { pointerType: "touch" });
    expect(screen.queryByRole("note", { name: "选定K线读数" })).not.toBeInTheDocument();
    expect(document.querySelector(".chart-crosshair")).toBeNull();
    pointer(svg, "pointerdown", { pointerType: "touch" });
    expect(capture).toHaveBeenCalledTimes(2); expect(readout()).toHaveTextContent("2026-09-21");
  });
  it("does not capture secondary buttons and ignores surfaces with no geometry", () => {
    render(<ResearchChart value={fixture()} parameters={parameters} reportRequestId={request} />);
    const { svg, capture } = pointerSurface();
    pointer(svg, "pointerdown", { button: 2 });
    expect(capture).not.toHaveBeenCalled();
    vi.spyOn(svg, "getBoundingClientRect").mockReturnValue({ width: 0, height: 0 } as DOMRect);
    pointer(svg, "pointermove");
    expect(screen.queryByRole("note", { name: "选定K线读数" })).not.toBeInTheDocument();
  });
  it("toggles price overlays and separate units in both plot and readout without a request", () => {
    const fetch = vi.spyOn(globalThis, "fetch");
    const { container } = render(<ResearchChart value={fixture()} parameters={parameters} reportRequestId={request} />);
    expect(container.querySelectorAll(".chart-line")).toHaveLength(2);
    fireEvent.keyDown(plot(), { key: "End" });
    expect(readout()).toHaveTextContent("11.333 元/股"); expect(readout()).toHaveTextContent("80 点");
    fireEvent.click(screen.getByRole("checkbox", { name: "SMA3" }));
    expect(container.querySelectorAll(".chart-line")).toHaveLength(1);
    expect(readout()).not.toHaveTextContent("SMA3");
    fireEvent.change(screen.getByLabelText("独立指标"), { target: { value: "" } });
    expect(container.querySelectorAll(".chart-line")).toHaveLength(0);
    expect(readout()).not.toHaveTextContent("RSI");
    expect(fetch).not.toHaveBeenCalled(); expect(container.querySelector("[style]")).toBeNull();
  });
  it("keeps VWMA on the price axis and signed OBV in a separately selected panel", () => {
    const { data, scope } = extendedFixture();
    render(<ResearchChart value={data} parameters={scope} reportRequestId={request} />);
    expect(screen.getByRole("checkbox", { name: "VWMA2" })).toBeVisible();
    const field = screen.getByLabelText("独立指标");
    expect(field).toHaveTextContent("ADX2");
    expect(field).toHaveTextContent("+DI2"); expect(field).toHaveTextContent("−DI2");
    expect(field).toHaveTextContent("OBV"); expect(field).not.toHaveTextContent("VWMA");
    expect(field).not.toHaveTextContent(/元\/股|（点）|（股）|window=/);
    fireEvent.change(field, { target: { value: "obv():value" } });
    fireEvent.keyDown(plot(), { key: "Home" }); fireEvent.keyDown(plot(), { key: "ArrowRight" });
    expect(within(readout()).getByText("-50 股")).toBeVisible();
  });
  it("uses short BOLL, MACD and volume names while preserving internal selection identities", () => {
    const { container } = render(<ResearchChart value={multiFieldFixture()} parameters={{ ...parameters, indicators: undefined }} reportRequestId={request} />);
    expect(screen.getByRole("checkbox", { name: "SMA3" })).toBeVisible();
    expect(screen.getByRole("checkbox", { name: "BOLL(3,2)" })).toBeVisible();
    expect(screen.getByLabelText("价格指标图例")).toHaveTextContent("BOLL 中轨");
    expect(screen.getByLabelText("价格指标图例")).toHaveTextContent("BOLL 上轨");
    expect(screen.getByLabelText("价格指标图例")).toHaveTextContent("BOLL 下轨");
    expect(screen.getByRole("option", { name: "MACD DIF" })).toHaveValue("macd(fast=2,slow=3,signal=2):dif");
    for (const label of ["MACD DEA", "MACD 柱", "成交量(3)", "成交量均量(3)", "此前均量(3)", "量比(3)"]) {
      expect(screen.getByRole("option", { name: label })).toBeInTheDocument();
    }
    fireEvent.keyDown(plot(), { key: "End" });
    expect(readout()).toHaveTextContent("另有 1 条线");
    expect(readout().querySelectorAll("dl > div")).toHaveLength(9);
    expect(readout()).toHaveTextContent("SMA3"); expect(readout()).toHaveTextContent("BOLL 中轨");
    fireEvent.click(screen.getByRole("checkbox", { name: "BOLL(3,2)" }));
    expect(readout()).not.toHaveTextContent("另有");
    expect(readout()).toHaveTextContent("SMA3"); expect(readout()).toHaveTextContent("MACD DIF");
    expect(container.textContent).not.toMatch(/window=|stddev_multiplier=|fast=|slow=|signal=/);
  });
  it("keeps multiple BOLL and MACD parameter sets distinguishable with common notation", () => {
    const data = multiFieldFixture();
    const extra = data.series.filter((series) => ["boll", "macd"].includes(series.kind)).map((series) => {
      const params: Record<string, number> = series.kind === "boll" ? { window: 5, stddev_multiplier: 2 } : { fast: 3, slow: 4, signal: 2 };
      const id = series.kind === "boll" ? "boll(window=5,stddev_multiplier=2.0)" : "macd(fast=3,slow=4,signal=2)";
      return { ...series, parameters: params, indicator_id: id, id: `${id}:${series.field}` };
    });
    data.series.push(...extra);
    const { container } = render(<ResearchChart value={data} parameters={{ ...parameters, indicators: undefined }} reportRequestId={request} />);
    const legend = screen.getByLabelText("价格指标图例");
    expect(legend).toHaveTextContent("BOLL(3,2) 中轨"); expect(legend).toHaveTextContent("BOLL(5,2) 中轨");
    expect(screen.getByRole("option", { name: "MACD(2,3,2) DIF" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "MACD(3,4,2) DIF" })).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/window=|stddev_multiplier=|fast=|signal=/);
  });
  it.each([["ema", "ema-sma-seed-v1", "EMA3"], ["atr", "atr-wilder-prior-close-v1", "ATR3"]])(
    "uses a readable %s period label", (kind, algorithm, label) => {
      const data = fixture(); data.series[0] = { ...data.series[0], kind, algorithm_version: algorithm,
        indicator_id: `${kind}(window=3)`, id: `${kind}(window=3):value` };
      render(<ResearchChart value={data} parameters={{ ...parameters, indicators: undefined }} reportRequestId={request} />);
      expect(kind === "ema" ? screen.getByRole("checkbox", { name: label }) : screen.getByRole("option", { name: label })).toBeInTheDocument();
    },
  );
  it("caps narrow-screen readings and formats large quantities without long digit strings", () => {
    let resize: ResizeObserverCallback = () => {};
    vi.stubGlobal("ResizeObserver", class {
      constructor(callback: ResizeObserverCallback) { resize = callback; }
      observe() {} disconnect() {}
    });
    const data = fixture(); data.bars[3].volume = 1e15;
    for (const window of [4, 5, 6, 7]) data.series.push({ ...data.series[0],
      indicator_id: `sma(window=${window})`, id: `sma(window=${window}):value`, parameters: { window } });
    render(<ResearchChart value={data} parameters={{ ...parameters, indicators: undefined }} reportRequestId={request} />);
    fireEvent.keyDown(plot(), { key: "End" });
    expect(readout().querySelectorAll("dl > div")).toHaveLength(9);
    expect(readout()).toHaveTextContent("SMA5"); expect(readout()).toHaveTextContent("另有 2 条线");
    act(() => resize([{ contentRect: { width: 300 } } as ResizeObserverEntry], {} as ResizeObserver));
    expect(readout()).toHaveTextContent("另有 3 条线");
    expect(readout().querySelectorAll("dl > div")).toHaveLength(8);
    expect(readout()).toHaveTextContent("SMA3"); expect(readout()).toHaveTextContent("SMA4");
    expect(readout()).toHaveTextContent("RSI2"); expect(readout()).not.toHaveTextContent("SMA5");
    expect(readout()).toHaveTextContent("1E15 股");
    act(() => resize([{ contentRect: { width: 960 } } as ResizeObserverEntry], {} as ResizeObserver));
    expect(readout()).toHaveTextContent("SMA5"); expect(readout()).toHaveTextContent("另有 2 条线");
    act(() => resize([{ contentRect: { width: 300 } } as ResizeObserverEntry], {} as ResizeObserver));
    fireEvent.change(screen.getByLabelText("独立指标"), { target: { value: "" } });
    expect(readout()).not.toHaveTextContent("RSI2"); expect(readout()).toHaveTextContent("另有 3 条线");
    for (const name of ["SMA5", "SMA6", "SMA7"]) fireEvent.click(screen.getByRole("checkbox", { name }));
    expect(readout()).not.toHaveTextContent("另有");
    expect(readout()).toHaveTextContent("SMA3"); expect(readout()).toHaveTextContent("SMA4");
    expect(readout().querySelectorAll("dl > div")).toHaveLength(7);
  });
  it("uses only returned trading days and renders a flat single candle safely", () => {
    const data = fixture(); data.bars = [data.bars[2]];
    data.series.forEach((series) => { series.values = [series.values[2]]; series.statuses = [series.statuses[2]]; });
    const { container } = render(<ResearchChart value={data} parameters={parameters} reportRequestId={request} />);
    expect(container.querySelectorAll(".chart-flat")).toHaveLength(1);
    expect(container.innerHTML).not.toMatch(/NaN|Infinity|2026-09-23/);
    expect(container.querySelectorAll(".chart-point")).toHaveLength(2);
    fireEvent.keyDown(plot(), { key: "ArrowLeft" }); expect(readout()).toHaveTextContent("2026-09-24");
  });
  it("clamps selection when reducing the window and retains that selection when expanding again", () => {
    const data = fixture(); const start = new Date("2026-07-01T00:00:00Z");
    data.bars = Array.from({ length: 90 }, (_, index) => ({ ...data.bars[0], date: new Date(start.valueOf() + index * 86400000).toISOString().slice(0, 10) }));
    data.start_date = data.bars[0].date; data.as_of_date = data.bars.at(-1)!.date;
    data.series.forEach((series) => { series.values = Array(90).fill(12); series.statuses = Array(90).fill("available"); });
    const { container } = render(<ResearchChart value={data} parameters={{ ...parameters, start_date: data.start_date, as_of_date: data.as_of_date }} reportRequestId={request} />);
    expect(container.querySelectorAll(".chart-volume")).toHaveLength(60);
    fireEvent.change(screen.getByLabelText("显示条数"), { target: { value: "366" } });
    fireEvent.keyDown(plot(), { key: "Home" }); expect(readout()).toHaveTextContent(data.start_date);
    fireEvent.change(screen.getByLabelText("显示条数"), { target: { value: "30" } });
    expect(container.querySelectorAll(".chart-volume")).toHaveLength(30);
    expect(readout()).toHaveTextContent(data.bars[60].date);
    fireEvent.change(screen.getByLabelText("显示条数"), { target: { value: "366" } });
    expect(readout()).toHaveTextContent(data.bars[60].date);
  });
  it("adapts the SVG coordinates on resize and disconnects its observer", () => {
    let resize: ResizeObserverCallback = () => {};
    const disconnect = vi.fn();
    vi.stubGlobal("ResizeObserver", class {
      constructor(callback: ResizeObserverCallback) { resize = callback; }
      observe() {} disconnect = disconnect;
    });
    const { container, unmount } = render(<ResearchChart value={fixture()} parameters={parameters} reportRequestId={request} />);
    act(() => resize([{ contentRect: { width: 300 } } as ResizeObserverEntry], {} as ResizeObserver));
    expect(screen.getByRole("img")).toHaveAttribute("viewBox", "0 0 300 529");
    expect(within(screen.getByRole("img")).getByText("09-21")).toBeInTheDocument();
    expect(within(screen.getByRole("img")).getByText("09-25")).toBeInTheDocument();
    expect(within(screen.getByRole("img")).queryByText("09-22")).not.toBeInTheDocument();
    expect(container.querySelector("[style]")).toBeNull();
    unmount(); expect(disconnect).toHaveBeenCalledOnce();
  });
});
