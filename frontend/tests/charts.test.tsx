import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ResearchChart } from "../src/ResearchChart";
import { chartScale, lineSegments, parseResearchChart, validChartDate, type ChartParameters, type ChartSeries, type ResearchChartData } from "../src/charts";

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
    const data = fixture();
    const specs: { kind: string; parameters: Record<string, number>; id: string; algorithm: string; fields: string[]; unit: ChartSeries["unit"] }[] = [
      { kind: "boll", parameters: { window: 3, stddev_multiplier: 2 }, id: "boll(window=3,stddev_multiplier=2.0)", algorithm: "boll-population-ddof0-v1", fields: ["middle", "upper", "lower"], unit: "CNY/share" },
      { kind: "macd", parameters: { fast: 2, slow: 3, signal: 2 }, id: "macd(fast=2,slow=3,signal=2)", algorithm: "macd-sma-seed-double-histogram-v1", fields: ["dif", "dea", "histogram"], unit: "CNY/share" },
      { kind: "volume", parameters: { window: 3 }, id: "volume(window=3)", algorithm: "volume-prior-records-ratio-v1", fields: ["volume_shares", "volume_ma", "previous_volume_ma", "relative_volume"], unit: "shares" },
    ];
    data.series = specs.flatMap((spec) => spec.fields.map((field) => ({ id: `${spec.id}:${field}`, indicator_id: spec.id, kind: spec.kind,
      parameters: spec.parameters, algorithm_version: spec.algorithm, field, label: field, unit: field === "relative_volume" ? "multiple" : spec.unit,
      start_effect_warning: false, values: [1, 2, 3, 4], statuses: ["available", "available", "available", "available"] } as ChartSeries)));
    expect(parseResearchChart(data, { ...parameters, indicators: undefined }, request)).not.toBeNull();
    data.series.pop(); expect(parseResearchChart(data, { ...parameters, indicators: undefined }, request)).toBeNull();
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

function openChart() {
  const details = screen.getByText("历史 K 线与指标").closest("details")!;
  details.open = true; fireEvent(details, new Event("toggle"));
}
describe("research chart component", () => {
  it("omits an absent chart and degrades invalid charts without throwing", () => {
    const { rerender, container } = render(<ResearchChart value={null} parameters={parameters} reportRequestId={request} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<ResearchChart value={{ status: "unavailable" }} parameters={parameters} reportRequestId={request} />);
    expect(screen.getByRole("note")).toHaveTextContent("不影响上方已确认的研究答案与证据");
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });
  it("states provenance immediately and defers SVG rendering until expanded", () => {
    render(<ResearchChart value={fixture()} parameters={parameters} reportRequestId={request} />);
    expect(screen.getByText("人工样例 · 非真实行情")).toBeInTheDocument();
    expect(screen.getByText(/复权：未知/)).toBeInTheDocument();
    expect(screen.getByText(/请求范围：2026-09-21 — 2026-09-25/)).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument(); openChart();
    expect(screen.getByRole("img", { name: /历史日 K 线/ })).toBeInTheDocument();
    expect(screen.getByText(/红涨 \/ 绿跌 \/ 灰平/)).toBeInTheDocument();
  });
  it("marks saved context as historical rather than a fresh analysis", () => {
    const data = fixture(); data.context = "saved"; data.saved_at = "2026-09-25T12:30:00Z";
    render(<ResearchChart value={data} parameters={parameters} reportRequestId={request} />);
    expect(screen.getByText(/已保存快照，非新分析/)).toBeInTheDocument();
    expect(screen.getByText(/保存时间：2026-09-25T12:30:00Z/)).toBeInTheDocument();
  });
  it("exposes selected values and missing statuses through a native keyboard-accessible range", () => {
    render(<ResearchChart value={fixture()} parameters={parameters} reportRequestId={request} />); openChart();
    const slider = screen.getByRole("slider"); slider.focus(); expect(slider).toHaveFocus();
    expect(slider).toHaveAttribute("step", "1"); expect(slider).toHaveAttribute("aria-valuetext", "2026-09-25");
    fireEvent.change(slider, { target: { value: "0" } });
    expect(slider).toHaveAttribute("aria-valuetext", "2026-09-21");
    expect(screen.getAllByText("历史不足")).toHaveLength(2);
    expect(screen.getAllByText("—")).toHaveLength(2);
    expect(screen.getByLabelText("选定交易日行情")).toHaveTextContent("成交量100 股");
    fireEvent.change(slider, { target: { value: "2" } });
    expect(slider).toHaveAttribute("aria-valuetext", "2026-09-24");
    expect(screen.getByLabelText("选定交易日行情")).toHaveTextContent("成交量0 股");
  });
  it("toggles price overlays and independent units without recomputing or network activity", () => {
    const { container } = render(<ResearchChart value={fixture()} parameters={parameters} reportRequestId={request} />); openChart();
    expect(container.querySelectorAll(".chart-line")).toHaveLength(2);
    fireEvent.click(screen.getByRole("checkbox", { name: "sma(window=3)" }));
    expect(container.querySelectorAll(".chart-line")).toHaveLength(1);
    fireEvent.change(screen.getByLabelText("独立指标"), { target: { value: "" } });
    expect(container.querySelectorAll(".chart-line")).toHaveLength(0);
    expect(container.querySelector("[style]")).toBeNull();
  });
  it("uses only returned trading days and renders a flat single candle safely", () => {
    const data = fixture(); data.bars = [data.bars[2]]; data.series.forEach((series) => { series.values = [series.values[2]]; series.statuses = [series.statuses[2]]; });
    const { container } = render(<ResearchChart value={data} parameters={parameters} reportRequestId={request} />); openChart();
    expect(container.querySelectorAll(".chart-flat")).toHaveLength(1);
    expect(container.innerHTML).not.toMatch(/NaN|Infinity|2026-09-23/);
    expect(container.querySelectorAll(".chart-point")).toHaveLength(2);
  });
  it("limits visible rows and clamps the native date selection to the new window", () => {
    const data = fixture(); const start = new Date("2026-07-01T00:00:00Z");
    data.bars = Array.from({ length: 90 }, (_, index) => ({ ...data.bars[0], date: new Date(start.valueOf() + index * 86400000).toISOString().slice(0, 10) }));
    data.start_date = data.bars[0].date; data.as_of_date = data.bars.at(-1)!.date;
    data.series.forEach((series) => { series.values = Array(90).fill(12); series.statuses = Array(90).fill("available"); });
    render(<ResearchChart value={data} parameters={{ ...parameters, start_date: data.start_date, as_of_date: data.as_of_date }} reportRequestId={request} />); openChart();
    expect(screen.getByText(/60 条。仅使用返回交易日/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("显示日线"), { target: { value: "366" } });
    fireEvent.change(screen.getByRole("slider"), { target: { value: "0" } });
    fireEvent.change(screen.getByLabelText("显示日线"), { target: { value: "30" } });
    expect(screen.getByRole("slider")).toHaveAttribute("min", "60");
    expect(screen.getByRole("slider")).toHaveValue("60");
  });
});
