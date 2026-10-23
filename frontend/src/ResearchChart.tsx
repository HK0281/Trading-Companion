import { useId, useState } from "react";
import { chartScale, lineSegments, parseResearchChart, type ChartParameters, type ChartSeries, type ResearchChartData } from "./charts";
import "./charts.css";

const unitNames = { "CNY/share": "元/股", shares: "股", index_points: "点", multiple: "倍" };
const statusNames = { available: "可用", insufficient_history: "历史不足", undefined: "未定义", calculation_error: "计算失败" };
const format = (value: number) => new Intl.NumberFormat("zh-CN", { maximumSignificantDigits: 7 }).format(value);
const priceKind = (series: ChartSeries) => ["sma", "ema", "boll"].includes(series.kind);
const tone = (open: number, close: number) => close > open ? "chart-rise" : close < open ? "chart-fall" : "chart-flat";

function SeriesLine({ values, x, y, className }: { values: readonly (number | null)[]; x: (index: number) => number; y: (value: number) => number; className: string }) {
  return <g className={className}>{lineSegments(values, x, y).map((points, index) => points.includes(" ")
    ? <polyline key={index} points={points} className="chart-line" />
    : <circle key={index} cx={Number(points.split(",")[0])} cy={Number(points.split(",")[1])} r="2.4" className="chart-point" />)}</g>;
}

function ChartWorkspace({ data }: { data: ResearchChartData }) {
  const id = useId();
  const [windowSize, setWindowSize] = useState(60);
  const [selectedIndex, setSelectedIndex] = useState(data.bars.length - 1);
  const groups = [...new Set(data.series.filter(priceKind).map((series) => series.indicator_id))];
  const [hidden, setHidden] = useState<string[]>([]);
  const independent = data.series.filter((series) => !priceKind(series));
  const [fieldId, setFieldId] = useState(independent[0]?.id ?? "");
  const offset = Math.max(0, data.bars.length - windowSize);
  const bars = data.bars.slice(offset);
  const selected = Math.max(offset, Math.min(data.bars.length - 1, selectedIndex));
  const bar = data.bars[selected];
  const overlay = data.series.filter((series) => priceKind(series) && !hidden.includes(series.indicator_id));
  const field = independent.find((series) => series.id === fieldId);
  const fieldValues = field?.values.slice(offset) ?? [];
  const left = 76, right = 940;
  const x = (index: number) => left + (index + .5) / bars.length * (right - left);
  const bodyWidth = Math.max(.7, Math.min(12, (right - left) / bars.length * .65));
  const price = chartScale([...bars.flatMap((item) => [item.high, item.low]), ...overlay.flatMap((series) => series.values.slice(offset).filter((item): item is number => item !== null))], 28, 230);
  const volumeMax = Math.max(1, ...bars.map((item) => item.volume));
  const volumeY = (value: number) => 332 - value / volumeMax * 65;
  const indicator = chartScale(fieldValues.filter((value): value is number => value !== null), 397, 487, true);
  const dates = [...new Set([0, Math.floor((bars.length - 1) / 2), bars.length - 1])];
  const toggle = (indicatorId: string) => setHidden((previous) => previous.includes(indicatorId) ? previous.filter((item) => item !== indicatorId) : [...previous, indicatorId]);

  return <div className="chart-workspace">
    <div className="chart-controls">
      <label htmlFor={`${id}-window`}>显示日线 <select id={`${id}-window`} value={windowSize}
        onChange={(event) => setWindowSize(Number(event.target.value))}>
        <option value={30}>最近 30 条</option><option value={60}>最近 60 条</option><option value={120}>最近 120 条</option><option value={366}>全部</option>
      </select></label>
      <label htmlFor={`${id}-field`}>独立指标 <select id={`${id}-field`} value={field?.id ?? ""} onChange={(event) => setFieldId(event.target.value)}>
        <option value="">不显示</option>{independent.map((series) => <option value={series.id} key={series.id}>{series.indicator_id} · {series.label}（{unitNames[series.unit]}）</option>)}
      </select></label>
    </div>
    {!!groups.length && <fieldset className="chart-overlays"><legend>价格叠加 · 元/股</legend>{groups.map((group) => <label key={group}>
      <input type="checkbox" checked={!hidden.includes(group)} onChange={() => toggle(group)} />{group}
    </label>)}</fieldset>}
    <p className="chart-window">当前可见：{bars[0].date} — {bars.at(-1)!.date}，{bars.length} 条。仅使用返回交易日，不补周末、停牌或其他缺失日期。</p>
    <div className="chart-plot">
      <svg viewBox={`0 0 960 ${field ? 529 : 370}`} width="960" height={field ? 529 : 370} role="img" aria-labelledby={`${id}-title ${id}-desc`}>
        <title id={`${id}-title`}>{data.instrument_id} 历史日 K 线、成交量{field ? "及独立指标" : ""}</title>
        <desc id={`${id}-desc`}>红色上涨、绿色下跌、灰色平盘，以当日收盘对开盘比较。不同单位分区显示；指标缺失处断线。用下方日期滑块查看数值，显示时保留7位有效数字。</desc>
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
          <text x={left} y="382" className="chart-axis-label">{field.indicator_id} · {field.label}（{unitNames[field.unit]}）</text>
          {[indicator.max, 0, indicator.min].map((value, index) => <g key={index}>
            <line x1={left} x2={right} y1={indicator.project(value)} y2={indicator.project(value)} className="chart-grid" />
            <text x={left - 8} y={indicator.project(value) + 4} textAnchor="end" className="chart-tick">{format(value)}</text>
          </g>)}
          {field.field === "histogram" ? fieldValues.map((value, index) => value === null ? null : <rect key={index}
            x={x(index) - bodyWidth / 2} y={Math.min(indicator.project(0), indicator.project(value))} width={bodyWidth}
            height={Math.max(.7, Math.abs(indicator.project(value) - indicator.project(0)))} className={value >= 0 ? "chart-hist-positive" : "chart-hist-negative"} />)
            : <SeriesLine values={fieldValues} x={x} y={indicator.project} className="chart-colour-0" />}
          {!fieldValues.some((value) => value !== null) && <text x="508" y="445" textAnchor="middle" className="chart-empty">本区间没有可用数值；未补零。</text>}
        </g>}
        <line x1={x(selected - offset)} x2={x(selected - offset)} y1="25" y2={field ? 490 : 333} className="chart-selection" />
        {dates.map((index) => <text x={x(index)} y={field ? 516 : 356} key={index} textAnchor={index === 0 ? "start" : index === bars.length - 1 ? "end" : "middle"} className="chart-tick">{bars[index].date}</text>)}
      </svg>
    </div>
    {!!overlay.length && <ul className="chart-legend" aria-label="价格指标图例">{overlay.map((series) => <li key={series.id} className={`chart-colour-${data.series.indexOf(series) % 6}`}>
      <span aria-hidden="true">━</span> {series.indicator_id} · {series.label}
    </li>)}</ul>}
    <p className="chart-direction">红涨 / 绿跌 / 灰平：收盘价相对当日开盘价，不是相对前一日收盘价。</p>
    <label className="chart-slider" htmlFor={`${id}-date`}>选定交易日：<strong aria-live="polite">{bar.date}</strong>
      <input id={`${id}-date`} type="range" min={offset} max={data.bars.length - 1} step="1" value={selected}
        aria-valuetext={bar.date} onChange={(event) => setSelectedIndex(Number(event.target.value))} />
      <small>支持键盘方向键逐日查看；数值显示保留7位有效数字，不改变原计算精度。图形不构成预测。</small>
    </label>
    <dl className="chart-ohlcv" aria-label="选定交易日行情">{[["开盘", bar.open], ["最高", bar.high], ["最低", bar.low], ["收盘", bar.close], ["成交量", bar.volume]].map(([name, value]) => <div key={name}>
      <dt>{name}</dt><dd>{format(Number(value))} {name === "成交量" ? "股" : "元/股"}</dd>
    </div>)}</dl>
    {!!data.series.length && <div className="chart-table-scroll"><table className="chart-values">
      <caption>{bar.date} 指标读数 · 图表点不是独立证据编号</caption>
      <thead><tr><th>指标 / 字段</th><th>数值</th><th>状态与限制</th></tr></thead>
      <tbody>{data.series.map((series) => <tr key={series.id}><td>{series.indicator_id}<small>{series.label}</small></td>
        <td>{series.values[selected] === null ? "—" : `${format(series.values[selected]!)} ${unitNames[series.unit]}`}</td>
        <td>{statusNames[series.statuses[selected]]}{series.start_effect_warning && <small>包含递推起点敏感性提示</small>}</td>
      </tr>)}</tbody>
    </table></div>}
  </div>;
}

export function ResearchChart({ value, parameters, reportRequestId, historical = false }: {
  value: unknown; parameters: ChartParameters | null; reportRequestId: string | undefined; historical?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  if (value === undefined || value === null) return null;
  const data = parseResearchChart(value, parameters, reportRequestId);
  if (!data) return <p className="chart-unavailable" role="note">图表暂不可用或未通过范围校验；不影响上方已确认的研究答案与证据。</p>;
  const saved = historical || data.context === "saved";
  return <details className="research-chart" onToggle={(event) => setExpanded(event.currentTarget.open)}>
    <summary>历史 K 线与指标 <span>{data.bars.length} 条日线 · {saved ? "已保存快照，非新分析" : "本轮数据快照"}</span></summary>
    <div className={`chart-provenance ${data.data_authenticity === "synthetic" ? "chart-synthetic" : ""}`}>
      <strong>{data.data_authenticity === "synthetic" ? "人工样例 · 非真实行情" : data.data_authenticity === "unverified_csv" ? "CSV 输入 · 真实性未核验" : "数据提供方报告值 · 未独立核验"}</strong>
      <p>{saved ? "历史上下文，不是新行情或重新分析。" : "历史日线快照，不是实时行情。"}来源：{data.source === "csv" ? "CSV" : data.source === "tencent" ? "腾讯" : "AKShare / 东方财富"}；复权：{data.price_adjustment === "unknown" ? "未知，不能假定未复权" : "不复权"}。</p>
      <p>请求范围：{data.start_date} — {data.as_of_date}；实际范围：{data.bars[0].date} — {data.bars.at(-1)!.date}。</p>
      <p>获取时间：{data.retrieved_at ?? "未提供"}{data.saved_at && `；保存时间：${data.saved_at}`}。</p>
      <small>图表仅校验请求绑定、字段口径与日线对齐；不等于所有历史点已建立独立证据。研究结论以报告证据为准。</small>
    </div>
    {expanded && <ChartWorkspace data={data} key={`${data.request_id}:${data.context}`} />}
  </details>;
}
