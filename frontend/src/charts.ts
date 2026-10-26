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
  const ids = new Set<string>();
  const instances = new Map<string, string>();
  const fields = new Map<string, Set<string>>();
  for (const series of value.series) {
    if (!object(series) || !specification(series) || ids.has(series.id) || !Array.isArray(series.values) || !Array.isArray(series.statuses)
      || series.values.length !== value.bars.length || series.statuses.length !== value.bars.length) return null;
    ids.add(series.id);
    const spec = `${series.kind}:${JSON.stringify(series.parameters, Object.keys(series.parameters).sort())}:${series.algorithm_version}`;
    if (instances.has(series.indicator_id) && instances.get(series.indicator_id) !== spec) return null;
    instances.set(series.indicator_id, spec);
    if (!fields.has(series.indicator_id)) fields.set(series.indicator_id, new Set());
    fields.get(series.indicator_id)!.add(series.field);
    if (instances.size > 12) return null;
    for (let index = 0; index < series.values.length; index++) {
      const number = series.values[index]; const state = series.statuses[index];
      if (!["available", "insufficient_history", "undefined", "calculation_error"].includes(state)
        || (state === "available" ? !finite(number) : number !== null)) return null;
      if (number !== null && ((series.kind === "rsi" || series.kind === "adx") && (number < 0 || number > 100)
        || (series.kind === "atr" || series.kind === "volume") && number < 0
        || series.kind === "vwma" && number <= 0
        || series.kind === "obv" && !Number.isSafeInteger(number)
        || series.field === "volume_shares" && !Number.isInteger(number))) return null;
      if (["adx", "obv"].includes(series.kind) && state === "undefined") return null;
    }
  }
  for (const series of value.series as ChartSeries[]) {
    if (fields.get(series.indicator_id)!.size !== Object.keys(definitions[series.kind]).length) return null;
  }
  if (new Set(instances.values()).size !== instances.size) return null;
  if (parameters.indicators !== undefined) {
    if (!Array.isArray(parameters.indicators) || parameters.indicators.length < 1 || parameters.indicators.length > 12) return null;
    const expected = new Set<string>();
    for (const item of parameters.indicators) {
      if (!object(item) || typeof item.kind !== "string" || !Object.hasOwn(algorithms, item.kind)) return null;
      const params = Object.fromEntries(Object.entries(item).filter(([name]) => name !== "kind"));
      expected.add(`${item.kind}:${JSON.stringify(params, Object.keys(params).sort())}:${algorithms[item.kind]}`);
    }
    if (expected.size !== parameters.indicators.length || expected.size !== instances.size
      || [...instances.values()].some((spec) => !expected.has(spec))) return null;
  }
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
