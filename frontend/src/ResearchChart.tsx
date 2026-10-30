import { useEffect, useId, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";
import { chartScale, lineSegments, parseResearchChart, parseChartCapabilities, parseIntervalChart,
  type ChartParameters, type ChartSeries, type ResearchChartData, type DisplayChartData, type ChartInterval } from "./charts";
import "./charts.css";

const unitNames = { "CNY/share": "元/股", shares: "股", index_points: "点", multiple: "倍" };
const statusNames = { available: "可用", insufficient_history: "历史不足", undefined: "未定义", calculation_error: "计算失败" };
const format = (value: number) => new Intl.NumberFormat("zh-CN", { maximumSignificantDigits: 7 }).format(value);
const priceKind = (series: ChartSeries) => ["sma", "ema", "boll", "vwma"].includes(series.kind);
const tone = (open: number, close: number) => close > open ? "chart-rise" : close < open ? "chart-fall" : "chart-flat";
const readoutNumber = (value: number) => new Intl.NumberFormat("zh-CN", {
  maximumSignificantDigits: 7, notation: Math.abs(value) >= 1e7 || value !== 0 && Math.abs(value) < .0001 ? "scientific" : "standard",
}).format(value);
const instanceName = (series: ChartSeries) => series.kind === "obv" ? "OBV"
  : series.kind === "boll" ? `BOLL(${series.parameters.window},${series.parameters.stddev_multiplier})`
  : series.kind === "macd" ? `MACD(${series.parameters.fast},${series.parameters.slow},${series.parameters.signal})`
  : `${series.kind.toUpperCase()}${series.parameters.window}`;
const fieldNames: Record<string, string> = { middle: "中轨", upper: "上轨", lower: "下轨", dif: "DIF", dea: "DEA", histogram: "柱",
  volume_shares: "成交量", volume_ma: "成交量均量", previous_volume_ma: "此前均量", relative_volume: "量比" };

function seriesName(series: ChartSeries, multiple: boolean) {
  if (series.kind === "boll") return `${multiple ? instanceName(series) : "BOLL"} ${fieldNames[series.field]}`;
  if (series.kind === "macd") return `${multiple ? instanceName(series) : "MACD"} ${fieldNames[series.field]}`;
  if (series.kind === "volume") return `${fieldNames[series.field]}(${series.parameters.window})`;
  if (series.kind === "adx" && series.field !== "value") return `${series.field === "plus_di" ? "+DI" : "−DI"}${series.parameters.window}`;
  return instanceName(series);
}

function SeriesLine({ values, x, y, className }: { values: readonly (number | null)[]; x: (index: number) => number; y: (value: number) => number; className: string }) {
  return <g className={className}>{lineSegments(values, x, y).map((points, index) => points.includes(" ")
    ? <polyline key={index} points={points} className="chart-line" />
    : <circle key={index} cx={Number(points.split(",")[0])} cy={Number(points.split(",")[1])} r="2.4" className="chart-point" />)}</g>;
}

const intervalNames = { "1d": "日线", "1w": "周线", "60m": "1小时", "120m": "2小时" };
const dateLabel = (date: string, narrow = false) => date.length > 10
  ? `${date.slice(narrow ? 5 : 0, 10)} ${date.slice(11, 16)}` : narrow ? date.slice(5) : date;

function ChartWorkspace({ data, intervalControls }: { data: DisplayChartData; intervalControls: ReactNode }) {
  const id = useId();
  const plot = useRef<HTMLDivElement>(null);
  const activePointer = useRef<number | null>(null);
  const [width, setWidth] = useState(960);
  const [cursorY, setCursorY] = useState<number | null>(null);
  const [pointerVisible, setPointerVisible] = useState(false);
  const [keyboardFocused, setKeyboardFocused] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const [windowSize, setWindowSize] = useState(60);
  const [selectedIndex, setSelectedIndex] = useState(data.bars.length - 1);
  const groups = [...new Map(data.series.filter(priceKind).map((series) => [series.indicator_id, series])).values()];
  const [hidden, setHidden] = useState<string[]>([]);
  const independent = data.series.filter((series) => !priceKind(series));
  const [fieldId, setFieldId] = useState(independent[0]?.id ?? "");
  const offset = Math.max(0, data.bars.length - windowSize);
  const bars = data.bars.slice(offset);
  const selected = Math.max(offset, Math.min(data.bars.length - 1, selectedIndex));
  const bar = data.bars[selected];
  const overlay = data.series.filter((series) => priceKind(series) && !hidden.includes(series.indicator_id));
  const field = independent.find((series) => series.id === fieldId);
  const name = (series: ChartSeries) => seriesName(series,
    new Set(data.series.filter((item) => item.kind === series.kind).map((item) => item.indicator_id)).size > 1);
  const inspectedSeries = [...overlay, ...(field ? [field] : [])];
  const shownSeries = [...overlay.slice(0, width < 560 ? 2 : 3), ...(field ? [field] : [])];
  const omittedLines = inspectedSeries.length - shownSeries.length;
  const visible = pointerVisible || keyboardFocused;
  const fieldValues = field?.values.slice(offset) ?? [];
  const left = 64, right = width - 16;
  const x = (index: number) => left + (index + .5) / bars.length * (right - left);
  const bodyWidth = Math.max(.7, Math.min(12, (right - left) / bars.length * .65));
  const price = chartScale([...bars.flatMap((item) => [item.high, item.low]), ...overlay.flatMap((series) => series.values.slice(offset).filter((item): item is number => item !== null))], 28, 230);
  const volumeMax = Math.max(1, ...bars.map((item) => item.volume));
  const volumeY = (value: number) => 332 - value / volumeMax * 65;
  const indicator = chartScale(fieldValues.filter((value): value is number => value !== null), 397, 487, true);
  const dates = [...new Set(width < 560 ? [0, bars.length - 1] : [0, Math.floor((bars.length - 1) / 2), bars.length - 1])];
  const toggle = (indicatorId: string) => setHidden((previous) => previous.includes(indicatorId) ? previous.filter((item) => item !== indicatorId) : [...previous, indicatorId]);
  const height = field ? 529 : 370;
  const crossY = Math.max(28, Math.min(field ? 487 : 332, cursorY ?? price.project(bar.close)));
  useEffect(() => {
    const element = plot.current;
    if (!element) return;
    const resize = (value: number) => { if (value > 0) setWidth(Math.max(240, Math.min(960, Math.floor(value)))); };
    resize(element.getBoundingClientRect().width);
    if (typeof ResizeObserver === "undefined") {
      const update = () => resize(element.getBoundingClientRect().width);
      window.addEventListener("resize", update);
      return () => window.removeEventListener("resize", update);
    }
    const observer = new ResizeObserver(([entry]) => { if (entry) resize(entry.contentRect.width); });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const announce = (index: number) => setAnnouncement(`${data.bars[index].date}，收盘 ${format(data.bars[index].close)} 元/股`);
  const moveKey = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    setKeyboardFocused(true);
    const next = event.key === "Home" ? offset : event.key === "End" ? data.bars.length - 1
      : Math.max(offset, Math.min(data.bars.length - 1, selected + (event.key === "ArrowLeft" ? -1 : 1)));
    setSelectedIndex(next); setCursorY(null); announce(next);
  };
  const selectPointer = (event: PointerEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const px = (event.clientX - rect.left) / rect.width * width;
    const index = Math.max(0, Math.min(bars.length - 1, Math.floor((px - left) / (right - left) * bars.length)));
    setSelectedIndex(offset + index);
    setCursorY((event.clientY - rect.top) / rect.height * height);
    setPointerVisible(true);
  };
  const finishPointer = (event: PointerEvent<SVGSVGElement>) => {
    if (activePointer.current !== event.pointerId) return;
    activePointer.current = null;
    if (event.pointerType === "touch" || event.type === "pointercancel") setPointerVisible(false);
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };
  const valueText = (series: ChartSeries) => series.values[selected] === null ? "—" : `${readoutNumber(series.values[selected]!)} ${unitNames[series.unit]}`;

  return <div className="chart-workspace">
    <div className="chart-controls">
      <label htmlFor={`${id}-window`}>显示条数 <select id={`${id}-window`} value={windowSize}
        onChange={(event) => {
          const size = Number(event.target.value), next = Math.max(data.bars.length - size, selected);
          setWindowSize(size); setSelectedIndex(next); setCursorY(null); announce(next);
        }}>
        <option value={30}>最近 30 条</option><option value={60}>最近 60 条</option><option value={120}>最近 120 条</option><option value={366}>全部</option>
      </select></label>
      <label htmlFor={`${id}-field`}>独立指标 <select id={`${id}-field`} value={field?.id ?? ""} onChange={(event) => setFieldId(event.target.value)}>
        <option value="">不显示</option>{independent.map((series) => <option value={series.id} key={series.id}>{name(series)}</option>)}
      </select></label>
    </div>
    <div className="chart-choices">{intervalControls}
    {!!groups.length && <fieldset className="chart-overlays"><legend>价格叠加 · 元/股</legend>{groups.map((group) => <label key={group.indicator_id}>
      <input type="checkbox" checked={!hidden.includes(group.indicator_id)} onChange={() => toggle(group.indicator_id)} />{instanceName(group)}
    </label>)}</fieldset>}</div>
    <div className="chart-plot" ref={plot} tabIndex={0} role="group" aria-label={`互动${intervalNames[data.version === "research-chart-v1" ? "1d" : data.interval]}图`} aria-describedby={`${id}-keys`}
      onKeyDown={moveKey} onFocus={() => {
        if (activePointer.current === null) { setKeyboardFocused(true); announce(selected); }
      }} onBlur={() => setKeyboardFocused(false)}>
      <span id={`${id}-keys`} className="sr-only">鼠标移动或触屏拖动查看读数；左右键逐条移动，Home和End跳到区间两端。</span>
      <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height} role="img" aria-labelledby={`${id}-title ${id}-desc`}
        onPointerEnter={(event) => {
          if (event.pointerType !== "touch" && event.isPrimary !== false
            && (activePointer.current === null || activePointer.current === event.pointerId)) selectPointer(event);
        }}
        onPointerDown={(event) => {
          if (event.isPrimary === false || event.button !== 0 || activePointer.current !== null) return;
          activePointer.current = event.pointerId;
          setKeyboardFocused(false);
          event.currentTarget.setPointerCapture?.(event.pointerId);
          plot.current?.focus({ preventScroll: true });
          selectPointer(event);
        }}
        onPointerMove={(event) => {
          if (event.isPrimary === false || activePointer.current !== null && activePointer.current !== event.pointerId) return;
          if (event.pointerType !== "touch" || activePointer.current === event.pointerId) selectPointer(event);
        }}
        onPointerLeave={(event) => {
          if (event.isPrimary === false || activePointer.current !== null && activePointer.current !== event.pointerId) return;
          setPointerVisible(false);
        }}
        onPointerUp={finishPointer} onPointerCancel={finishPointer}
        onLostPointerCapture={(event) => {
          if (activePointer.current === event.pointerId) { activePointer.current = null; setPointerVisible(false); }
        }}>
        <title id={`${id}-title`}>{data.instrument_id} 历史{data.version === "research-chart-v1" ? "日" : intervalNames[data.interval]} K 线、成交量{field ? "及独立指标" : ""}</title>
        <desc id={`${id}-desc`}>红涨、绿跌、灰平，以收盘对开盘比较。价格、成交量和副图分轴；缺项断线。十字线选择交易日，浮层显示读数。</desc>
        {[price.max, (price.min + price.max) / 2, price.min].map((value, index) => <g key={index}>
          <line x1={left} x2={right} y1={price.project(value)} y2={price.project(value)} className="chart-grid" />
          <text x={left - 8} y={price.project(value) + 4} textAnchor="end" className="chart-tick">{format(value)}</text>
        </g>)}
        <text x={left} y="16" className="chart-axis-label">价格 · 元/股</text>
        {bars.map((item, index) => <g key={item.date} className={tone(item.open, item.close)}>
          <line x1={x(index)} x2={x(index)} y1={price.project(item.high)} y2={price.project(item.low)} className="chart-wick" />
          {item.open === item.close ? <line x1={x(index) - bodyWidth / 2} x2={x(index) + bodyWidth / 2} y1={price.project(item.close)} y2={price.project(item.close)} className="chart-wick" />
            : <rect x={x(index) - bodyWidth / 2} y={Math.min(price.project(item.open), price.project(item.close))}
              width={bodyWidth} height={Math.max(.7, Math.abs(price.project(item.open) - price.project(item.close)))} className="chart-candle" />}
          <rect x={x(index) - bodyWidth / 2} y={volumeY(item.volume)} width={bodyWidth} height={332 - volumeY(item.volume)} className="chart-volume" />
        </g>)}
        {overlay.map((series) => <SeriesLine key={series.id} values={series.values.slice(offset)} x={x} y={price.project}
          className={`chart-colour-${data.series.indexOf(series) % 6}`} />)}
        <text x={left} y="256" className="chart-axis-label">成交量 · 股</text>
        <text x={left - 8} y="274" textAnchor="end" className="chart-tick">{format(volumeMax)}</text>
        <text x={left - 8} y="335" textAnchor="end" className="chart-tick">0</text>
        <line x1={left} x2={right} y1="332" y2="332" className="chart-grid" />
        {field && <g>
          <text x={left} y="382" className="chart-axis-label">{name(field)} · {unitNames[field.unit]}</text>
          {[indicator.max, 0, indicator.min].map((value, index) => <g key={index}>
            <line x1={left} x2={right} y1={indicator.project(value)} y2={indicator.project(value)} className="chart-grid" />
            <text x={left - 8} y={indicator.project(value) + 4} textAnchor="end" className="chart-tick">{format(value)}</text>
          </g>)}
          {field.field === "histogram" ? fieldValues.map((value, index) => value === null ? null : <rect key={index}
            x={x(index) - bodyWidth / 2} y={Math.min(indicator.project(0), indicator.project(value))} width={bodyWidth}
            height={Math.max(.7, Math.abs(indicator.project(value) - indicator.project(0)))} className={value >= 0 ? "chart-hist-positive" : "chart-hist-negative"} />)
            : <SeriesLine values={fieldValues} x={x} y={indicator.project} className="chart-colour-0" />}
          {!fieldValues.some((value) => value !== null) && <text x={(left + right) / 2} y="445" textAnchor="middle" className="chart-empty">历史不足或无可用读数</text>}
        </g>}
        {visible && <g className="chart-crosshair" aria-hidden="true">
          <line x1={x(selected - offset)} x2={x(selected - offset)} y1="25" y2={field ? 490 : 333} className="chart-selection chart-selection-x" />
          <line x1={left} x2={right} y1={crossY} y2={crossY} className="chart-selection chart-selection-y" />
        </g>}
        {dates.map((index) => <text x={x(index)} y={field ? 516 : 356} key={index} textAnchor={index === 0 ? "start" : index === bars.length - 1 ? "end" : "middle"} className="chart-tick">{dateLabel(bars[index].date, width < 560)}</text>)}
      </svg>
      {visible && <div className={`chart-tooltip ${x(selected - offset) < width / 2 ? "chart-tooltip-right" : "chart-tooltip-left"}`}
        role="note" aria-label="选定K线读数">
        <strong>{dateLabel(bar.date)}</strong>
        {"complete" in bar && !bar.complete && <p>本周未完（截至 {data.as_of_date}）</p>}
        {"start_at" in bar && <p className="chart-time-range" title={`${bar.start_at} — ${bar.end_at}`}>{dateLabel(String(bar.start_at), true)} — {dateLabel(String(bar.end_at), true)}</p>}
        <dl>{[["开", bar.open], ["高", bar.high], ["低", bar.low], ["收", bar.close], ["量", bar.volume]].map(([label, value]) => <div key={label}>
          <dt>{label}</dt><dd>{readoutNumber(Number(value))} {label === "量" ? "股" : "元/股"}</dd>
        </div>)}
          {shownSeries.map((series) => <div key={series.id}>
            <dt className={`chart-colour-${priceKind(series) ? data.series.indexOf(series) % 6 : 0}`} title={name(series)}>{name(series)}</dt>
            <dd>{valueText(series)}{series.values[selected] === null && <small>{statusNames[series.statuses[selected]]}</small>}</dd>
          </div>)}
        </dl>
        {!!omittedLines && <p>另有 {omittedLines} 条线</p>}
      </div>}
    </div>
    <span className="sr-only" role="status" aria-live="polite" aria-atomic="true">{announcement}</span>
    {!!overlay.length && <ul className="chart-legend" aria-label="价格指标图例">{overlay.map((series) => <li key={series.id} className={`chart-colour-${data.series.indexOf(series) % 6}`}>
      <span aria-hidden="true">━</span> {name(series)}
    </li>)}</ul>}
  </div>;
}

type IntervalLoader = (interval: ChartInterval, signal: AbortSignal) => Promise<unknown>;

function IntervalWorkspace({ daily, historical, capabilityValue, loadInterval }: {
  daily: ResearchChartData; historical: boolean; capabilityValue: unknown; loadInterval?: IntervalLoader;
}) {
  const [loaded, setLoaded] = useState<{ snapshot: ResearchChartData; chart: DisplayChartData } | null>(null);
  const data = loaded?.snapshot === daily ? loaded.chart : daily;
  const [interval, setInterval] = useState<ChartInterval | "1d">("1d");
  const [loading, setLoading] = useState(false), [failed, setFailed] = useState(false);
  const active = useRef<AbortController | null>(null), revision = useRef(0);
  const capabilities = parseChartCapabilities(capabilityValue, daily);
  useEffect(() => {
    setLoaded(null); setInterval("1d"); setLoading(false); setFailed(false);
    return () => { revision.current++; active.current?.abort(); };
  }, [daily]);
  const select = async (next: ChartInterval | "1d") => {
    const current = ++revision.current;
    active.current?.abort(); setFailed(false); setInterval(next);
    if (next === "1d") { setLoaded(null); setLoading(false); return; }
    if (!loadInterval || !capabilities?.[next].enabled) return;
    const abort = new AbortController(); active.current = abort;
    setLoading(true);
    try {
      const response = await loadInterval(next, abort.signal);
      if (revision.current !== current || abort.signal.aborted) return;
      const parsed = parseIntervalChart(response, daily, next);
      if (!parsed) throw new Error("chart_unavailable");
      setLoaded({ snapshot: daily, chart: parsed });
    } catch {
      if (revision.current !== current || abort.signal.aborted) return;
      setFailed(true); setInterval("1d"); setLoaded(null);
    } finally { if (revision.current === current) setLoading(false); }
  };
  const current = data.version === "research-chart-v1" ? "1d" : data.interval;
  const saved = historical || daily.context === "saved";
  const hourly = current === "60m" || current === "120m";
  const controls = <div className="chart-intervals" role="group" aria-label="K线周期">
    {(["60m", "120m", "1d", "1w"] as const).map((item) => <button type="button" key={item} aria-pressed={interval === item}
      aria-label={intervalNames[item]} aria-busy={loading && interval === item}
      disabled={item !== "1d" && (!loadInterval || !capabilities?.[item].enabled || loading && interval === item)}
      title={item !== "1d" && !capabilities?.[item].enabled ? item === "1w" ? "本会话暂无周线能力" : "当前来源暂不支持小时线" : undefined}
      onClick={() => void select(item)}>{intervalNames[item]}{loading && interval === item ? "…" : ""}</button>)}
  </div>;
  return <section className="research-chart" aria-label="历史 K 线与指标">
    <header className="chart-heading"><h4>历史 K 线与指标</h4><span>{data.bars.length} 条{intervalNames[current]} · {saved
      ? hourly ? "按历史研究窗口取数，非新分析" : "已保存快照，非新分析" : "本轮数据快照"}</span></header>
    <p className={`chart-provenance ${data.data_authenticity !== "provider_reported" ? "chart-warning" : ""}`}>
      <strong>{data.data_authenticity === "synthetic" ? "人工样例 · 非真实行情" : data.data_authenticity === "unverified_csv" ? "CSV 输入 · 真实性未核验" : "供应商报告值 · 未独立核验"}</strong>
      {" · 来源："}{data.source === "csv" ? "CSV" : data.source === "tencent" ? "腾讯" : "AKShare / 东方财富"}
      {" · 复权："}{data.price_adjustment === "unknown" ? "未知" : "不复权"}{hourly ? " · 分钟数据有延迟、非实时 · 上海时间" : current === "1d" ? " · 历史日线，非实时" : " · 历史周线，非实时"}
    </p>
    {data.version !== "research-chart-v1" && <p className="chart-period-note">实际覆盖 {dateLabel(data.coverage.start_at)} — {dateLabel(data.coverage.end_at)}（上海时间）。指标按{intervalNames[current]}条数重算，空值表示历史不足或无可用读数。
      {current === "1w" && data.bars.some((bar) => !bar.complete) && ` 本周未完（截至快照 ${data.as_of_date}）。`}</p>}
    {!capabilities?.["60m"].enabled && <p className="chart-period-note">当前来源暂不支持小时线；日线和可用周线仍可查看。</p>}
    {loading && <p role="status" className="chart-period-note">正在加载{intervalNames[interval]}，仍可切回日线…</p>}
    {failed && <p role="status" className="chart-period-note">所选周期暂不可用，已显示原日线；可以重新选择周期。</p>}
    <ChartWorkspace data={data} intervalControls={controls} key={`${data.request_id}:${current}`} />
  </section>;
}

export function ResearchChart({ value, parameters, reportRequestId, historical = false, capabilities, loadInterval }: {
  value: unknown; parameters: ChartParameters | null; reportRequestId: string | undefined; historical?: boolean;
  capabilities?: unknown; loadInterval?: IntervalLoader;
}) {
  if (value === undefined || value === null) return null;
  const data = parseResearchChart(value, parameters, reportRequestId);
  if (!data) return <p className="chart-unavailable" role="note">图表暂不可用或未通过范围校验；不影响上方已确认的研究答案与证据。</p>;
  return <IntervalWorkspace daily={data} historical={historical} capabilityValue={capabilities} loadInterval={loadInterval}
    key={`${data.request_id}:${data.context}`} />;
}
