import type { PublicTurn } from "./api";
import { validChartDate } from "./charts";

const indices: Record<string, string> = { "000001.SH": "上证指数", "399001.SZ": "深证成指", "000300.SH": "沪深300", "399006.SZ": "创业板指", "000688.SH": "科创50" };
const record = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
type Evidence = { id: string; label: string; raw_value: number; unit: string; date: string; source: string };
type Quality = { version: string; generated_sentences: number; retained_sentences: number; deleted_sentences: number;
  retained_paragraphs: number; rejected_paragraphs: number; partially_retained_sentences: number; repair_attempts: number; partial: boolean };
export type IndexResearchData = { request_id: string; name: string; instrument_id: string; benchmark_defaulted: boolean;
  direction: string; conviction: string; conviction_note: string; horizon: string | null; latest_date: string;
  data_authenticity: string; context: string; blocks: { title: string; text: string; evidence_ids: string[] }[];
  evidence: Evidence[]; limitations: string[]; plan_notice: string | null; quality?: Quality };

export function parseIndexResearch(value: unknown, turn?: PublicTurn): IndexResearchData | null {
  if (!record(value) || value.version !== "index-research-v1" || value.instrument_kind !== "index" || value.price_unit !== "index_point"
    || typeof value.instrument_id !== "string" || indices[value.instrument_id] !== value.name
    || typeof value.request_id !== "string" || !/^[A-Za-z0-9_.-]{1,100}$/.test(value.request_id)
    || typeof value.benchmark_defaulted !== "boolean" || value.benchmark_defaulted && value.instrument_id !== "000001.SH"
    || !["偏多", "震荡", "偏空", "暂不判断"].includes(String(value.direction)) || !["高", "中", "低"].includes(String(value.conviction))
    || !["model", "program"].includes(String(value.origin)) || typeof value.conviction_note !== "string"
    || value.horizon !== null && (typeof value.horizon !== "string" || value.horizon.length > 120)
    || !validChartDate(value.start_date) || !validChartDate(value.as_of_date) || !validChartDate(value.latest_date)
    || value.start_date > value.latest_date || value.latest_date > value.as_of_date
    || !["tencent", "csv"].includes(String(value.source))
    || (value.source === "csv" ? value.data_authenticity !== "synthetic" : value.data_authenticity !== "provider_reported")
    || !["current", "saved"].includes(String(value.context))
    || !Array.isArray(value.evidence) || !value.evidence.length || value.evidence.length > 100
    || !Array.isArray(value.blocks) || value.blocks.length < 1 || value.blocks.length > 6
    || !Array.isArray(value.limitations) || value.limitations.length > 100 || !value.limitations.every((s) => typeof s === "string" && s.length < 300)
    || value.plan_notice !== null && (typeof value.plan_notice !== "string" || value.plan_notice.length > 300)) return null;
  if (turn && (turn.report?.request_id !== value.request_id || turn.parameters?.instrument_id !== value.instrument_id
    || turn.parameters.start_date !== value.start_date || turn.parameters.as_of_date !== value.as_of_date || turn.parameters.source !== value.source)) return null;
  const ids = new Set<string>();
  if (value.quality !== undefined) {
    const q = value.quality;
    const counts = ["generated_sentences", "retained_sentences", "deleted_sentences", "retained_paragraphs",
      "rejected_paragraphs", "partially_retained_sentences", "repair_attempts"];
    if (!record(q) || q.version !== "index-answer-quality-v1" || typeof q.partial !== "boolean"
      || !counts.every((key) => typeof q[key] === "number" && Number.isSafeInteger(q[key]) && (q[key] as number) >= 0 && (q[key] as number) <= 3600)
      || q.generated_sentences !== (q.retained_sentences as number) + (q.deleted_sentences as number)
      || (q.repair_attempts as number) > 1 || (q.retained_paragraphs as number) > 6 || (q.rejected_paragraphs as number) > 6
      || (q.partially_retained_sentences as number) > (q.retained_sentences as number)
      || q.partial !== Boolean(q.deleted_sentences || q.rejected_paragraphs || q.partially_retained_sentences)
      || value.origin === "model" && q.retained_paragraphs !== value.blocks.length) return null;
  }
  for (const item of value.evidence) {
    if (!record(item) || typeof item.id !== "string" || !/^E[1-9]\d*$/.test(item.id) || ids.has(item.id)
      || item.instrument_id !== value.instrument_id || item.source !== value.source || typeof item.label !== "string" || item.label.length > 200
      || typeof item.raw_value !== "number" || !Number.isFinite(item.raw_value) || Math.abs(item.raw_value) > 1e15
      || !["daily", "weekly"].includes(String(item.timeframe))
      || (item.indicator_id === null
        ? !["close_price", "window_low", "window_high"].includes(String(item.field)) || item.unit !== "index_point" || item.timeframe !== "daily"
        : typeof item.indicator_id !== "string" || !/^(sma|ema|macd|rsi|boll|atr|adx)\(/.test(item.indicator_id)
          || item.unit !== (/^(rsi|adx)\(/.test(item.indicator_id) ? "index_points" : "index_point"))
      || !validChartDate(item.date)
      || item.date < value.start_date || item.date > value.as_of_date) return null;
    ids.add(item.id);
  }
  for (const block of value.blocks) {
    if (!record(block) || typeof block.title !== "string" || !block.title.trim() || block.title.length > 40 || /[\x00-\x1f\x7f]/.test(block.title)
      || typeof block.text !== "string" || !block.text || block.text.length > 600 || !Array.isArray(block.evidence_ids)
      || !block.evidence_ids.length || !block.evidence_ids.every((id) => typeof id === "string" && ids.has(id))) return null;
  }
  if (!value.blocks.some((block) => block.title === "结论")) return null;
  return value as unknown as IndexResearchData;
}

export function IndexResearch({ turn }: { turn: PublicTurn }) {
  const data = parseIndexResearch(turn.index_research, turn);
  if (!data) return <p role="note">指数研究未通过显示校验，请重新提出研究问题。</p>;
  return <div className="response-content index-research">
    {data.benchmark_defaulted && <p>本次以上证指数作为大盘观察基准。</p>}
    <h4 className="response-headline">{data.name} · {data.direction}</h4>
    <p className="data-note">截至 {data.latest_date} · {data.horizon ?? "本轮技术观察"} · 把握{data.conviction}（{data.conviction_note}）</p>
    {data.context === "saved" && <p className="saved-result-label">基于已保存快照，未重新取数</p>}
    {data.data_authenticity === "synthetic" && <p className="data-note" role="note">人工指数样例，非真实行情</p>}
    {data.quality?.partial && <p className="data-note" role="note">部分内容未通过核对已移除</p>}
    {data.blocks.map((block, i) => <section className="insight readable-text" key={i}>
      <h5>{block.title}</h5><p>{block.text}</p>
      <small>依据：{block.evidence_ids.map((id) => data.evidence.find((e) => e.id === id)!).map((e) => `${e.label}（${e.date}）`).join("、")}</small>
    </section>)}
    {data.plan_notice && <p className="data-note" role="note">{data.plan_notice}</p>}
    <details><summary>数据范围与局限</summary><ul>{data.limitations.map((s, i) => <li key={i}>{s}</li>)}</ul></details>
  </div>;
}
