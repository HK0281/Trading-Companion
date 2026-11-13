import { useState } from "react";
import { chartScale } from "./charts";
import { reviewPriceLabels, reviewTime, type ResearchReviewPrices } from "./researchReviews";

const priceFields = new Set(["reference_entry", "reference_stop", "target_1", "target_2", "reference_close", "exit_below", "observe_below", "observe_above"]);
export function ResearchReviewChart({ data }: { data: ResearchReviewPrices }) {
  const [selected, setSelected] = useState(0);
  const item = data.item;
  if (data.status !== "available" || data.points.length === 0) return <div className="review-chart" role="status">
    {item.prices.notice && <p className="review-caution">{item.prices.notice}</p>}
    <p>{data.message ?? "暂无可展示的后续已完成日线。"}</p></div>;
  const points = data.points, active = Math.min(selected, points.length - 1);
  const references = item.trade_plan?.price_adjustment === data.price_adjustment && item.trade_plan?.data_authenticity === data.data_authenticity
    ? Object.entries(item.trade_plan.values).filter(([field]) => priceFields.has(field)) : [];
  const width = 760, height = 300, left = 62, right = 730, top = 18, bottom = 250;
  const scale = chartScale([...points.map(point => point.close), ...references.map(([, value]) => Number(value))], top, bottom);
  const start = Date.parse(points[0].date), span = Date.parse(points.at(-1)!.date) - start;
  const x = (day: string) => span === 0 ? (left + right) / 2 : left + (Date.parse(day) - start) / span * (right - left);
  const choose = (clientX: number, element: SVGSVGElement) => {
    const rect = element.getBoundingClientRect(); if (!rect.width) return;
    const position = (clientX - rect.left) / rect.width * width;
    setSelected(points.reduce((closest, point, index) => Math.abs(x(point.date) - position) < Math.abs(x(points[closest].date) - position) ? index : closest, 0));
  };
  return <section className="review-chart" aria-label={`${item.name}的后续日线`}>
    <h4>后来实际日线收盘价 · 元/股</h4>
    <p className="review-meta">{item.instrument_id} · 范围 {item.prices.start_date} — {item.prices.end_date}<br />
      腾讯行情 · 不复权 · 供应商报告，未经独立核实 · 取数 {reviewTime(data.retrieved_at!)}（上海时间）</p>
    {item.prices.notice && <p className="review-caution">{item.prices.notice}</p>}
    <p className="review-meta">仅展示来源实际返回的已完成日线；非交易日、停牌和数据空缺不补价格。不复权数据在除权时可能跳变。</p>
    <div className="review-chart-interaction" tabIndex={0} role="group" aria-label="后续收盘价图，方向键选择日期" onKeyDown={event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault(); setSelected(event.key === "Home" ? 0 : event.key === "End" ? points.length - 1
        : Math.max(0, Math.min(points.length - 1, active + (event.key === "ArrowLeft" ? -1 : 1))));
    }}>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="后续收盘价与旧参考价位" onMouseMove={event => choose(event.clientX, event.currentTarget)}
        onClick={event => choose(event.clientX, event.currentTarget)}>
        {[0, 1, 2, 3, 4].map(index => { const value = scale.min + (scale.max - scale.min) * index / 4; const y = scale.project(value);
          return <g key={index}><line x1={left} x2={right} y1={y} y2={y} className="review-grid-line" /><text x={left - 8} y={y + 4} textAnchor="end">{value.toFixed(2)}</text></g>; })}
        {references.map(([field, value]) => <line key={field} x1={left} x2={right} y1={scale.project(Number(value))} y2={scale.project(Number(value))} className="review-reference-line"><title>{reviewPriceLabels[field]}：{value} 元</title></line>)}
        <polyline points={points.map(point => `${x(point.date)},${scale.project(point.close)}`).join(" ")} className="review-close-line" />
        <line x1={x(points[active].date)} x2={x(points[active].date)} y1={top} y2={bottom} className="review-cursor-line" />
        <circle cx={x(points[active].date)} cy={scale.project(points[active].close)} r="4" className="review-close-point" />
        <text x={left} y={278}>{points[0].date}</text><text x={right} y={278} textAnchor="end">{points.at(-1)!.date}</text>
      </svg>
      <p className="review-selected-price" aria-live="polite">{points[active].date} · 实际收盘 {points[active].close} 元</p>
    </div>
    {references.length > 0 && <p className="review-meta">虚线为旧参考价位（行情 {item.trade_plan!.source_date}）：{references.map(([field, value]) => `${reviewPriceLabels[field]} ${value} 元`).join("；")}。</p>}
    <p className="review-meta">旧价位由当时程序计算，不是预测目标，也不代表真实成交；本图不计算命中率、准确率或投资收益。</p>
  </section>;
}
