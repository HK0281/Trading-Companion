/** Finite text formatting only. No HTML parser, links, images or executable markup. */
type Claim = { text: string; evidence_ids: string[] };
type Level = Claim & { lower: string; upper: string; lower_label: string; upper_label: string };
export type Narrative = { instrument_id: string; origin?: string; summary: Claim | null; current: Claim | null;
  stance: Claim | null; drivers: (Claim & { title: string })[]; levels: Level[];
  scenarios: Record<string, Claim | null> };
export type NarrativeQuality = { state: string; code: string; reasons: { code: string; section: string; index: number }[] };
const obj = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === "object" && !Array.isArray(v);
const txt = (v: unknown, max = 6000): v is string => typeof v === "string" && v.length > 0 && v.length <= max;

export function parseNarratives(value: Record<string, unknown>, securities: Set<unknown>, evidence: Map<unknown, unknown>) {
  const claim = (v: unknown, instrument: unknown) => obj(v) && txt(v.text) && Array.isArray(v.evidence_ids)
    && v.evidence_ids.length <= 240 && v.evidence_ids.every(id => evidence.get(id) === instrument)
    && new Set(v.evidence_ids).size === v.evidence_ids.length;
  const optionalClaim = (v: unknown, instrument: unknown) => v === null || claim(v, instrument);
  if (!Array.isArray(value.narratives) || value.narratives.length > 3 || !obj(value.quality)
    || !["complete", "partial", "fallback"].includes(String(value.quality.state))
    || !txt(value.quality.code, 100) || !Array.isArray(value.quality.reasons) || value.quality.reasons.length > 20
    || !value.quality.reasons.every(r => obj(r) && txt(r.code, 100) && txt(r.section, 30) && Number.isInteger(r.index))) return null;
  const seen = new Set();
  for (const n of value.narratives) {
    if (!obj(n) || !securities.has(n.instrument_id) || seen.has(n.instrument_id)
      || ![n.summary, n.current, n.stance].every(c => optionalClaim(c, n.instrument_id))
      || !Array.isArray(n.drivers) || n.drivers.length > 6 || !n.drivers.every(d => obj(d) && txt(d.title, 100) && claim(d, n.instrument_id))
      || !obj(n.scenarios) || !Object.keys(n.scenarios).every(k => ["base", "better", "worse"].includes(k))
      || !Object.values(n.scenarios).every(c => optionalClaim(c, n.instrument_id))
      || !Array.isArray(n.levels) || n.levels.length > 6
      || !n.levels.every(l => obj(l) && claim(l, n.instrument_id) && [l.lower, l.upper].every(p => typeof p === "string" && /^\d+(?:\.\d+)?$/.test(p) && Number(p) > 0)
        && Number(l.lower) <= Number(l.upper) && txt(l.lower_label, 100) && txt(l.upper_label, 100))) return null;
    seen.add(n.instrument_id);
  }
  return { narratives: value.narratives as Narrative[], quality: value.quality as NarrativeQuality };
}

export function InlineText({ text }: { text: string }) {
  return <>{text.split(/(\*\*[^*\n]+\*\*)/g).map((part, i) => part.startsWith("**") && part.endsWith("**")
    ? <strong key={i}>{part.slice(2, -2)}</strong> : part)}</>;
}
const reasons: Record<string, string> = { answer_numeric_unmatched: "部分数字未能对应资料，相关句子已移除",
  answer_binding_invalid: "部分数字的资料来源无效，相关句子已移除",
  answer_binding_value_mismatch: "部分数字与所标资料不一致，相关句子已移除",
  answer_horizon_mismatch: "部分研判期限未能对应本次问题", answer_stance_mismatch: "部分结论与操作倾向未通过核对",
  answer_hard_content: "部分内容未通过安全或证券身份核对", answer_structure_invalid: "部分回答结构未完成",
  answer_model_unavailable: "本轮模型分析未完成", answer_context_unusable: "本轮分析资料未通过核对" };
const levelLabels: Record<string, string> = { reference_entry: "入场参考", reference_stop: "止损参考",
  target_1: "目标1", target_2: "目标2", latest_close: "最新收盘价", close_price: "收盘价",
  reference_close: "收盘参考", exit_below: "退出观察位", observe_below: "下方观察位", observe_above: "上方观察位",
  reference_target: "目标参考", risk_per_share: "每股参考风险", reward_per_share: "每股参考收益" };
function levelLabel(label: string) {
  if (Object.hasOwn(levelLabels, label)) return levelLabels[label];
  return /[\u3400-\u9fff]/.test(label) || /^(?:MA|SMA|EMA)\d+$/.test(label) ? label : "";
}

export function ResearchNarrative({ narratives, quality, securities }: { narratives: Narrative[]; quality: NarrativeQuality;
  securities: { instrument_id: string; name: string }[] }) {
  const reason = quality.reasons[0]?.code ?? quality.code;
  return <div className="research-narratives">
    {quality.state !== "complete" && <p className="data-note" role="status">
      {quality.state === "fallback" ? "本轮模型分析未完成，以下为程序事实与参考价位" : Object.hasOwn(reasons, reason) ? reasons[reason] : "部分分析可用，未通过的内容已移除"}
    </p>}
    {narratives.map(n => <section key={n.instrument_id} aria-label={`${securities.find(s => s.instrument_id === n.instrument_id)?.name}分析`}>
      {securities.length > 1 && <h4>{securities.find(s => s.instrument_id === n.instrument_id)?.name}</h4>}
      {n.summary && <p className="narrative-conclusion"><strong><InlineText text={n.summary.text} /></strong></p>}
      {n.current && <p><InlineText text={n.current.text} /></p>}
      {n.drivers.length > 0 && <ul className="narrative-drivers">{n.drivers.map((d, i) =>
        <li key={i}><strong><InlineText text={d.title} /></strong>：<InlineText text={d.text} /></li>)}</ul>}
      {n.levels.length > 0 && <div className="table-scroll"><table className="reference-plan"><caption>关键价位区间 · 仅供参考</caption>
        <thead><tr><th>区间</th><th>{quality.state === "fallback" || n.origin === "application" ? "程序事实" : "我的判断"}</th></tr></thead>
        <tbody>{n.levels.map((l, i) => {
          const label = [...new Set([levelLabel(l.lower_label), levelLabel(l.upper_label)].filter(Boolean))].join(" / ");
          return <tr key={i}><td>{l.lower === l.upper ? l.lower : `${l.lower}—${l.upper}`} 元
            {label && <small>{label}</small>}</td><td><InlineText text={l.text} /></td></tr>;
        })}</tbody></table></div>}
      {Object.entries(n.scenarios).some(([, c]) => c) && <ul className="narrative-scenarios">{Object.entries(n.scenarios).map(([key, c]) => c &&
        <li key={key}><strong>{{ base: "基准情景", better: "变好条件", worse: "变坏条件" }[key]}</strong>：<InlineText text={c.text} /></li>)}</ul>}
      {n.stance && <p><InlineText text={n.stance.text} /></p>}
    </section>)}
  </div>;
}
