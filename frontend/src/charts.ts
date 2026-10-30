/** Bounded, request-bound browser validation. Historical points are not evidence IDs. */
export type ChartParameters = { instrument_id: string; start_date: string; as_of_date: string; source?: string; indicators?: unknown };
export type ChartStatus = "available" | "insufficient_history" | "undefined" | "calculation_error";
export type ChartUnit = "CNY/share" | "index_points" | "shares" | "multiple";
export interface ChartBar { date: string; open: number; high: number; low: number; close: number; volume: number }
export interface ChartSeries {
  id: string; indicator_id: string; kind: string; parameters: Record<string, number>; algorithm_version: string;
  field: string; label: string; unit: ChartUnit; start_effect_warning: boolean; values: (number | null)[]; statuses: ChartStatus[];
}
export interface ResearchChartData {
  version: "research-chart-v1"; status: "available"; code: "aligned_research_series";
  validation_scope: "request_bound_aligned_series_not_full_historical_evidence";
  request_id: string; instrument_id: string; start_date: string; as_of_date: string;
  source: "csv" | "tencent" | "akshare_eastmoney"; price_adjustment: "unknown" | "unadjusted";
  data_authenticity: "synthetic" | "unverified_csv" | "provider_reported";
  retrieved_at: string | null; context: "current" | "saved"; saved_at: string | null;
  bars: ChartBar[]; series: ChartSeries[];
}

export type ChartInterval = "1w" | "60m" | "120m";
export interface IntervalChartData extends Omit<ResearchChartData, "version" | "code" | "validation_scope" | "bars"> {
  version: "research-chart-interval-v1"; code: "chart_only_series"; interval: ChartInterval; timezone: "Asia/Shanghai";
  coverage: { start_at: string; end_at: string; bar_count: number };
  bars: (ChartBar & { start_at: string; end_at: string; complete: boolean })[];
}
export type DisplayChartData = ResearchChartData | IntervalChartData;
export type ChartCapabilities = Record<ChartInterval, { enabled: boolean; code: string }>;

export function parseChartCapabilities(value: unknown, daily: ResearchChartData): ChartCapabilities | null {
  if (!object(value) || value.version !== "research-chart-capabilities-v1" || value.research_id !== daily.request_id
    || !object(value.intervals) || Object.keys(value.intervals).length !== 3) return null;
  for (const interval of ["1w", "60m", "120m"]) {
    const item = value.intervals[interval];
    if (!object(item) || typeof item.enabled !== "boolean" || typeof item.code !== "string"
      || !["ready", "chart_hourly_source_unsupported", "chart_market_unsupported"].includes(item.code)
      || item.enabled !== (item.code === "ready") || interval === "1w" && !item.enabled
      || interval !== "1w" && item.enabled && daily.source !== "tencent") return null;
  }
  return value.intervals as ChartCapabilities;
}

export function parseIntervalChart(value: unknown, daily: ResearchChartData, interval: ChartInterval): IntervalChartData | null {
  if (!object(value) || value.version !== "research-chart-interval-v1" || value.status !== "available"
    || value.code !== "chart_only_series" || value.interval !== interval || value.timezone !== "Asia/Shanghai"
    || !["request_id", "instrument_id", "start_date", "as_of_date", "source", "price_adjustment", "data_authenticity"]
      .every((key) => value[key] === daily[key as keyof ResearchChartData])
    || !["current", "saved"].includes(String(value.context)) || !timestamp(value.saved_at)
    || (value.context === "current" ? value.saved_at !== null : value.saved_at === null)
    || value.retrieved_at === null || !timestamp(value.retrieved_at)
    || interval !== "1w" && (value.source !== "tencent" || value.data_authenticity !== "provider_reported")
    || !Array.isArray(value.bars) || !value.bars.length || value.bars.length > (interval === "1w" ? 53 : 320)
    || !Array.isArray(value.series) || !value.series.length || value.series.length > 48
    || !object(value.coverage) || value.coverage.bar_count !== value.bars.length) return null;
  let previous = "";
  for (const bar of value.bars) {
    // Adjacent hourly periods may share a boundary; overlapping periods are invalid.
    if (!object(bar) || typeof bar.start_at !== "string" || typeof bar.end_at !== "string"
      || !timestamp(bar.start_at) || !timestamp(bar.end_at)
      || !bar.start_at.endsWith("+08:00") || !bar.end_at.endsWith("+08:00")
      || bar.start_at < previous || bar.start_at > bar.end_at || typeof bar.complete !== "boolean"
      || bar.start_at.slice(0, 10) < daily.start_date || bar.end_at.slice(0, 10) > daily.as_of_date
      || ![bar.open, bar.high, bar.low, bar.close].every((item) => finite(item) && item > 0)
      || !finite(bar.volume) || !Number.isSafeInteger(bar.volume) || bar.volume < 0
      || Number(bar.low) > Math.min(Number(bar.open), Number(bar.close))
      || Number(bar.high) < Math.max(Number(bar.open), Number(bar.close))) return null;
    const day = bar.end_at.slice(0, 10);
    const start = bar.start_at.slice(11, 19), end = bar.end_at.slice(11, 19);
    if (interval === "1w") {
      const date = new Date(`${day}T00:00:00Z`), weekday = (date.getUTCDay() + 6) % 7;
      const monday = new Date(date.valueOf() - weekday * 86400000).toISOString().slice(0, 10);
      const sunday = new Date(date.valueOf() + (6 - weekday) * 86400000).toISOString().slice(0, 10);
      if (bar.date !== day || monday < daily.start_date || bar.start_at.slice(0, 10) < monday
        || bar.complete !== (sunday <= daily.as_of_date) || start !== "09:30:00" || end !== "15:00:00") return null;
    } else {
      const starts: Record<string, string> = interval === "60m"
        ? { "10:30:00": "09:30:00", "11:30:00": "10:30:00", "14:00:00": "13:00:00", "15:00:00": "14:00:00" }
        : { "11:30:00": "09:30:00", "15:00:00": "13:00:00" };
      if (bar.date !== bar.end_at || bar.start_at.slice(0, 10) !== day || starts[end] !== start || !bar.complete
        || [0, 6].includes(new Date(`${day}T00:00:00Z`).getUTCDay())
        || Date.parse(bar.end_at) > Date.parse(value.retrieved_at as string)) return null;
    }
    previous = bar.end_at;
  }
  const bars = value.bars as IntervalChartData["bars"];
  if (value.coverage.start_at !== bars[0].start_at || value.coverage.end_at !== bars.at(-1)!.end_at) return null;
  const expected = [...new Map(daily.series.map((s) => [s.indicator_id, { kind: s.kind, ...s.parameters }])).values()];
  if (!validateSeries(value as unknown as IntervalChartData, { ...daily, indicators: expected })) return null;
  try { if (new TextEncoder().encode(JSON.stringify(value)).length > 256 * 1024) return null; } catch { return null; }
  return value as unknown as IntervalChartData;
}

const object = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value) && Math.abs(value) <= 1e15;
const identifier = (value: unknown): value is string => typeof value === "string" && /^[A-Za-z0-9_.-]{1,100}$/.test(value);
const algorithms: Record<string, string> = {
  sma: "sma-close-v1", ema: "ema-sma-seed-v1", macd: "macd-sma-seed-double-histogram-v1",
  rsi: "rsi-wilder-flat50-v1", boll: "boll-population-ddof0-v1", atr: "atr-wilder-prior-close-v1", volume: "volume-prior-records-ratio-v1",
  adx: "adx-wilder-double-smoothed-v1", obv: "obv-snapshot-zero-seed-v1", vwma: "vwma-close-volume-weighted-v1",
};
const definitions: Record<string, Record<string, ChartUnit>> = {
  sma: { value: "CNY/share" }, ema: { value: "CNY/share" }, macd: { dif: "CNY/share", dea: "CNY/share", histogram: "CNY/share" },
  rsi: { value: "index_points" }, boll: { middle: "CNY/share", upper: "CNY/share", lower: "CNY/share" },
  atr: { value: "CNY/share" }, volume: { volume_shares: "shares", volume_ma: "shares", previous_volume_ma: "shares", relative_volume: "multiple" },
  adx: { value: "index_points", plus_di: "index_points", minus_di: "index_points" },
  obv: { value: "shares" }, vwma: { value: "CNY/share" },
};

/** Date.UTC alone silently normalizes impossible calendar dates. Compare every part. */
export function validChartDate(value: unknown): value is string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  if (year < 1) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(date.valueOf()) && date.getUTCFullYear() === year && date.getUTCMonth() + 1 === month && date.getUTCDate() === day;
}

function timestamp(value: unknown): value is string | null {
  if (value === null) return true;
  if (typeof value !== "string" || value.length > 40) return false;
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  return !!match && validChartDate(match[1]) && Number(match[2]) < 24 && Number(match[3]) < 60 && Number(match[4]) < 60
    && (match[5] === "Z" || Number(match[5].slice(1, 3)) < 24 && Number(match[5].slice(4)) < 60)
    && Number.isFinite(Date.parse(value));
}

function specification(value: Record<string, unknown>): value is Record<string, unknown> & ChartSeries {
  const kind = value.kind;
  if (typeof kind !== "string" || !Object.hasOwn(algorithms, kind) || value.algorithm_version !== algorithms[kind]
    || !object(value.parameters) || typeof value.indicator_id !== "string" || value.indicator_id.length > 180) return false;
  const names = kind === "obv" ? [] : kind === "macd" ? ["fast", "slow", "signal"] : kind === "boll" ? ["window", "stddev_multiplier"] : ["window"];
  const params = value.parameters;
  if (Object.keys(params).length !== names.length || !names.every((name) => Object.hasOwn(params, name))) return false;
  if (!names.every((name) => {
    const item = params[name];
    if (name === "stddev_multiplier") return finite(item) && item > 0 && item <= 4;
    return finite(item) && Number.isInteger(item) && item >= (["sma", "ema", "volume", "vwma"].includes(kind) ? 1 : 2)
      && item <= (name === "signal" ? 60 : 120);
  }) || kind === "macd" && Number(params.fast) >= Number(params.slow)) return false;
  const prefix = `${kind}(`;
  if (!value.indicator_id.startsWith(prefix) || !value.indicator_id.endsWith(")")) return false;
  const contents = value.indicator_id.slice(prefix.length, -1);
  const tokens = contents ? contents.split(",") : [];
  if (tokens.length !== names.length || !tokens.every((token, index) => {
    const [name, text, extra] = token.split("=");
    return name === names[index] && extra === undefined && /^(?:0|[1-9]\d*)(?:\.\d+)?(?:e[+-]?\d+)?$/.test(text ?? "")
      && Number(text) === params[name] && (name === "stddev_multiplier" || text === String(params[name]));
  })) return false;
  return typeof value.field === "string" && Object.hasOwn(definitions[kind], value.field) && value.unit === definitions[kind][value.field]
    && value.id === `${value.indicator_id}:${value.field}` && typeof value.label === "string" && value.label.length > 0 && value.label.length <= 200
    && typeof value.start_effect_warning === "boolean";
}

/** Invalid charts fail locally; they must never invalidate the confirmed answer. */
export function parseResearchChart(value: unknown, parameters: ChartParameters | null, reportRequestId: string | undefined): ResearchChartData | null {
  if (!object(value) || !parameters || !identifier(reportRequestId) || value.request_id !== reportRequestId
    || value.version !== "research-chart-v1" || value.status !== "available" || value.code !== "aligned_research_series"
    || value.validation_scope !== "request_bound_aligned_series_not_full_historical_evidence"
    || typeof value.instrument_id !== "string" || !/^\d{6}\.(SH|SZ|BJ)$/.test(value.instrument_id)
    || value.instrument_id !== parameters.instrument_id || value.start_date !== parameters.start_date || value.as_of_date !== parameters.as_of_date
    || !validChartDate(value.start_date) || !validChartDate(value.as_of_date) || value.start_date > value.as_of_date
    || Date.parse(value.as_of_date) - Date.parse(value.start_date) > 365 * 86400000
    || typeof value.source !== "string" || !["csv", "tencent", "akshare_eastmoney"].includes(value.source)
    || parameters.source !== undefined && value.source !== parameters.source
    || !timestamp(value.retrieved_at) || !timestamp(value.saved_at) || typeof value.context !== "string" || !["current", "saved"].includes(value.context)
    || value.context === "saved" && value.saved_at === null || value.context === "current" && value.saved_at !== null) return null;
  if (value.source === "csv" ? value.price_adjustment !== "unknown" || value.data_authenticity !== "synthetic" && value.data_authenticity !== "unverified_csv"
    : value.price_adjustment !== "unadjusted" || value.data_authenticity !== "provider_reported") return null;
  if (!Array.isArray(value.bars) || value.bars.length < 1 || value.bars.length > 366 || !Array.isArray(value.series) || value.series.length < 1 || value.series.length > 48) return null;
  let previous = "";
  for (const bar of value.bars) {
    if (!object(bar) || !validChartDate(bar.date) || bar.date <= previous || bar.date < value.start_date || bar.date > value.as_of_date
      || ![bar.open, bar.high, bar.low, bar.close].every((item) => finite(item) && item > 0)
      || !finite(bar.volume) || !Number.isInteger(bar.volume) || bar.volume < 0
      || Number(bar.low) > Math.min(Number(bar.open), Number(bar.close)) || Number(bar.high) < Math.max(Number(bar.open), Number(bar.close))) return null;
    previous = bar.date;
  }
  if (!validateSeries(value as unknown as ResearchChartData, parameters)) return null;
  return value as unknown as ResearchChartData;
}

export interface ChartScale { min: number; max: number; project: (value: number) => number }
/** A flat/single-point series gets explicit padding rather than a zero-width axis. */
export function chartScale(values: readonly number[], top: number, bottom: number, includeZero = false): ChartScale {
  let min = Math.min(...values), max = Math.max(...values);
  if (!values.length) { min = 0; max = 1; }
  if (includeZero) { min = Math.min(0, min); max = Math.max(0, max); }
  const padding = max === min ? Math.max(Math.abs(max) * .04, .01) : (max - min) * .08;
  min -= padding; max += padding;
  return { min, max, project: (value: number) => bottom - (value - min) / (max - min) * (bottom - top) };
}

/** Return separate polylines and isolated points; never bridge missing values. */
export function lineSegments(values: readonly (number | null)[], x: (index: number) => number, y: (value: number) => number): string[] {
  const result: string[] = []; let current: string[] = [];
  const flush = () => { if (current.length) result.push(current.join(" ")); current = []; };
  values.forEach((value, index) => { if (value === null) flush(); else current.push(`${x(index)},${y(value)}`); });
  flush(); return result;
}

function validateSeries(value: Pick<ResearchChartData, "bars" | "series">, parameters: ChartParameters): boolean {
  const ids = new Set<string>();
  const instances = new Map<string, string>();
  const fields = new Map<string, Set<string>>();
  for (const series of value.series) {
    if (!object(series) || !specification(series) || ids.has(series.id) || !Array.isArray(series.values) || !Array.isArray(series.statuses)
      || series.values.length !== value.bars.length || series.statuses.length !== value.bars.length) return false;
    ids.add(series.id);
    const spec = `${series.kind}:${JSON.stringify(series.parameters, Object.keys(series.parameters).sort())}:${series.algorithm_version}`;
    if (instances.has(series.indicator_id) && instances.get(series.indicator_id) !== spec) return false;
    instances.set(series.indicator_id, spec);
    if (!fields.has(series.indicator_id)) fields.set(series.indicator_id, new Set());
    fields.get(series.indicator_id)!.add(series.field);
    if (instances.size > 12) return false;
    for (let index = 0; index < series.values.length; index++) {
      const number = series.values[index]; const state = series.statuses[index];
      if (!["available", "insufficient_history", "undefined", "calculation_error"].includes(state)
        || (state === "available" ? !finite(number) : number !== null)) return false;
      if (number !== null && ((series.kind === "rsi" || series.kind === "adx") && (number < 0 || number > 100)
        || (series.kind === "atr" || series.kind === "volume") && number < 0
        || series.kind === "vwma" && number <= 0
        || series.kind === "obv" && !Number.isSafeInteger(number)
        || series.field === "volume_shares" && !Number.isInteger(number))) return false;
      if (["adx", "obv"].includes(series.kind) && state === "undefined") return false;
    }
  }
  for (const series of value.series as ChartSeries[]) {
    if (fields.get(series.indicator_id)!.size !== Object.keys(definitions[series.kind]).length) return false;
  }
  if (new Set(instances.values()).size !== instances.size) return false;
  if (parameters.indicators !== undefined) {
    if (!Array.isArray(parameters.indicators) || parameters.indicators.length < 1 || parameters.indicators.length > 12) return false;
    const expected = new Set<string>();
    for (const item of parameters.indicators) {
      if (!object(item) || typeof item.kind !== "string" || !Object.hasOwn(algorithms, item.kind)) return false;
      const params = Object.fromEntries(Object.entries(item).filter(([name]) => name !== "kind"));
      expected.add(`${item.kind}:${JSON.stringify(params, Object.keys(params).sort())}:${algorithms[item.kind]}`);
    }
    if (expected.size !== parameters.indicators.length || expected.size !== instances.size
      || [...instances.values()].some((spec) => !expected.has(spec))) return false;
  }
  return true;
}
