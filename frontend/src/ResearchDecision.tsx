/** v2 verdicts and application-calculated prices; no provider text becomes HTML. */
type Claim = { text: string; evidence_ids: string[] };
type Verdict = { instrument_id: string; status: string; code: string; rating: string | null;
  direction: string | null; horizon: string; conviction: string | null; reasons: Claim[];
  invalidation: Claim | null; plan_id: string | null };
type Plan = { plan_id: string; instrument_id: string; purpose: string; status: string; code: string;
  source_date: string | null; data_authenticity: string | null; source: string | null;
  price_adjustment: string | null; values: Record<string, string> | null };
export type DecisionData = { decisions: Verdict[]; trade_plans: Plan[] };
const obj = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === "object" && !Array.isArray(v);
const txt = (v: unknown, max = 600): v is string => typeof v === "string" && v.length > 0 && v.length <= max;
const nullable = (v: unknown) => v === null || txt(v, 200);
const fieldSets: Record<string, string[]> = {
  conditional_entry: ["reference_entry", "reference_stop", "target_1", "target_2", "reward_risk_1", "reward_risk_2", "atr", "atr_multiple"],
  holding_exit: ["reference_close", "exit_below"], wait: ["observe_below", "observe_above"],
};
const ratings: Record<string, [string, string[]]> = { "买入": ["conditional_entry", ["偏多"]], "增持": ["conditional_entry", ["偏多"]],
  "持有": ["wait", ["偏多", "震荡"]], "减持": ["holding_exit", ["震荡", "偏空"]], "卖出": ["holding_exit", ["震荡", "偏空"]] };

export function parseDecision(value: Record<string, unknown>, securities: Set<string>, evidence: Map<string, string>): DecisionData | null {
  if (!Array.isArray(value.decisions) || value.decisions.length !== securities.size
    || securities.size < 1 || !Array.isArray(value.trade_plans) || value.trade_plans.length !== securities.size * 3) return null;
  const plans = new Map<string, Plan>();
  const purposes = new Set<string>();
  for (const p of value.trade_plans) {
    if (!obj(p) || !txt(p.plan_id, 200) || plans.has(p.plan_id) || !securities.has(String(p.instrument_id))
      || !txt(p.purpose) || !Object.hasOwn(fieldSets, p.purpose) || !["available", "unavailable"].includes(String(p.status))
      || p.version !== "trade-plan-v1" || p.method !== "atr_reference_plan_v1" || p.origin !== "application" || p.execution_allowed !== false
      || !txt(p.code, 100) || !nullable(p.source) || !nullable(p.data_authenticity) || !nullable(p.price_adjustment)
      || !(p.source_date === null || typeof p.source_date === "string" && /^\d{4}-\d{2}-\d{2}$/.test(p.source_date))) return null;
    const purposeKey = `${p.instrument_id}|${p.purpose}`;
    if (purposes.has(purposeKey)) return null;
    purposes.add(purposeKey);
    if (p.status === "available") {
      const fields = fieldSets[p.purpose];
      // trade-plan-v1 ATR is displayed to 4 decimals; see docs/research-decision-plan-design.md.
      if (!obj(p.values) || Object.keys(p.values).length !== fields.length || !fields.every(k =>
        typeof (p.values as Record<string, unknown>)[k] === "string" && /^[0-9]{1,18}(?:\.[0-9]{1,12})?$/.test(String((p.values as Record<string, unknown>)[k]))
        && Number((p.values as Record<string, unknown>)[k]) > 0)) return null;
      const n = (k: string) => Number((p.values as Record<string, string>)[k]);
      if (p.purpose === "conditional_entry" && !(n("reference_stop") < n("reference_entry") && n("reference_entry") < n("target_1") && n("target_1") < n("target_2"))) return null;
    } else if (p.values !== null) return null;
    plans.set(p.plan_id, p as unknown as Plan);
  }
  const seen = new Set<string>();
  for (const d of value.decisions) {
    if (!obj(d) || !txt(d.instrument_id) || !securities.has(d.instrument_id) || seen.has(d.instrument_id)
      || !["available", "unavailable"].includes(String(d.status)) || !txt(d.code, 100) || !txt(d.horizon, 80)
      || !Array.isArray(d.reasons) || d.reasons.length > 4 || !nullable(d.plan_id)
      || d.conviction_scope !== "model_self_assessed_not_historically_calibrated") return null;
    const claim = (c: unknown) => obj(c) && txt(c.text) && Array.isArray(c.evidence_ids) && c.evidence_ids.length > 0
      && c.evidence_ids.length <= 20 && new Set(c.evidence_ids).size === c.evidence_ids.length
      && c.evidence_ids.every(id => typeof id === "string" && evidence.get(id) === d.instrument_id);
    if (!d.reasons.every(claim) || !(d.invalidation === null || claim(d.invalidation))) return null;
    if (d.status === "available") {
      const rating = typeof d.rating === "string" && Object.hasOwn(ratings, d.rating) ? ratings[d.rating] : null;
      if (!rating || !rating[1].includes(String(d.direction)) || !["高", "中", "低"].includes(String(d.conviction))
        || d.reasons.length < 2 || d.invalidation === null) return null;
      if (d.plan_id !== null) {
        const p = plans.get(String(d.plan_id));
        if (!p || p.instrument_id !== d.instrument_id || p.purpose !== rating[0] || p.status !== "available") return null;
      } else if ([...plans.values()].some(p => p.instrument_id === d.instrument_id && p.purpose === rating[0] && p.status === "available")) return null;
    } else if (d.rating !== null || d.direction !== null || d.conviction !== null || d.plan_id !== null) return null;
    seen.add(d.instrument_id);
  }
  return value as unknown as DecisionData;
}

const labels: Record<string, string> = { reference_entry: "入场参考", reference_stop: "止损", target_1: "止盈 1（1.5R）",
  target_2: "止盈 2（2.5R）", reward_risk_1: "实际盈亏比 1", reward_risk_2: "实际盈亏比 2", atr: "ATR", atr_multiple: "ATR 倍数",
  reference_close: "收盘参考", exit_below: "退出观察位", observe_below: "下方观察位", observe_above: "上方观察位" };
const unavailable: Record<string, string> = { plan_excluded: "本次研究未启用技术面或参考计划。",
  risk_atr_not_requested: "本次未计算所需的波动指标。", risk_atr_unavailable: "最新波动指标不可用或历史窗口不足。",
  risk_nonpositive_reference: "未取得有效的正价格或历史波动。", risk_evidence_mismatch: "行情未通过来源和日期核对。",
  plan_price_grid_unavailable: "按价格网格舍入后无法形成有效计划。", decision_context_unusable: "未取得完整且通过核对的计划资料。" };
function PlanTable({ plan }: { plan: Plan }) {
  if (!plan.values) return <p className="data-note">参考计划不可用：{unavailable[plan.code] ?? "缺少所需的有效行情或波动资料。"}</p>;
  return <>
    {plan.purpose === "holding_exit" && <p>如已持有：收盘跌破 {plan.values.exit_below} 元时考虑减仓或退出。<br />空仓：观望。</p>}
    {plan.purpose === "wait" && <p>等待观察；触及观察价位时重新核对依据，不代表自动买卖。</p>}
    <div className="table-scroll"><table className="reference-plan"><caption>ATR 参考计划 · 行情 {plan.source_date}</caption>
      <thead><tr><th>项目</th><th>参考值</th></tr></thead><tbody>{Object.entries(plan.values).map(([key, v]) =>
        <tr key={key}><th scope="row">{labels[key]}</th><td>{v}{key.startsWith("reward_risk") || key === "atr_multiple" ? " 倍" : " 元"}</td></tr>)}</tbody></table></div>
    <small>数据性质：{plan.data_authenticity === "synthetic" ? "人工样例，非真实行情" : plan.source === "csv" ? "CSV 数据，真实性未经验证" : "供应商历史数据"} ·
      {({ tencent: "腾讯行情", csv: "开发样例", akshare_eastmoney: "东方财富行情" } as Record<string, string>)[plan.source ?? ""] ?? "历史行情"} ·
      {plan.price_adjustment === "unadjusted" ? "不复权" : "按来源所示复权口径"}<br />
      价位由规则计算，不是期限内的预测目标。未计费用、滑点、跳空及交易限制，不保证按参考价成交。</small>
    {plan.purpose === "conditional_entry" && <p className="data-note">失效条件：后续收盘不高于 {plan.values.reference_stop} 元时，原计划失效，应重新评估。</p>}
  </>;
}

export function ResearchDecision({ data, securities, renderEvidence }: { data: DecisionData;
  securities: { name: string; instrument_id: string }[]; renderEvidence?: (ids: string[]) => React.ReactNode }) {
  return <div className="research-decisions">{data.decisions.map(d => {
    const name = securities.find(s => s.instrument_id === d.instrument_id)?.name ?? d.instrument_id;
    const plans = data.trade_plans.filter(p => p.instrument_id === d.instrument_id);
    const selected = plans.find(p => p.plan_id === d.plan_id) ?? plans.find(p => p.purpose === "conditional_entry");
    return <section className="research-verdict" key={d.instrument_id} aria-label={`${name}参考研判`}>
      <h4>{name} · 参考研判</h4>
      {d.status === "available" ? <>
        <dl className="verdict-grid"><div><dt>评级</dt><dd>{d.rating}</dd></div><div><dt>方向</dt><dd>{d.direction}</dd></div>
          <div><dt>期限</dt><dd>{d.horizon}</dd></div><div><dt>把握</dt><dd>{d.conviction}</dd></div></dl>
        <small>模型自评，未经历史校准；持有、增持或减持均以假如已有仓位为条件。</small>
      </> : <p className="data-note">本轮研判未形成；下方仅为程序计算的参考价位。期限：{d.horizon}</p>}
      {d.reasons.length > 0 && <details><summary>研判依据</summary>{d.reasons.map((r, i) => <div key={i}><p>{r.text}</p>{renderEvidence?.(r.evidence_ids)}</div>)}</details>}
      {d.invalidation && <details><summary>研判失效或重评条件</summary><p>{d.invalidation.text}</p>{renderEvidence?.(d.invalidation.evidence_ids)}</details>}
      {selected && <PlanTable plan={selected} />}
      {d.status === "available" && ["减持", "卖出"].includes(d.rating ?? "") && !selected?.values && <p>如已持有：当前缺少有效退出价位。<br />空仓：观望。</p>}
    </section>;
  })}</div>;
}
