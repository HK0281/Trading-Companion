/** Index-only display contract. Exact decimal checks never recompute market indicators. */
import { validChartDate } from "./charts";
import type { IndexResearchData } from "./IndexResearch";
import { displayRatio, horizonProfile } from "./ResearchDecision";

const obj = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === "object" && !Array.isArray(v);
const text = (v: unknown, max: number): v is string => typeof v === "string" && v.length > 0 && v.length <= max && !/[\x00-\x1f\x7f]/.test(v);
const purposes = ["conditional_entry", "holding_exit", "wait"] as const;
type Purpose = typeof purposes[number];
const directionPurpose: Record<string, Purpose> = { 偏多: "conditional_entry", 震荡: "wait", 偏空: "holding_exit" };
const fields: Record<Purpose, string[]> = {
  conditional_entry: ["reference_entry", "reference_stop", "target_1", "target_2", "reward_risk_1", "reward_risk_2", "atr", "atr_multiple"],
  holding_exit: ["reference_close", "exit_below"], wait: ["observe_below", "observe_above"],
};
const labels: Record<string, string> = { reference_entry: "入场参考", reference_stop: "止损参考（跌破观察位）",
  target_1: "止盈1（目标1）", target_2: "止盈2（目标2）", reward_risk_1: "实际盈亏比1", reward_risk_2: "实际盈亏比2",
  reference_close: "收盘参考", exit_below: "退出观察位（跌破止损参考）", observe_below: "下方观察位", observe_above: "上方观察位", atr: "ATR", atr_multiple: "ATR倍数" };
const reasons: Record<string, string> = {
  plan_not_requested: "本轮未请求点位计划。", plan_excluded: "本轮已排除参考计划。",
  risk_atr_not_requested: "范围未允许ATR14，未补充计划依赖。", decision_horizon_required: "尚未确认观察期限。",
  plan_long_history_insufficient: "长线计划需要至少365自然日窗口及350日实际历史跨度。",
  plan_weekly_atr_unavailable: "完整周线不足15根或周ATR不可用，未回退日ATR。",
  plan_atr_unavailable: "同轮日ATR14不足或波动无效。", plan_price_grid_unavailable: "点位舍入后无法形成有效计划。",
  index_direction_unavailable: "尚无通过核对的明确方向，未展示点位计划。",
};
const note = "指数不可直接交易，点位为情景参考。";
const parameters: Record<string, [string, string, string[]]> = { short: ["daily", "1.5", ["1.5", "2.5"]],
  medium: ["daily", "2.5", ["2", "3"]], long: ["weekly", "2", ["2", "3.5"]] };
const scale = 10n ** 20n;
function decimal(v: unknown): bigint | null {
  if (typeof v !== "string" || !/^[0-9]{1,18}(?:\.[0-9]{1,20})?$/.test(v)) return null;
  const [whole, fraction = ""] = v.split(".");
  const n = BigInt(whole) * scale + BigInt(fraction.padEnd(20, "0"));
  return n > 0n && n <= 10n ** 18n * scale ? n : null;
}
type Block = { title: string; text: string; evidence_ids: string[] };
type Plan = { purpose: Purpose | null; status: string; reason: string; horizon: string | null;
  atr_timeframe: string | null; atr_source_date: string | null; source_date: string;
  values: Record<string, string> | null; quality: { partial: boolean }; blocks: Block[] };

export function parseIndexScenario(value: unknown, research: IndexResearchData): Plan | null {
  if (!obj(value) || value.version !== "index-scenario-plan-v1" || value.method !== "index_atr_scenario_v1"
    || value.instrument_kind !== "index" || value.price_unit !== "index_point" || value.origin !== "application"
    || value.execution_allowed !== false || value.note !== note || value.instrument_id !== research.instrument_id
    || value.request_id !== research.request_id || value.source !== research.source || value.data_authenticity !== research.data_authenticity
    || value.horizon !== research.horizon || value.source_date !== research.latest_date
    || typeof value.input_snapshot_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(value.input_snapshot_sha256)
    || value.atr_algorithm !== "atr-wilder-prior-close-v1" || value.status !== "available" && value.status !== "unavailable") return null;
  const purpose = research.origin === "model" ? directionPurpose[research.direction] ?? null : null;
  if (value.purpose !== purpose || value.plan_id !== (purpose
    ? `${research.request_id}:index_atr_scenario_v1:${value.input_snapshot_sha256.slice(0, 24)}:${purpose}` : null)) return null;
  const profile = horizonProfile(value.horizon), expected = profile ? parameters[profile] : null;
  if (value.horizon_profile !== profile || !Array.isArray(value.target_multiples) || !value.target_multiples.every(v => typeof v === "string")
    || (expected ? value.atr_timeframe !== expected[0] || value.atr_multiple !== expected[1]
      || value.target_multiples.length !== expected[2].length || value.target_multiples.some((v, i) => v !== expected[2][i])
      : value.atr_timeframe !== null || value.atr_multiple !== null || value.target_multiples.length !== 0)) return null;
  if (value.atr_source_date !== null && (!validChartDate(value.atr_source_date)
    || value.atr_source_date < research.start_date || value.atr_source_date > research.latest_date)) return null;
  const ids = new Set<string>();
  if (!Array.isArray(value.evidence) || !Array.isArray(value.blocks) || value.blocks.length > 2) return null;
  if (value.status === "available") {
    if (!purpose || !expected || value.code !== "plan_ready" || value.reason !== "" || !validChartDate(value.atr_source_date)
      || !obj(value.values)) return null;
    // A weekly ATR must end in a completed week; daily ATR ends on the current daily close.
    const atrDay = new Date(value.atr_source_date + "T00:00:00Z");
    const weekEnd = atrDay.getTime() + (7 - atrDay.getUTCDay()) % 7 * 86400000;
    if (atrDay.getUTCDay() === 0 || atrDay.getUTCDay() === 6
      || value.atr_timeframe === "daily" && value.atr_source_date !== research.latest_date
      || value.atr_timeframe === "weekly" && weekEnd > Date.parse(research.as_of_date + "T00:00:00Z")) return null;
    const keys = fields[purpose], values = value.values;
    if (Object.keys(values).length !== keys.length || !keys.every(k => decimal(values[k]) !== null)
      || value.evidence.length !== keys.length) return null;
    const n = (k: string) => decimal(values[k])!;
    if (purpose === "conditional_entry") {
      const entry = n("reference_entry"), stop = n("reference_stop"), first = n("target_1"), second = n("target_2"), risk = entry - stop;
      if (!(stop < entry && entry < first && first < second) || values.atr_multiple !== value.atr_multiple) return null;
      for (const [i, target] of [first, second].entries()) {
        const grid = scale / 100n, ratioGrid = scale / 1000000n;
        if (target !== (entry + risk * decimal(expected[2][i])! / scale) / grid * grid
          || n(`reward_risk_${i + 1}`) !== (target - entry) * 1000000n / risk * ratioGrid) return null;
      }
    } else if (purpose === "holding_exit" ? n("exit_below") >= n("reference_close") : n("observe_below") >= n("observe_above")) return null;
    for (const [i, e] of value.evidence.entries()) {
      const field = keys[i], id = `S${purposes.indexOf(purpose) + 1}_${i + 1}`;
      if (!obj(e) || e.id !== id || e.field !== field || e.plan_id !== value.plan_id || e.instrument_id !== value.instrument_id
        || e.content_kind !== "index_scenario" || e.kind !== "technical" || e.source !== value.source
        || e.timeframe !== value.atr_timeframe || e.date !== (field === "atr" ? value.atr_source_date : value.source_date)
        || e.unit !== (field.startsWith("reward_risk") || field === "atr_multiple" ? "ratio" : "index_point")
        || e.raw_value !== values[field] || e.display_value !== values[field] || e.label !== labels[field]) return null;
      ids.add(id);
    }
  } else if (value.values !== null || value.evidence.length !== 0 || value.blocks.length !== 0
    || typeof value.code !== "string" || !Object.hasOwn(reasons, value.code) || value.reason !== reasons[value.code]
    || value.code === "index_direction_unavailable" && purpose !== null
    || ["plan_long_history_insufficient", "plan_weekly_atr_unavailable"].includes(value.code) && profile !== "long"
    || value.code === "plan_atr_unavailable" && value.atr_timeframe !== "daily"
    || value.code === "decision_horizon_required" && profile !== null) return null;
  for (const b of value.blocks) {
    if (!obj(b) || typeof b.title !== "string" || !["情景", "关键点位", "观察重点", "局限"].includes(b.title) || !text(b.text, 600)
      || /conditional_entry|holding_exit|\bwait\b/.test(b.text)
      || !Array.isArray(b.evidence_ids) || !b.evidence_ids.length || b.evidence_ids.length > 20
      || new Set(b.evidence_ids).size !== b.evidence_ids.length || !b.evidence_ids.every(id => typeof id === "string" && ids.has(id))) return null;
  }
  const q = value.quality;
  const counts = ["generated_sentences", "retained_sentences", "deleted_sentences", "retained_paragraphs", "rejected_paragraphs", "partially_retained_sentences", "repair_attempts"];
  if (!obj(q) || q.version !== "index-answer-quality-v1" || typeof q.partial !== "boolean"
    || !counts.every(k => Number.isSafeInteger(q[k]) && Number(q[k]) >= 0 && Number(q[k]) <= 3600)
    || q.generated_sentences !== Number(q.retained_sentences) + Number(q.deleted_sentences)
    || q.retained_paragraphs !== value.blocks.length || Number(q.rejected_paragraphs) > 2 || Number(q.repair_attempts) > 1
    || Number(q.partially_retained_sentences) > Number(q.retained_sentences)
    || q.partial !== Boolean(q.deleted_sentences || q.rejected_paragraphs || q.partially_retained_sentences)) return null;
  return value as unknown as Plan;
}

export function IndexScenario({ value, research }: { value: unknown; research: IndexResearchData }) {
  const plan = parseIndexScenario(value, research);
  if (!plan) return <p className="data-note" role="note">指数点位计划未通过显示校验；已保留有效走势和图表。</p>;
  return <section className="index-scenario research-verdict" aria-label="指数点位情景计划">
    <h5>点位情景计划</h5>
    {plan.horizon && <p className="data-note">观察期限：{plan.horizon} · {plan.atr_timeframe === "weekly" ? "完整周线" : "日线"} ATR14（历史波动指标）</p>}
    <p className="data-note">收盘日期 {plan.source_date}{plan.atr_source_date && ` · 波动日期 ${plan.atr_source_date}`}</p>
    {plan.values && plan.purpose ? <>
      {plan.purpose === "holding_exit" && <p>假如已持有跟踪该指数的基金，以下仅为指数层面的跌破警惕位，不对应基金成交价。</p>}
      {plan.purpose === "conditional_entry" && <p>偏多条件下的入场与止损止盈参考，触及点位时需重新核对依据。</p>}
      {plan.purpose === "wait" && <p>等待观察，触及区间边界时重新核对依据，不代表自动买卖。</p>}
      <div className="table-scroll"><table className="reference-plan"><caption>指数点位参考</caption>
        <thead><tr><th scope="col">项目</th><th scope="col">参考值</th></tr></thead><tbody>{fields[plan.purpose].map(field =>
          <tr key={field}><th scope="row">{labels[field]}</th><td>{field.startsWith("reward_risk") ? displayRatio(plan.values![field]) : plan.values![field]}{field.startsWith("reward_risk") || field === "atr_multiple" ? " 倍" : " 点"}</td></tr>)}</tbody>
      </table></div>
    </> : <p role="note">点位计划不可用：{plan.reason}</p>}
    {plan.quality.partial && <p className="data-note" role="note">部分计划说明未通过核对已移除</p>}
    {plan.blocks.map((b, i) => <section className="insight readable-text" key={i}><h5>{b.title}</h5><p>{b.text}</p></section>)}
    <p className="data-note">{note}</p>
  </section>;
}
